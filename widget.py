import sys
import threading
from datetime import datetime

import requests
from PyQt6.QtCore import Qt, QTimer, QObject, QRectF, pyqtSignal
from PyQt6.QtGui import (
    QBitmap, QPainter, QPen, QColor, QPalette,
    QFont, QTextCharFormat, QTextCursor,
)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QComboBox, QFrame, QMenu,
)

API     = "http://localhost:5000"
POLL_MS = 3000
W, H    = 720, 295
RADIUS  = 10

LANGUAGES = [
    ("English",    "en"),
    ("Russian",    "ru"),
    ("Spanish",    "es"),
    ("French",     "fr"),
    ("German",     "de"),
    ("Portuguese", "pt"),
    ("Italian",    "it"),
    ("Turkish",    "tr"),
    ("Japanese",   "ja"),
    ("Korean",     "ko"),
    ("Chinese",    "zh-CN"),
    ("Arabic",     "ar"),
    ("Hindi",      "hi"),
    ("Polish",     "pl"),
    ("Ukrainian",  "uk"),
    ("Dutch",      "nl"),
    ("Swedish",    "sv"),
    ("Norwegian",  "no"),
    ("Danish",     "da"),
    ("Finnish",    "fi"),
    ("Czech",      "cs"),
    ("Romanian",   "ro"),
    ("Hungarian",  "hu"),
]

STYLESHEET = """
* { font-family: 'Segoe UI'; }

/* ── Containers show through to window background ── */
QWidget  { background: transparent; color: #e2e8f0; }
QFrame   { background: transparent; }

/* ── URL input ── */
QLineEdit {
    background: #1e1e1e;
    color: #ffffff;
    border: 1px solid #333333;
    border-radius: 8px;
    padding: 7px 12px;
    font-size: 13px;
    selection-background-color: #1d4ed8;
}
QLineEdit:focus { border-color: #2563eb; background: #242424; }

/* ── Buttons ── */
QPushButton { border: none; border-radius: 8px; font-size: 12px; font-weight: 700; }

QPushButton#start {
    background: #0d1f14;
    color: #00ff88;
    border: 1px solid #00ff88;
    padding: 8px 20px;
    letter-spacing: 0.3px;
}
QPushButton#start:hover    { background: #122a1c; color: #33ffaa; border-color: #33ffaa; }
QPushButton#start:disabled { background: #0a1510; color: #1a4731; border-color: #1a3325; }

QPushButton#stop {
    background: #1f0d0d;
    color: #ff4444;
    border: 1px solid #ff4444;
    padding: 8px 20px;
}
QPushButton#stop:hover    { background: #2a1010; color: #ff6666; border-color: #ff6666; }
QPushButton#stop:disabled { background: #0f0a0a; color: #3a1a1a; border-color: #2a1212; }

QPushButton#paste {
    background: #1a1a1a;
    color: #888888;
    font-size: 15px;
    padding: 5px 9px;
    border: 1px solid #333333;
}
QPushButton#paste:hover { background: #222222; color: #bbbbbb; }

QPushButton#close {
    background: transparent;
    color: #3d4a5c;
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 4px;
}
QPushButton#close:hover { background: #dc2626; color: #ffffff; }

/* ── Language combo boxes ── */
QComboBox {
    background: #161616;
    color: #64b5f6;
    border: 1px solid #2a3a4a;
    border-radius: 8px;
    padding: 6px 10px;
    font-size: 12px;
    min-width: 115px;
}
QComboBox:hover { border-color: #3a5a7a; color: #90caf9; }
QComboBox::drop-down { border: none; width: 14px; }
QComboBox QAbstractItemView {
    background: #161616;
    color: #e5e7eb;
    selection-background-color: #1d4ed8;
    border: 1px solid #2a3a4a;
    outline: none;
    padding: 3px;
}
QComboBox QAbstractItemView::item { padding: 5px 8px; border-radius: 4px; }

/* ── Subtitle history area ── */
QTextEdit {
    background: #080808;
    color: #e2e8f0;
    border: none;
    border-radius: 6px;
    selection-background-color: #1d4ed8;
    padding: 4px;
}
QScrollBar:vertical {
    background: transparent;
    width: 4px;
    border-radius: 2px;
}
QScrollBar::handle:vertical {
    background: #2a2a2a;
    border-radius: 2px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover  { background: #3f3f3f; }
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical      { height: 0; }

/* ── Context menu ── */
QMenu {
    background: #161616;
    color: #e5e7eb;
    border: 1px solid #2a2a2a;
    padding: 4px;
}
QMenu::item           { padding: 6px 16px; border-radius: 5px; }
QMenu::item:selected  { background: #1d4ed8; }
QMenu::separator      { height: 1px; background: #252525; margin: 3px 0; }
"""


# ── Cross-thread signals ───────────────────────────────────────────────────────

class _Sig(QObject):
    translation = pyqtSignal(str, str)        # translation, original
    status      = pyqtSignal(bool, str, str, str)  # running, url, src, dest
    start_ok    = pyqtSignal()
    start_fail  = pyqtSignal()
    stop_done   = pyqtSignal()
    conn_error  = pyqtSignal()


# ── Widget ────────────────────────────────────────────────────────────────────

class TranslatorWidget(QWidget):
    def __init__(self):
        super().__init__()
        self._last_text = ""
        self._drag_pos  = None
        self._sig       = _Sig()

        self._sig.translation.connect(self._on_translation)
        self._sig.status.connect(self._on_status_restore)
        self._sig.start_ok.connect(self._on_start_ok)
        self._sig.start_fail.connect(self._on_start_fail)
        self._sig.stop_done.connect(lambda: self._apply_running(False))
        self._sig.conn_error.connect(lambda: self._set_accent("#dc2626"))

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setWindowOpacity(0.95)
        self.setFixedSize(W, H)
        self.setStyleSheet(STYLESHEET)

        # Solid dark background (painted by Qt before children)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor("#0d0d0d"))
        self.setPalette(pal)
        self.setAutoFillBackground(True)

        self._build_ui()
        self._position_window()

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(
            lambda: threading.Thread(target=self._fetch_latest, daemon=True).start()
        )
        threading.Thread(target=self._fetch_status, daemon=True).start()

    # ── Rounded mask ─────────────────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_mask()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_mask()

    def _apply_mask(self):
        bmp = QBitmap(self.size())
        bmp.fill(Qt.GlobalColor.color0)
        p = QPainter(bmp)
        p.setBrush(Qt.GlobalColor.color1)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(self.rect(), RADIUS, RADIUS)
        p.end()
        self.setMask(bmp)

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 2-px status accent at the very top
        self._accent_bar = QFrame()
        self._accent_bar.setFixedHeight(2)
        self._accent_bar.setStyleSheet("background: #1e293b;")
        root.addWidget(self._accent_bar)

        root.addWidget(self._make_titlebar())
        root.addWidget(self._sep("#181818"))
        root.addWidget(self._make_controls())
        root.addWidget(self._sep("#111111"))
        root.addWidget(self._make_subtitle_area(), stretch=1)

    @staticmethod
    def _sep(color: str) -> QFrame:
        f = QFrame()
        f.setFixedHeight(1)
        f.setStyleSheet(f"background: {color};")
        return f

    # ── Title bar ─────────────────────────────────────────────────────────────

    def _make_titlebar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(28)
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bar.setStyleSheet("background: #0d0d0d;")
        bar.setCursor(Qt.CursorShape.SizeAllCursor)

        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 0, 6, 0)
        lay.setSpacing(8)

        title = QLabel("ULTRA TRANSLATOR")
        title.setStyleSheet(
            "color: #1e293b; font-size: 8px; font-weight: bold;"
            " letter-spacing: 2px;"
        )
        lay.addWidget(title)

        self._platform_lbl = QLabel("")
        self._platform_lbl.setStyleSheet("font-size: 13px; color: #ffd700;")
        lay.addWidget(self._platform_lbl)

        lay.addStretch()

        self._dot = QLabel("●")
        self._dot.setStyleSheet("color: #1e293b; font-size: 10px;")
        lay.addWidget(self._dot)

        close_btn = QPushButton("✕")
        close_btn.setObjectName("close")
        close_btn.setFixedSize(26, 20)
        close_btn.clicked.connect(self.close)
        lay.addWidget(close_btn)

        bar.mousePressEvent = self._bar_press
        bar.mouseMoveEvent  = self._bar_move
        return bar

    # ── Controls ──────────────────────────────────────────────────────────────

    def _make_controls(self) -> QWidget:
        w = QWidget()
        w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        w.setStyleSheet("background: #0d0d0d;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(9)

        # ── URL row ──
        url_row = QHBoxLayout()
        url_row.setSpacing(6)

        self._url = QLineEdit()
        self._url.setPlaceholderText("Paste Twitch, YouTube Live, or Kick URL…")
        url_row.addWidget(self._url, stretch=1)

        paste_btn = QPushButton("📋")
        paste_btn.setObjectName("paste")
        paste_btn.setFixedSize(34, 34)
        paste_btn.setToolTip("Paste from clipboard")
        paste_btn.clicked.connect(self._do_paste)
        url_row.addWidget(paste_btn)

        self._btn_start = QPushButton("Start")
        self._btn_start.setObjectName("start")
        self._btn_start.setFixedHeight(34)
        self._btn_start.clicked.connect(self._on_start_click)
        url_row.addWidget(self._btn_start)

        self._btn_stop = QPushButton("Stop")
        self._btn_stop.setObjectName("stop")
        self._btn_stop.setFixedHeight(34)
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop_click)
        url_row.addWidget(self._btn_stop)

        lay.addLayout(url_row)

        # ── Language row ──
        lang_row = QHBoxLayout()
        lang_row.setSpacing(7)

        def _dim(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setStyleSheet("color: #374151; font-size: 11px;")
            return lbl

        lang_row.addWidget(_dim("Stream language"))

        self._src = QComboBox()
        self._src.setToolTip("The language the streamer speaks")
        for name, _ in LANGUAGES:
            self._src.addItem(name)
        self._src.setCurrentText("Spanish")
        lang_row.addWidget(self._src)

        arrow = QLabel("→")
        arrow.setStyleSheet("color: #2d3748; font-size: 15px;")
        lang_row.addWidget(arrow)

        lang_row.addWidget(_dim("Translate to"))

        self._dest = QComboBox()
        self._dest.setToolTip("Your language — what you want to read")
        for name, _ in LANGUAGES:
            self._dest.addItem(name)
        self._dest.setCurrentText("Russian")
        lang_row.addWidget(self._dest)

        lang_row.addStretch()
        lay.addLayout(lang_row)
        return w

    # ── Subtitle area ─────────────────────────────────────────────────────────

    def _make_subtitle_area(self) -> QWidget:
        w = QWidget()
        w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        w.setStyleSheet("background: #0d0d0d;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 8, 12, 12)
        lay.setSpacing(5)

        self._sub = QTextEdit()
        self._sub.setReadOnly(True)
        self._sub.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._sub.customContextMenuRequested.connect(self._sub_context_menu)
        lay.addWidget(self._sub, stretch=1)

        self._orig_lbl = QLabel("")
        self._orig_lbl.setStyleSheet(
            "color: #374151; font-size: 10px; font-style: italic;"
        )
        self._orig_lbl.setWordWrap(True)
        lay.addWidget(self._orig_lbl)
        return w

    # ── Drag ─────────────────────────────────────────────────────────────────

    def _bar_press(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def _bar_move(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    # ── Positioning ───────────────────────────────────────────────────────────

    def _position_window(self):
        screen = QApplication.primaryScreen().availableGeometry()
        self.move((screen.width() - W) // 2, screen.height() - H - 60)

    # ── Clipboard paste ───────────────────────────────────────────────────────

    def _do_paste(self):
        text = QApplication.clipboard().text().strip()
        if text:
            self._url.setText(text)

    # ── Accent / status helpers ───────────────────────────────────────────────

    def _set_accent(self, color: str):
        self._accent_bar.setStyleSheet(f"background: {color};")
        self._dot.setStyleSheet(f"color: {color}; font-size: 10px;")

    # ── Start / Stop ──────────────────────────────────────────────────────────

    def _on_start_click(self):
        url = self._url.text().strip()
        if not url:
            self._url.setFocus()
            return
        self._btn_start.setEnabled(False)
        src  = self._lang_code(self._src.currentText())
        dest = self._lang_code(self._dest.currentText())
        threading.Thread(
            target=self._post_start, args=(url, src, dest), daemon=True
        ).start()

    def _post_start(self, url: str, src: str, dest: str):
        try:
            resp = requests.post(
                f"{API}/start",
                json={"stream_url": url, "src_lang": src, "dest_lang": dest},
                timeout=5,
            )
            if resp.ok:
                self._sig.start_ok.emit()
                return
        except Exception as e:
            print(f"❌ Start error: {e}")
        self._sig.start_fail.emit()

    def _on_start_ok(self):
        self._apply_running(True)
        threading.Thread(target=self._fetch_latest, daemon=True).start()
        if not self._poll_timer.isActive():
            self._poll_timer.start(POLL_MS)

    def _on_start_fail(self):
        self._btn_start.setEnabled(True)
        self._set_accent("#dc2626")

    def _on_stop_click(self):
        self._btn_stop.setEnabled(False)
        threading.Thread(target=self._post_stop, daemon=True).start()

    def _post_stop(self):
        try:
            requests.post(f"{API}/stop", timeout=5)
        except Exception:
            pass
        self._sig.stop_done.emit()

    def _apply_running(self, on: bool):
        self._btn_start.setEnabled(not on)
        self._btn_stop.setEnabled(on)
        if on:
            self._platform_lbl.setText(self._platform_icon(self._url.text()))
            self._set_accent("#22c55e")
        else:
            self._platform_lbl.setText("")
            self._set_accent("#1e293b")
            self._poll_timer.stop()

    # ── Polling ───────────────────────────────────────────────────────────────

    def _fetch_latest(self):
        try:
            resp = requests.get(f"{API}/latest", timeout=3)
            if resp.ok:
                d = resp.json()
                self._sig.translation.emit(
                    d.get("translation", ""),
                    d.get("recognized_text", ""),
                )
                return
        except Exception:
            pass
        self._sig.conn_error.emit()

    def _on_translation(self, translation: str, original: str):
        if not translation or translation == self._last_text:
            return
        self._last_text = translation
        self._append_entry(translation)
        self._orig_lbl.setText(f"({original})" if original else "")
        self._set_accent("#22c55e")

    def _append_entry(self, translation: str):
        cursor = self._sub.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        if not self._sub.document().isEmpty():
            gap = QTextCharFormat()
            gap.setFontPointSize(4)
            cursor.insertText("\n", gap)

        ts_fmt = QTextCharFormat()
        ts_fmt.setFontPointSize(9)
        ts_fmt.setForeground(QColor("#374151"))
        cursor.insertText(datetime.now().strftime("%H:%M") + "\n", ts_fmt)

        tx_fmt = QTextCharFormat()
        tx_fmt.setFontPointSize(16)
        tx_fmt.setFontWeight(QFont.Weight.Bold)
        tx_fmt.setForeground(QColor("#f1f5f9"))
        cursor.insertText(translation + "\n", tx_fmt)

        self._sub.setTextCursor(cursor)
        self._sub.ensureCursorVisible()

    # ── Subtitle context menu ─────────────────────────────────────────────────

    def _sub_context_menu(self, pos):
        menu = QMenu(self)
        copy_sel = menu.addAction("Copy selection")
        copy_all = menu.addAction("Copy all")
        action = menu.exec(self._sub.mapToGlobal(pos))
        if action == copy_sel:
            self._sub.copy()
        elif action == copy_all:
            text = self._sub.toPlainText().strip()
            if text:
                QApplication.clipboard().setText(text)

    # ── Startup sync ──────────────────────────────────────────────────────────

    def _fetch_status(self):
        try:
            resp = requests.get(f"{API}/status", timeout=3)
            if resp.ok:
                s = resp.json()
                self._sig.status.emit(
                    s.get("is_translating", False),
                    s.get("stream_url", ""),
                    s.get("src_lang", "es"),
                    s.get("dest_lang", "ru"),
                )
        except Exception:
            pass

    def _on_status_restore(self, running: bool, url: str, src: str, dest: str):
        if url:
            self._url.setText(url)
        src_name  = self._lang_name(src)
        dest_name = self._lang_name(dest)
        if src_name:
            self._src.setCurrentText(src_name)
        if dest_name:
            self._dest.setCurrentText(dest_name)
        if running:
            self._apply_running(True)
            threading.Thread(target=self._fetch_latest, daemon=True).start()
            self._poll_timer.start(POLL_MS)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _lang_code(name: str) -> str:
        return next((c for n, c in LANGUAGES if n == name), "en")

    @staticmethod
    def _lang_name(code: str) -> str:
        return next((n for n, c in LANGUAGES if c == code.lower()), "")

    @staticmethod
    def _platform_icon(url: str) -> str:
        if "twitch.tv" in url:  return "🎮"
        if "youtube.com" in url or "youtu.be" in url: return "▶️"
        if "kick.com" in url:   return "🟢"
        return ""


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = TranslatorWidget()
    win.show()
    sys.exit(app.exec())