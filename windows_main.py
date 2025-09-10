import os, sys, json, threading, time, tempfile, re, posixpath, stat as py_stat
from dataclasses import dataclass, asdict
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QMessageBox, QListWidget, QDialog, QLineEdit,
    QFileDialog, QListWidgetItem, QTabWidget, QPlainTextEdit, QSplitter,
    QInputDialog, QMenu, QStyle, QColorDialog, QFontDialog
)
from PySide6.QtGui import QAction, QFont, QColor, QDrag
from PySide6.QtCore import Qt, QProcess, Signal, QObject, QMimeData
import paramiko

# -----------------------
# Windows-specific paths
# -----------------------
APP_NAME = "sftp_client_improved"
DEFAULT_CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
os.makedirs(DEFAULT_CONFIG_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(DEFAULT_CONFIG_DIR, "sftp_config.json")
THEME_FILE = os.path.join(DEFAULT_CONFIG_DIR, "theme.json")

# -----------------------
# Theme (personalization)
# -----------------------
@dataclass
class Theme:
    name: str = "Default"
    bg_color: str = "#13111b"
    text_color: str = "#eae6ff"
    font_family: str = "Consolas"
    font_size: int = 11
    def qss(self) -> str:
        return (
            f"* {{ color: {self.text_color}; font-family: '{self.font_family}'; font-size: {self.font_size}pt; }} "
            f"QMainWindow {{ background-color: {self.bg_color}; }}"
            f"QPlainTextEdit {{ background-color: black; color: lime; font-family: Consolas; }}"
        )

class ThemeManager:
    def __init__(self, app: QApplication): self.app=app; self.theme=self.load()
    def load(self) -> Theme:
        try:
            if os.path.exists(THEME_FILE):
                data=json.load(open(THEME_FILE,"r",encoding="utf-8"))
                base=Theme(); base.__dict__.update(data); return base
        except: pass
        return Theme()
    def save(self): json.dump(asdict(self.theme),open(THEME_FILE,"w",encoding="utf-8"),indent=2,ensure_ascii=False)
    def apply(self,win): self.app.setStyleSheet(self.theme.qss()); win.setFont(QFont(self.theme.font_family,self.theme.font_size))

# -----------------------
# SSH Terminal Helper
# -----------------------
class SSHTerminal(QObject):
    output_received = Signal(str)
    def __init__(self,transport): super().__init__(); self.transport=transport; self.channel=None; self._running=False
    def open(self):
        try: self.channel=self.transport.open_session(); self.channel.get_pty(); self.channel.invoke_shell()
        except: return False
        self._running=True; threading.Thread(target=self._reader,daemon=True).start(); return True
    def _reader(self):
        while self._running and self.channel and not self.channel.closed:
            if self.channel.recv_ready():
                data=self.channel.recv(4096).decode(errors="ignore")
                clean=re.sub(r"\x1b\[[0-9;?]*[A-Za-z]","",data)
                self.output_received.emit(clean)
            else: time.sleep(0.05)
    def send(self,txt): 
        if self.channel: self.channel.send(txt+"\n")
    def close(self):
        self._running=False
        try: self.channel.close()
        except: pass

# -----------------------
# Local CMD
# -----------------------
class CmdConsole(QWidget):
    def __init__(self):
        super().__init__(); l=QVBoxLayout(self)
        self.out=QPlainTextEdit(); self.out.setReadOnly(True)
        self.inp=QLineEdit(); self.inp.setPlaceholderText("cmd> ..."); self.inp.returnPressed.connect(self.send)
        l.addWidget(self.out); l.addWidget(self.inp)
        self.proc=QProcess(self); self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self.on_out); self.proc.start("cmd.exe")
    def on_out(self): self.out.appendPlainText(self.proc.readAllStandardOutput().data().decode(errors="ignore"))
    def send(self):
        t = self.inp.text().strip()
        if t:
            self.proc.write((t + "\n").encode())
            self.inp.clear()

# -----------------------
# ConnectionTab
# -----------------------
class ConnectionTab(QWidget):
    def __init__(self,cfg,theme_mgr):
        super().__init__(); self.cfg=cfg; self.theme_mgr=theme_mgr
        self.remote_path=cfg.get("remote_path","/"); self.local_path=cfg.get("local_path") or os.path.expanduser("~")
        self.sftp=None; self.transport=None; self.ssh_terminal=None

        root=QVBoxLayout(self); split=QSplitter(Qt.Horizontal)

        # Remote Panel
        left=QVBoxLayout(); lw=QWidget(); lw.setLayout(left)
        self.remote_label=QLabel(f"Remote: {self.remote_path}"); left.addWidget(self.remote_label)
        btn_r=QPushButton("🔄 Refresh Remote"); btn_r.clicked.connect(self.list_remote_files); left.addWidget(btn_r)
        self.remote_list=QListWidget(self); self.remote_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.remote_list.customContextMenuRequested.connect(self.remote_menu); self.remote_list.itemDoubleClicked.connect(self.remote_double)
        left.addWidget(self.remote_list); split.addWidget(lw)

        # Local Panel
        right=QVBoxLayout(); rw=QWidget(); rw.setLayout(right)
        self.local_label=QLabel(f"Local: {self.local_path}"); right.addWidget(self.local_label)
        btn_l=QPushButton("🔄 Refresh Local"); btn_l.clicked.connect(self.list_local_files); right.addWidget(btn_l)
        self.local_list=QListWidget(self); self.local_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.local_list.customContextMenuRequested.connect(self.local_menu); self.local_list.itemDoubleClicked.connect(self.local_double)
        right.addWidget(self.local_list)

        # Terminal
        self.term=QPlainTextEdit(); self.term.setReadOnly(True)
        self.term_in=QLineEdit(); self.term_in.setPlaceholderText("ssh> ..."); self.term_in.returnPressed.connect(self.send_term)
        right.addWidget(QLabel("SSH Terminal")); right.addWidget(self.term); right.addWidget(self.term_in)

        split.addWidget(rw); root.addWidget(split)
        self.connect_all(); self.list_remote_files(); self.list_local_files()

    def icon(self,is_dir): return QApplication.style().standardIcon(QStyle.SP_DirIcon if is_dir else QStyle.SP_FileIcon)
    def connect_all(self):
        try:
            if self.sftp:self.sftp.close()
            if self.transport:self.transport.close()
        except: pass
        try:
            t=paramiko.Transport((self.cfg["server"],int(self.cfg.get("port",22))))
            t.connect(username=self.cfg["username"],password=self.cfg.get("password"))
            self.transport=t; self.sftp=paramiko.SFTPClient.from_transport(t)
            self.ssh_terminal=SSHTerminal(t); self.ssh_terminal.open(); self.ssh_terminal.output_received.connect(self.term.appendPlainText)
        except Exception as e: QMessageBox.critical(self,"Connect Error",str(e))

    # ---------- List ------------
    def list_remote_files(self):
        self.remote_list.clear()
        try:
            self.remote_list.addItem("..")
            for f in self.sftp.listdir_attr(self.remote_path):
                it=QListWidgetItem(f.filename+("/" if py_stat.S_ISDIR(f.st_mode) else "")); it.setData(Qt.UserRole,f.filename)
                it.setIcon(self.icon(py_stat.S_ISDIR(f.st_mode))); self.remote_list.addItem(it)
            self.remote_label.setText(f"Remote: {self.remote_path}")
        except Exception as e: print(e)
    def list_local_files(self):
        self.local_list.clear()
        try:
            self.local_list.addItem("..")
            for n in os.listdir(self.local_path):
                p=os.path.join(self.local_path,n); it=QListWidgetItem(n+("/" if os.path.isdir(p) else "")); it.setData(Qt.UserRole,n)
                it.setIcon(self.icon(os.path.isdir(p))); self.local_list.addItem(it)
            self.local_label.setText(f"Local: {self.local_path}")
        except Exception as e: print(e)

    # ---------- Double Click ------------
    def remote_double(self, it):
        name = it.data(Qt.UserRole) or it.text()
        if name == "..":
            self.remote_path = posixpath.dirname(self.remote_path.rstrip("/")) or "/"
            self.list_remote_files()
            return

        path = posixpath.join(self.remote_path, name)
        if self.is_dir(path):
            self.remote_path = path
            self.list_remote_files()
        else:
            try:
                fd, tmpfile = tempfile.mkstemp(prefix="sftp_", suffix="_" + name)
                os.close(fd)
                self.sftp.get(path, tmpfile)
                os.startfile(tmpfile)   # ← always full path
            except Exception as e:
                QMessageBox.critical(self, "Remote opening error", str(e))

    def local_double(self, it):
        name = it.data(Qt.UserRole) or it.text()
        if name == "..":
            self.local_path = os.path.dirname(self.local_path) or self.local_path
            self.list_local_files()
            return
        path = os.path.join(self.local_path, name)
        if os.path.isdir(path):
            self.local_path = path
            self.list_local_files()
        else:
            try:
                os.startfile(path)   # ← always full path
            except Exception as e:
                QMessageBox.critical(self, "Opening failed", str(e))

    def is_dir(self,path): 
        try:return py_stat.S_ISDIR(self.sftp.stat(path).st_mode)
        except: return False

    # ---------- Context Menus ------------
    def remote_menu(self,pos):
        it=self.remote_list.itemAt(pos); m=QMenu(self)
        if it:
            m.addAction("Open",lambda:self.remote_double(it))
            m.addAction("Rename",lambda:self.rename_remote(it))
            m.addAction("Delete",lambda:self.delete_remote(it))
            m.addAction("New File",self.create_remote_file)
            m.addAction("New Folder",self.create_remote_dir)
        m.exec(self.remote_list.mapToGlobal(pos))
    def local_menu(self,pos):
        it=self.local_list.itemAt(pos); m=QMenu(self)
        if it:
            m.addAction("Open",lambda:self.local_double(it))
            m.addAction("Rename",lambda:self.rename_local(it))
            m.addAction("Delete",lambda:self.delete_local(it))
            m.addAction("New File",self.create_local_file)
            m.addAction("New Folder",self.create_local_dir)
            m.addAction("Upload to Remote",lambda:self.upload_file(it))
            m.exec(self.local_list.mapToGlobal(pos))
    # ---------- Actions ------------
    def rename_remote(self,it):
        n=it.data(Qt.UserRole); new,ok=QInputDialog.getText(self,"Rename Remote","New:",text=n)
        if ok:self.sftp.rename(posixpath.join(self.remote_path,n),posixpath.join(self.remote_path,new)); self.list_remote_files()
    def delete_remote(self,it):
        n=it.data(Qt.UserRole); p=posixpath.join(self.remote_path,n)
        try:self.sftp.remove(p)
        except:self.sftp.rmdir(p)
        self.list_remote_files()
    def create_remote_file(self):
        n,ok=QInputDialog.getText(self,"New File (remote)","Name:");
        if ok: f=self.sftp.open(posixpath.join(self.remote_path,n),"w"); f.close(); self.list_remote_files()
    def create_remote_dir(self):
        n,ok=QInputDialog.getText(self,"New Folder (remote)","Name:");
        if ok:self.sftp.mkdir(posixpath.join(self.remote_path,n)); self.list_remote_files()

    def upload_file(self,it):
        n=it.data(Qt.UserRole); src=os.path.join(self.local_path,n); dst=posixpath.join(self.remote_path,n)
        try:self.sftp.put(src,dst); self.list_remote_files()
        except Exception as e: QMessageBox.critical(self,"Upload Error",str(e))
    def rename_local(self,it):
        n=it.data(Qt.UserRole); new,ok=QInputDialog.getText(self,"Rename Local","New:",text=n)
        if ok: os.rename(os.path.join(self.local_path,n),os.path.join(self.local_path,new)); self.list_local_files()
    def delete_local(self,it):
        n=it.data(Qt.UserRole); p=os.path.join(self.local_path,n)
        try: os.remove(p)
        except: os.rmdir(p)
        self.list_local_files()
    def create_local_file(self):
        n,ok=QInputDialog.getText(self,"New File (local)","Name:");
        if ok: open(os.path.join(self.local_path,n),"w").close(); self.list_local_files()
    def create_local_dir(self):
        n,ok=QInputDialog.getText(self,"New Folder (local)","Name:");
        if ok: os.makedirs(os.path.join(self.local_path,n),exist_ok=True); self.list_local_files()

    # ---------- Terminal ------------
    def send_term(self):
        t=self.term_in.text().strip()
        if not t:return
        if t.startswith("cd "):
            arg=t[3:].strip(); self.remote_path=posixpath.normpath(posixpath.join(self.remote_path,arg)); self.list_remote_files()
        self.ssh_terminal.send(t); self.term_in.clear()

# -----------------------
# Config Dialog
# -----------------------
class ConfigDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Connection")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Server"))
        self.server = QLineEdit()
        layout.addWidget(self.server)

        layout.addWidget(QLabel("Port"))
        self.port = QLineEdit("22")
        layout.addWidget(self.port)

        layout.addWidget(QLabel("Username"))
        self.username = QLineEdit()
        layout.addWidget(self.username)

        layout.addWidget(QLabel("Password (optional)"))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        layout.addWidget(self.password)

        layout.addWidget(QLabel("Private Key (optional)"))
        key_row = QHBoxLayout()
        self.keyfile = QLineEdit()
        key_btn = QPushButton("Choose")
        key_btn.clicked.connect(self.pick_key)
        key_row.addWidget(self.keyfile); key_row.addWidget(key_btn)
        layout.addLayout(key_row)

        layout.addWidget(QLabel("Remote Path (optional)"))
        self.remote_path = QLineEdit("/")
        layout.addWidget(self.remote_path)

        layout.addWidget(QLabel("Local Clone Folder (optional)"))
        local_row = QHBoxLayout()
        self.local_path = QLineEdit()
        local_btn = QPushButton("Choose")
        local_btn.clicked.connect(self.pick_local)
        local_row.addWidget(self.local_path); local_row.addWidget(local_btn)
        layout.addLayout(local_row)

        btns = QHBoxLayout()
        ok = QPushButton("OK"); ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        btns.addWidget(ok); btns.addWidget(cancel)
        layout.addLayout(btns)

    def pick_key(self):
        start = os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(self, "Choose Private Key", start)
        if path: self.keyfile.setText(path)

    def pick_local(self):
        path = QFileDialog.getExistingDirectory(self, "Choose Local Folder", os.path.expanduser("~"))
        if path: self.local_path.setText(path)

    def get_data(self):
        return {
            "server": self.server.text().strip(),
            "port": int(self.port.text().strip() or 22),
            "username": self.username.text().strip(),
            "password": self.password.text(),
            "keyfile": self.keyfile.text().strip() or None,
            "remote_path": self.remote_path.text().strip() or "/",
            "local_path": self.local_path.text().strip() or ""
        }

# -----------------------
# Settings / Personalization Dialog
# -----------------------
class SettingsDialog(QDialog):
    def __init__(self, theme_mgr: ThemeManager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings / Personalization")
        self.theme_mgr = theme_mgr
        layout = QVBoxLayout(self)

        # Background color
        bg_row = QHBoxLayout()
        bg_row.addWidget(QLabel("Background Color"))
        self.bg_btn = QPushButton("Choose")
        self.bg_btn.clicked.connect(self.pick_bg)
        bg_row.addWidget(self.bg_btn)
        layout.addLayout(bg_row)

        # Text color
        text_row = QHBoxLayout()
        text_row.addWidget(QLabel("Text Color"))
        self.text_btn = QPushButton("Choose")
        self.text_btn.clicked.connect(self.pick_text)
        text_row.addWidget(self.text_btn)
        layout.addLayout(text_row)

        # Font
        font_row = QHBoxLayout()
        font_row.addWidget(QLabel("Font"))
        self.font_btn = QPushButton("Choose")
        self.font_btn.clicked.connect(self.pick_font)
        font_row.addWidget(self.font_btn)
        layout.addLayout(font_row)

        # Font size
        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Font Size"))
        self.size_input = QLineEdit(str(self.theme_mgr.theme.font_size))
        size_row.addWidget(self.size_input)
        layout.addLayout(size_row)

        btns = QHBoxLayout()
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self.apply)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self.save_and_close)
        cancel = QPushButton("Close")
        cancel.clicked.connect(self.reject)
        btns.addWidget(apply_btn); btns.addWidget(save_btn); btns.addWidget(cancel)
        layout.addLayout(btns)

    def pick_bg(self):
        color = QColorDialog.getColor(QColor(self.theme_mgr.theme.bg_color), self, "Choose Background Color")
        if color.isValid():
            self.theme_mgr.theme.bg_color = color.name()

    def pick_text(self):
        color = QColorDialog.getColor(QColor(self.theme_mgr.theme.text_color), self, "Choose Text Color")
        if color.isValid():
            self.theme_mgr.theme.text_color = color.name()

    def pick_font(self):
        ok, font = QFontDialog.getFont(QFont(self.theme_mgr.theme.font_family, self.theme_mgr.theme.font_size), self)
        if ok:
            self.theme_mgr.theme.font_family = font.family()
            self.theme_mgr.theme.font_size = font.pointSize()

    def apply(self):
        try:
            self.theme_mgr.theme.font_size = int(self.size_input.text().strip() or self.theme_mgr.theme.font_size)
        except Exception:
            pass
        self.theme_mgr.apply(self.parent())

    def save_and_close(self):
        self.apply()
        self.theme_mgr.save()
        self.accept()

# -----------------------
# Main Application Window
# -----------------------
class AppWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SFTP Client (Windows)")
        self.resize(1200, 700)

        self.theme_mgr = ThemeManager(QApplication.instance())
        self.theme_mgr.apply(self)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.setCentralWidget(self.tabs)

        menubar = self.menuBar()
        srv_menu = menubar.addMenu("Server")
        new_conn_act = QAction("New Connection...", self)
        new_conn_act.triggered.connect(self.open_new_conn_dialog)
        srv_menu.addAction(new_conn_act)

        tools = menubar.addMenu("Tools")
        settings_act = QAction("Settings", self)
        settings_act.triggered.connect(self.open_settings)
        tools.addAction(settings_act)

        local_menu = menubar.addMenu("Local")
        cmd_act = QAction("New CMD Tab", self)
        cmd_act.triggered.connect(self.open_cmd_tab)
        local_menu.addAction(cmd_act)
        icon = self.style().standardIcon(QStyle.SP_ComputerIcon)
        self.setWindowIcon(icon)
        self.saved_configs = self.load_saved_configs()
        for cfg in self.saved_configs:
            self.add_connection_tab(cfg)

    def load_saved_configs(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else [data]
            except Exception:
                pass
        return []

    def save_configs(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.saved_configs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("save configs error:", e)

    def open_new_conn_dialog(self):
        dlg = ConfigDialog(self)
        if dlg.exec():
            cfg = dlg.get_data()
            # ensure local_path defaults to user's home directory
            if not cfg.get("local_path"):
                cfg["local_path"] = os.path.join(os.path.expanduser("~"), "Cloned_From_" + cfg.get("server", "server"))
            self.saved_configs.append(cfg)
            self.save_configs()
            self.add_connection_tab(cfg)

    def add_connection_tab(self, cfg: dict):
        tab = ConnectionTab(cfg, self.theme_mgr)
        title = f"{cfg.get('username')}@{cfg.get('server')}"
        self.tabs.addTab(tab, title)

    def open_cmd_tab(self):
        tab = CmdConsole()
        self.tabs.addTab(tab, "CMD")

    def close_tab(self, index: int):
        widget = self.tabs.widget(index)
        if widget:
            # try to cleanup connections if ConnectionTab
            try:
                if isinstance(widget, ConnectionTab):
                    if widget.ssh_terminal:
                        widget.ssh_terminal.close()
                    if widget.sftp:
                        widget.sftp.close()
                    if widget.transport:
                        widget.transport.close()
            except Exception:
                pass
            self.tabs.removeTab(index)

    def open_settings(self):
        dlg = SettingsDialog(self.theme_mgr, parent=self)
        dlg.exec()

# -----------------------
# Entry point
# -----------------------
def main():
    app = QApplication(sys.argv)
    win = AppWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
