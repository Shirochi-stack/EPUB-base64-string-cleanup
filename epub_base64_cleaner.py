import os
import json
import re
import tempfile
import zipfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from PySide6 import QtCore, QtGui, QtWidgets


def _get_config_path():
    base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or os.path.expanduser("~")
    cfg_dir = os.path.join(base, "EpubBase64Cleaner")
    try:
        os.makedirs(cfg_dir, exist_ok=True)
    except Exception:
        pass
    return os.path.join(cfg_dir, "config.json")


CONFIG_PATH = _get_config_path()


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(cfg: dict):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def strip_base64_blobs(text: str):
    if not text:
        return text, []
    removed = []
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
    removed += re.findall(r"[A-Za-z0-9+/=]{40,}", text)
    text = re.sub(r"[A-Za-z0-9+/=]{40,}", "", text)
    return text, removed


def process_epub(input_path: str, output_path: str, report_path: str, log):
    if not zipfile.is_zipfile(input_path):
        raise ValueError("Input file is not a valid EPUB/ZIP.")

    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(input_path, "r") as zf:
            zf.extractall(tmpdir)

        exts = {".xhtml", ".html", ".htm", ".xml", ".opf", ".ncx"}
        modified = 0
        scanned = 0
        removed_total = 0
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
                    if removed:
                        removed_total += len(removed)
                    if cleaned != original:
                        with open(path, "w", encoding="utf-8") as f:
                            f.write(cleaned)
                        modified += 1
                        if removed:
                            with open(report_path, "a", encoding="utf-8") as rf:
                                rel = os.path.relpath(path, tmpdir)
                                rf.write(f"== {rel} ==\n")
                                for item in removed:
                                    rf.write(item.strip() + "\n")
                                rf.write("\n")
                except Exception:
                    continue

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

    log(f"{os.path.basename(input_path)} -> scanned: {scanned}, removed base64 strings: {removed_total}")
    return input_path, output_path, report_path


class CleanerWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self._config = load_config()
        self.files = []
        self._log_lock = QtCore.QMutex()

        self.setWindowTitle("EPUB Base64 Cleaner")
        icon_path = os.path.join(os.path.dirname(__file__), "broom.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QtGui.QIcon(icon_path))
        screen = QtGui.QGuiApplication.primaryScreen().availableGeometry()
        w = int(screen.width() * 0.45)
        h = int(screen.height() * 0.45)
        self.resize(w, h)
        self.setMinimumSize(int(screen.width() * 0.35), int(screen.height() * 0.35))

        self._build_ui()
        last_out = self._config.get("last_output_dir")
        if last_out:
            self.output_edit.setText(last_out)

        self.setAcceptDrops(True)

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        row_in = QtWidgets.QHBoxLayout()
        row_in.addWidget(QtWidgets.QLabel("Input EPUB(s)"))
        row_in.addStretch(1)
        browse_in = QtWidgets.QPushButton("Browse...")
        browse_in.clicked.connect(self.browse_input)
        row_in.addWidget(browse_in)
        layout.addLayout(row_in)

        row_out = QtWidgets.QHBoxLayout()
        row_out.addWidget(QtWidgets.QLabel("Output Folder"))
        self.output_edit = QtWidgets.QLineEdit()
        row_out.addWidget(self.output_edit, 1)
        browse_out = QtWidgets.QPushButton("Browse...")
        browse_out.clicked.connect(self.browse_output)
        row_out.addWidget(browse_out)
        layout.addLayout(row_out)

        btn_row = QtWidgets.QHBoxLayout()
        clean_btn = QtWidgets.QPushButton("Clean EPUB")
        clean_btn.clicked.connect(self.run_clean)
        btn_row.addWidget(clean_btn)
        remove_btn = QtWidgets.QPushButton("Remove Selected")
        remove_btn.clicked.connect(self.remove_selected)
        btn_row.addWidget(remove_btn)
        clear_btn = QtWidgets.QPushButton("Clear List")
        clear_btn.clicked.connect(self.clear_list)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.list_widget = QtWidgets.QListWidget()
        self.list_widget.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.list_widget.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self.show_context_menu)
        layout.addWidget(self.list_widget)

        self.log_text = QtWidgets.QTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text, 1)

        self._apply_dark_theme()

    def _apply_dark_theme(self):
        palette = QtGui.QPalette()
        palette.setColor(QtGui.QPalette.Window, QtGui.QColor(30, 30, 30))
        palette.setColor(QtGui.QPalette.WindowText, QtGui.QColor(230, 230, 230))
        palette.setColor(QtGui.QPalette.Base, QtGui.QColor(43, 43, 43))
        palette.setColor(QtGui.QPalette.Text, QtGui.QColor(230, 230, 230))
        palette.setColor(QtGui.QPalette.Button, QtGui.QColor(51, 51, 51))
        palette.setColor(QtGui.QPalette.ButtonText, QtGui.QColor(230, 230, 230))
        palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor(68, 68, 68))
        palette.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor(255, 255, 255))
        self.setPalette(palette)

    def log(self, msg: str):
        # microsecond backoff lock to avoid contention from parallel threads
        while True:
            if self._log_lock.tryLock():
                break
            time.sleep(0.000001)
        try:
            QtCore.QMetaObject.invokeMethod(
                self.log_text,
                "append",
                QtCore.Qt.QueuedConnection,
                QtCore.Q_ARG(str, msg),
            )
        finally:
            self._log_lock.unlock()


    def browse_input(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Select EPUB(s)", "", "EPUB files (*.epub);;All files (*)"
        )
        if paths:
            self.add_files(paths)

    def browse_output(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Select output folder")
        if path:
            self.output_edit.setText(path)
            self._config["last_output_dir"] = path
            save_config(self._config)

    def add_files(self, paths):
        added = 0
        for p in paths:
            if not p or not p.lower().endswith(".epub"):
                continue
            if p not in self.files:
                self.files.append(p)
                self.list_widget.addItem(p)
                added += 1
        if added:
            self.log(f"Added {added} file(s).")

    def show_context_menu(self, pos):
        menu = QtWidgets.QMenu(self)
        menu.addAction("Remove Selected", self.remove_selected)
        menu.addAction("Clear List", self.clear_list)
        menu.exec(self.list_widget.mapToGlobal(pos))

    def remove_selected(self):
        for item in self.list_widget.selectedItems():
            path = item.text()
            if path in self.files:
                self.files.remove(path)
            self.list_widget.takeItem(self.list_widget.row(item))

    def clear_list(self):
        self.files.clear()
        self.list_widget.clear()

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QtGui.QDropEvent):
        urls = event.mimeData().urls()
        paths = [u.toLocalFile() for u in urls if u.isLocalFile()]
        self.add_files(paths)

    def run_clean(self):
        out_dir = self.output_edit.text().strip()
        if not self.files:
            QtWidgets.QMessageBox.warning(self, "Missing input", "Please select one or more EPUB files.")
            return
        base_dir = os.path.dirname(self.files[0])
        if not out_dir:
            out_dir = os.path.join(base_dir, "Cleaned EPUBs")
            self.output_edit.setText(out_dir)
            self.log(f"Output folder not set; defaulting to: {out_dir}")
        # If output dir equals input dir, redirect to Cleaned EPUBs to avoid overwrite
        if os.path.abspath(out_dir) == os.path.abspath(base_dir):
            out_dir = os.path.join(base_dir, "Cleaned EPUBs")
            self.output_edit.setText(out_dir)
            self.log(f"Output folder matched input; using: {out_dir}")
        self._config["last_output_dir"] = out_dir
        save_config(self._config)

        def worker():
            try:
                self.log("Processing...")
                os.makedirs(out_dir, exist_ok=True)
                max_workers = min(4, max(1, len(self.files)))
                with ThreadPoolExecutor(max_workers=max_workers) as ex:
                    futures = []
                    for in_path in self.files:
                        base = os.path.splitext(os.path.basename(in_path))[0]
                        out_path = os.path.join(out_dir, base + ".epub")
                        logs_dir = os.path.join(out_dir, "logs")
                        os.makedirs(logs_dir, exist_ok=True)
                        report_path = os.path.join(logs_dir, base + "_removed.txt")
                        try:
                            if os.path.exists(report_path):
                                os.remove(report_path)
                        except Exception:
                            pass
                        label = os.path.basename(in_path)
                        self.log(f"Processing: {label}")
                        futures.append(ex.submit(process_epub, in_path, out_path, report_path, self.log))
                    for fut in as_completed(futures):
                        try:
                            in_path, out_path, report_path = fut.result()
                            self.log(f"Saved: {out_path}")
                            self.log(f"Report: {report_path}")
                        except Exception as e:
                            self.log(f"Error: {e}")
                self.log("Done.")
            except Exception as e:
                self.log(f"Error: {e}")

        QtCore.QThreadPool.globalInstance().start(_Worker(worker))


class _Worker(QtCore.QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        self.fn()


def main():
    app = QtWidgets.QApplication([])
    win = CleanerWindow()
    win.show()
    app.exec()


if __name__ == "__main__":
    main()
