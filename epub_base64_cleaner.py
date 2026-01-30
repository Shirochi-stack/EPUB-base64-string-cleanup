import os
import re
import threading
import tempfile
import zipfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


def strip_base64_blobs(text: str):
    if not text:
        return text, []
    removed = []
    # Remove hidden paragraphs with base64 (common pattern)
    removed += re.findall(
        r"<p\s+style=['\"]height:\s*0px;[^>]*>.*?</p>",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(
        r"<p\s+style=['\"]height:\s*0px;[^>]*>.*?</p>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Remove inline base64 data URLs
    removed += re.findall(
        r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # Remove embedded base64-ish tokens (>=40 chars)
    removed += re.findall(r"[A-Za-z0-9+/=]{40,}", text)
    text = re.sub(r"[A-Za-z0-9+/=]{40,}", "", text)
    return text, removed


def process_epub(input_path: str, output_path: str, report_path: str, log):
    if not zipfile.is_zipfile(input_path):
        raise ValueError("Input file is not a valid EPUB/ZIP.")

    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(input_path, "r") as zf:
            zf.extractall(tmpdir)

        # Process text files likely to contain base64 blobs
        exts = {".xhtml", ".html", ".htm", ".xml", ".opf", ".ncx"}
        modified = 0
        scanned = 0
        for root, _, files in os.walk(tmpdir):
            for name in files:
                ext = os.path.splitext(name)[1].lower()
                if ext not in exts:
                    continue
                path = os.path.join(root, name)
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        original = f.read()
                    cleaned, removed = strip_base64_blobs(original)
                    scanned += 1
                    if cleaned != original:
                        with open(path, "w", encoding="utf-8") as f:
                            f.write(cleaned)
                        modified += 1
                        # Log removed content to report file
                        if removed:
                            with open(report_path, "a", encoding="utf-8") as rf:
                                rel = os.path.relpath(path, tmpdir)
                                rf.write(f"== {rel} ==\n")
                                for item in removed:
                                    rf.write(item.strip() + "\n")
                                rf.write("\n")
                except Exception:
                    continue

        # Rebuild EPUB (mimetype must be first and uncompressed)
        with zipfile.ZipFile(output_path, "w") as out:
            mimetype_path = os.path.join(tmpdir, "mimetype")
            if os.path.exists(mimetype_path):
                out.write(mimetype_path, "mimetype", compress_type=zipfile.ZIP_STORED)
            for root, _, files in os.walk(tmpdir):
                for name in files:
                    full_path = os.path.join(root, name)
                    rel_path = os.path.relpath(full_path, tmpdir)
                    if rel_path == "mimetype":
                        continue
                    out.write(full_path, rel_path, compress_type=zipfile.ZIP_DEFLATED)

    log(f"{os.path.basename(input_path)} -> scanned: {scanned}, modified: {modified}, report: {os.path.basename(report_path)}")


class Base64CleanerGUI(tk.Tk):
    def __init__(self):
        # Try to enable drag-and-drop if tkinterdnd2 is available
        self._dnd_enabled = False
        try:
            from tkinterdnd2 import TkinterDnD, DND_FILES
            self._DND_FILES = DND_FILES
            TkinterDnD.Tk.__init__(self)
            self._dnd_enabled = True
        except Exception:
            super().__init__()
            self._DND_FILES = None
        self.title("EPUB Base64 Cleaner")
        self.geometry("640x360")
        self.minsize(520, 300)
        self.var_output = tk.StringVar()
        self.files = []
        self._log_lock = threading.Lock()

        self._build_ui()

    def _build_ui(self):
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        row_in = ttk.Frame(frm)
        row_in.pack(fill="x", pady=(0, 8))
        ttk.Label(row_in, text="Input EPUB(s)").pack(side="left")
        ttk.Button(row_in, text="Browse...", command=self.browse_input).pack(side="right")

        row_out = ttk.Frame(frm)
        row_out.pack(fill="x", pady=(0, 8))
        ttk.Label(row_out, text="Output Folder").pack(side="left")
        ttk.Entry(row_out, textvariable=self.var_output).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row_out, text="Browse...", command=self.browse_output).pack(side="right")

        btn_row = ttk.Frame(frm)
        btn_row.pack(pady=6, fill="x")
        btn_run = ttk.Button(btn_row, text="Clean EPUB", command=self.run_clean)
        btn_run.pack(side="left")
        ttk.Button(btn_row, text="Remove Selected", command=self.remove_selected).pack(side="left", padx=6)
        ttk.Button(btn_row, text="Clear List", command=self.clear_list).pack(side="left")

        self.files_list = tk.Listbox(frm, height=6)
        self.files_list.pack(fill="both", expand=False, pady=(4, 6))
        if self._dnd_enabled:
            self.files_list.drop_target_register(self._DND_FILES)
            self.files_list.dnd_bind("<<Drop>>", self.on_drop)
        self._build_context_menu()
        self.log_text = tk.Text(frm, height=10, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, pady=(8, 0))
        if not self._dnd_enabled:
            self.log("Drag-and-drop disabled (install tkinterdnd2 to enable).")

    def log(self, msg: str):
        # microsecond backoff lock to avoid contention from parallel threads
        while True:
            acquired = self._log_lock.acquire(False)
            if acquired:
                break
            time.sleep(0.000001)
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        finally:
            self._log_lock.release()

    def browse_input(self):
        paths = filedialog.askopenfilenames(
            title="Select EPUB(s)",
            filetypes=[("EPUB files", "*.epub"), ("All files", "*.*")]
        )
        if paths:
            self.add_files(list(paths))

    def browse_output(self):
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.var_output.set(path)

    def on_drop(self, event):
        # event.data may contain a Tcl list of filenames
        data = event.data
        if not data:
            return
        try:
            files = self.tk.splitlist(data)
        except Exception:
            files = [p.strip() for p in data.split() if p.strip()]
        self.add_files(list(files))

    def add_files(self, paths):
        added = 0
        for p in paths:
            p = p.strip().strip("{}")
            if not p or not p.lower().endswith(".epub"):
                continue
            if p not in self.files:
                self.files.append(p)
                self.files_list.insert("end", p)
                added += 1
        if added:
            self.log(f"Added {added} file(s).")

    def _build_context_menu(self):
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Remove Selected", command=self.remove_selected)
        self.menu.add_command(label="Clear List", command=self.clear_list)
        self.files_list.bind("<Button-3>", self.show_context_menu)

    def show_context_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def remove_selected(self):
        sel = list(self.files_list.curselection())
        if not sel:
            return
        for idx in reversed(sel):
            path = self.files_list.get(idx)
            if path in self.files:
                self.files.remove(path)
            self.files_list.delete(idx)

    def clear_list(self):
        self.files.clear()
        self.files_list.delete(0, "end")

    def run_clean(self):
        out_dir = self.var_output.get().strip()
        if not self.files:
            messagebox.showwarning("Missing input", "Please select one or more EPUB files.")
            return
        if not out_dir:
            messagebox.showwarning("Missing output", "Please select an output folder.")
            return

        def worker():
            try:
                self.log("Processing...")
                os.makedirs(out_dir, exist_ok=True)
                max_workers = min(4, max(1, len(self.files)))
                with ThreadPoolExecutor(max_workers=max_workers) as ex:
                    futures = []
                    for in_path in self.files:
                        base = os.path.splitext(os.path.basename(in_path))[0]
                        out_path = os.path.join(out_dir, base + "_cleaned.epub")
                        logs_dir = os.path.join(out_dir, "logs")
                        os.makedirs(logs_dir, exist_ok=True)
                        report_path = os.path.join(logs_dir, base + "_removed.txt")
                        # ensure report is fresh
                        try:
                            if os.path.exists(report_path):
                                os.remove(report_path)
                        except Exception:
                            pass
                        futures.append(ex.submit(process_epub, in_path, out_path, report_path, self.log))
                    for fut in as_completed(futures):
                        try:
                            fut.result()
                        except Exception as e:
                            self.log(f"Error: {e}")
                self.log("Done.")
            except Exception as e:
                self.log(f"Error: {e}")

        threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
    app = Base64CleanerGUI()
    app.mainloop()
