"""
watch_window.py — Embedded stream player + live subtitle overlay + Twitch OAuth.

Requires:
    pip install PyQt6 PyQt6-WebEngine requests
"""

import json
import os
import re
import sys
import threading
from pathlib import Path

_SETTINGS_FILE = Path(__file__).parent / "watch_settings.json"

# ── Replace with your real Twitch Client ID ───────────────────────────────────
TWITCH_CLIENT_ID = "haofrfzyxtscxep60sfg9hek15ueh9"
# ─────────────────────────────────────────────────────────────────────────────

# Must be set before QApplication is created
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--enable-gpu-rasterization --enable-zero-copy --ignore-gpu-blocklist",
)

import requests
from PyQt6.QtCore import Qt, QTimer, QObject, QUrl, pyqtSignal
from PyQt6.QtNetwork import QNetworkCookie
from PyQt6.QtWidgets import (
    QApplication, QDialog, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QFrame, QSizePolicy,
    QListWidget, QListWidgetItem,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings, QWebEnginePage

API     = "http://localhost:5000"
POLL_MS = 3000

LANGUAGES = [
    ("English",    "en"),  ("Russian",    "ru"),  ("Spanish",    "es"),
    ("French",     "fr"),  ("German",     "de"),  ("Portuguese", "pt"),
    ("Italian",    "it"),  ("Turkish",    "tr"),  ("Japanese",   "ja"),
    ("Korean",     "ko"),  ("Chinese",    "zh-CN"), ("Arabic",   "ar"),
    ("Hindi",      "hi"),  ("Polish",     "pl"),  ("Ukrainian",  "uk"),
    ("Dutch",      "nl"),  ("Swedish",    "sv"),  ("Norwegian",  "no"),
    ("Danish",     "da"),  ("Finnish",    "fi"),  ("Czech",      "cs"),
    ("Romanian",   "ro"),  ("Hungarian",  "hu"),  ("Bulgarian",  "bg"),
]


# ── Twitch OAuth helpers ──────────────────────────────────────────────────────

class _AuthPage(QWebEnginePage):
    """
    Custom page that intercepts Twitch's redirect to http://localhost and
    extracts the access_token from the URL fragment without making a real
    network request to localhost.
    """
    token_found = pyqtSignal(str)

    def acceptNavigationRequest(
        self, url: QUrl, nav_type: QWebEnginePage.NavigationType, is_main_frame: bool
    ) -> bool:
        if url.scheme() == "http" and url.host() == "localhost":
            for part in url.fragment().split("&"):
                if part.startswith("access_token="):
                    self.token_found.emit(part[len("access_token="):])
                    break
            return False  # Block the actual navigation to localhost
        return True


class TwitchAuthDialog(QDialog):
    """Modal Twitch login dialog. Emits token_received when OAuth succeeds."""
    token_received = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Login with Twitch")
        self.setMinimumSize(920, 700)

        self._view = QWebEngineView()
        self._page = _AuthPage(self._view)
        self._page.token_found.connect(self._on_token)
        self._view.setPage(self._page)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._view)

        auth_url = (
            f"https://id.twitch.tv/oauth2/authorize"
            f"?client_id={TWITCH_CLIENT_ID}"
            f"&redirect_uri=http://localhost"
            f"&response_type=token"
            f"&scope=user:read:follows+chat:read+chat:edit"
            f"&force_verify=false"
        )
        self._view.load(QUrl(auth_url))

    def _on_token(self, token: str):
        self.token_received.emit(token)
        self.accept()


# ── Cross-thread signals ──────────────────────────────────────────────────────

class _Sig(QObject):
    translation    = pyqtSignal(str, str)
    status_restore = pyqtSignal(bool, str, str, str, str)
    start_ok       = pyqtSignal()
    start_fail     = pyqtSignal()
    stop_done      = pyqtSignal()
    user_ready     = pyqtSignal(str, str)   # user_id, display_name
    channels_ready = pyqtSignal(list)       # list of Helix stream dicts
    twitch_error   = pyqtSignal(str)        # error message


# ── Main window ───────────────────────────────────────────────────────────────

class WatchWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SubFlow — Watch")
        self.setStyleSheet("background: #0a0a0a; color: #ffffff;")

        self._sig = _Sig()
        self._last_text: str      = ""
        self._scheduled: set[str] = set()
        self._selected_quality    = "480p"

        # Load all persisted settings at once
        saved = self._load_settings()
        self._subtitle_delay: int = saved["subtitle_delay"]
        self._twitch_token: str   = saved["twitch_token"]
        self._twitch_user_id: str = saved["twitch_user_id"]
        self._twitch_name: str    = saved["twitch_name"]

        # Timers
        self._preload_timer = QTimer(self)
        self._preload_timer.setSingleShot(True)
        self._preload_timer.timeout.connect(self._do_preload)

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(
            lambda: threading.Thread(target=self._fetch_latest, daemon=True).start()
        )

        # Signal wiring
        self._sig.translation.connect(self._on_translation)
        self._sig.status_restore.connect(self._on_status_restore)
        self._sig.start_ok.connect(self._on_start_ok)
        self._sig.start_fail.connect(self._on_start_fail)
        self._sig.stop_done.connect(lambda: self._apply_running(False))
        self._sig.user_ready.connect(self._on_user_ready)
        self._sig.channels_ready.connect(self._on_channels_ready)
        self._sig.twitch_error.connect(self._on_twitch_error)

        self._build_ui()
        self._configure_webview()
        self._center_on_screen()

        # Restore Twitch session if we have a saved token
        if self._twitch_token:
            self._refresh_channels()

        threading.Thread(target=self._fetch_status, daemon=True).start()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._make_topbar())
        root.addWidget(self._hsep())

        # Exactly one of these two panels is visible at a time
        self._login_banner   = self._make_login_banner()
        self._channels_panel = self._make_channels_panel()
        root.addWidget(self._login_banner)
        root.addWidget(self._channels_panel)

        root.addWidget(self._hsep())

        self._webview = QWebEngineView()
        self._webview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        root.addWidget(self._webview, stretch=1)

        root.addWidget(self._hsep())
        root.addWidget(self._make_subtitle_bar())

        self._set_logged_in(bool(self._twitch_token))

    @staticmethod
    def _hsep() -> QFrame:
        f = QFrame()
        f.setFixedHeight(1)
        f.setStyleSheet("background: #1a1a1a;")
        return f

    # ── Top bar (URL / quality / start-stop / languages) ─────────────────────

    def _make_topbar(self) -> QWidget:
        container = QWidget()
        container.setStyleSheet("background: #0d0d0d;")

        outer = QVBoxLayout(container)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(7)

        # Row 1 — URL + quality + Start/Stop
        row1 = QHBoxLayout()
        row1.setSpacing(7)

        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("Twitch, YouTube Live, or Kick URL…")
        self._url_input.setStyleSheet("""
            QLineEdit {
                background: #161616; color: #fff;
                border: 1px solid #2a2a2a; border-radius: 6px;
                padding: 5px 10px; font-size: 13px;
            }
            QLineEdit:focus { border-color: #2563eb; }
        """)
        self._url_input.textChanged.connect(self._on_url_changed)
        row1.addWidget(self._url_input, stretch=1)

        self._quality_btns: dict[str, QPushButton] = {}
        for label, key in [("480p", "480p"), ("720p", "720p"), ("Auto", "auto")]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(key == self._selected_quality)
            btn.setFixedSize(52, 30)
            btn.setStyleSheet("""
                QPushButton {
                    background: #161616; color: #555;
                    border: 1px solid #2a2a2a; border-radius: 6px;
                    font-size: 12px; font-weight: 700;
                }
                QPushButton:checked { background: #2563eb; color: #fff; border-color: #2563eb; }
                QPushButton:hover:!checked { color: #999; border-color: #3a3a3a; }
            """)
            btn.clicked.connect(lambda _, k=key: self._set_quality(k))
            self._quality_btns[key] = btn
            row1.addWidget(btn)

        self._btn_start = QPushButton("▶  Start")
        self._btn_start.setFixedSize(84, 30)
        self._btn_start.setStyleSheet(
            "background: #15803d; color: #fff; border: none;"
            " border-radius: 6px; font-size: 12px; font-weight: 700;"
        )
        self._btn_start.clicked.connect(self._on_start)
        row1.addWidget(self._btn_start)

        self._btn_stop = QPushButton("■  Stop")
        self._btn_stop.setFixedSize(84, 30)
        self._btn_stop.setEnabled(False)
        self._btn_stop.setStyleSheet("""
            QPushButton          { background: #991b1b; color: #fff; border: none;
                                   border-radius: 6px; font-size: 12px; font-weight: 700; }
            QPushButton:disabled { background: #1f0a0a; color: #3a1212; }
        """)
        self._btn_stop.clicked.connect(self._on_stop)
        row1.addWidget(self._btn_stop)
        outer.addLayout(row1)

        # Row 2 — language selectors
        row2 = QHBoxLayout()
        row2.setSpacing(8)

        def _dim(t: str) -> QLabel:
            lbl = QLabel(t)
            lbl.setStyleSheet("color: #333; font-size: 11px;")
            return lbl

        row2.addWidget(_dim("From"))
        self._src = self._lang_combo("Spanish")
        row2.addWidget(self._src)
        row2.addWidget(_dim("→"))
        row2.addWidget(_dim("To"))
        self._dest = self._lang_combo("Russian")
        row2.addWidget(self._dest)
        row2.addStretch()
        outer.addLayout(row2)

        return container

    def _lang_combo(self, default: str) -> QComboBox:
        cb = QComboBox()
        cb.setFixedHeight(26)
        cb.setStyleSheet("""
            QComboBox {
                background: #161616; color: #64b5f6;
                border: 1px solid #2a3a4a; border-radius: 6px;
                padding: 2px 8px; font-size: 12px; min-width: 110px;
            }
            QComboBox::drop-down { border: none; width: 14px; }
            QComboBox QAbstractItemView {
                background: #161616; color: #e5e7eb;
                selection-background-color: #1d4ed8;
                border: 1px solid #2a3a4a;
            }
        """)
        for name, _ in LANGUAGES:
            cb.addItem(name)
        cb.setCurrentText(default)
        return cb

    # ── Login banner (shown when logged out) ──────────────────────────────────

    def _make_login_banner(self) -> QWidget:
        banner = QWidget()
        banner.setStyleSheet("background: #0d0d0d;")
        banner.setFixedHeight(46)

        lay = QHBoxLayout(banner)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(12)

        lbl = QLabel("See your live followed channels:")
        lbl.setStyleSheet("color: #2d2d2d; font-size: 12px;")
        lay.addWidget(lbl)

        self._btn_login = QPushButton("🟣  Login with Twitch")
        self._btn_login.setFixedHeight(30)
        self._btn_login.setStyleSheet("""
            QPushButton       { background: #6441a5; color: #fff; border: none;
                                border-radius: 6px; font-size: 12px; font-weight: 700;
                                padding: 0 18px; }
            QPushButton:hover { background: #7d5bbe; }
        """)
        self._btn_login.clicked.connect(self._on_login_click)
        lay.addWidget(self._btn_login)
        lay.addStretch()
        return banner

    # ── Channels panel (shown when logged in) ─────────────────────────────────

    def _make_channels_panel(self) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet("background: #0d0d0d;")
        panel.setFixedHeight(172)

        outer = QVBoxLayout(panel)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(6)

        # Header row
        hdr = QHBoxLayout()

        self._ch_status_lbl = QLabel("Live channels")
        self._ch_status_lbl.setStyleSheet(
            "color: #3a3a3a; font-size: 11px; font-weight: 600;"
        )
        hdr.addWidget(self._ch_status_lbl)
        hdr.addStretch()

        btn_refresh = QPushButton("↺  Refresh")
        btn_refresh.setFixedSize(76, 24)
        btn_refresh.setStyleSheet(self._small_btn_style())
        btn_refresh.clicked.connect(self._refresh_channels)
        hdr.addWidget(btn_refresh)

        btn_logout = QPushButton("Log out")
        btn_logout.setFixedSize(60, 24)
        btn_logout.setStyleSheet(
            self._small_btn_style() +
            " QPushButton:hover { color: #ef4444; border-color: #ef4444; }"
        )
        btn_logout.clicked.connect(self._on_logout)
        hdr.addWidget(btn_logout)

        outer.addLayout(hdr)

        # Scrollable channel list — keyboard-navigable for TV remote
        self._channels_list = QListWidget()
        self._channels_list.setStyleSheet("""
            QListWidget {
                background: #0a0a0a; border: 1px solid #1e1e1e;
                border-radius: 6px; color: #ccc;
                font-size: 13px; outline: none;
            }
            QListWidget::item          { padding: 6px 10px; border-radius: 4px; }
            QListWidget::item:selected { background: #2563eb; color: #fff; }
            QListWidget::item:hover:!selected { background: #161616; }
        """)
        self._channels_list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._channels_list.itemActivated.connect(self._on_channel_activated)
        outer.addWidget(self._channels_list)

        return panel

    # ── Subtitle bar ──────────────────────────────────────────────────────────

    def _make_subtitle_bar(self) -> QWidget:
        bar = QWidget()
        bar.setStyleSheet("background: #0d0d0d;")
        bar.setFixedHeight(80)

        outer = QVBoxLayout(bar)
        outer.setContentsMargins(16, 10, 16, 8)
        outer.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(10)

        self._sub_lbl = QLabel("Waiting for translation…")
        self._sub_lbl.setStyleSheet("color: #1e293b; font-size: 20px; font-weight: 700;")
        self._sub_lbl.setWordWrap(True)
        top.addWidget(self._sub_lbl, stretch=1)

        adj = (
            "QPushButton { background: #161616; color: #555;"
            " border: 1px solid #252525; border-radius: 6px;"
            " font-size: 11px; font-weight: 700; padding: 0 6px; }"
            " QPushButton:hover { color: #aaa; border-color: #3a3a3a; }"
            " QPushButton:pressed { background: #1e1e1e; }"
        )

        btn_minus = QPushButton("- 1s")
        btn_minus.setFixedSize(46, 26)
        btn_minus.setStyleSheet(adj)
        btn_minus.clicked.connect(self._delay_minus)
        top.addWidget(btn_minus)

        self._delay_val_lbl = QLabel(f"Delay: {self._subtitle_delay}s")
        self._delay_val_lbl.setStyleSheet("color: #3a3a3a; font-size: 12px; font-weight: 600;")
        self._delay_val_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._delay_val_lbl.setFixedWidth(72)
        top.addWidget(self._delay_val_lbl)

        btn_plus = QPushButton("+ 1s")
        btn_plus.setFixedSize(46, 26)
        btn_plus.setStyleSheet(adj)
        btn_plus.clicked.connect(self._delay_plus)
        top.addWidget(btn_plus)

        outer.addLayout(top)

        self._orig_lbl = QLabel("")
        self._orig_lbl.setStyleSheet("color: #2a2a2a; font-size: 11px; font-style: italic;")
        outer.addWidget(self._orig_lbl)

        return bar

    @staticmethod
    def _small_btn_style() -> str:
        return (
            "QPushButton { background: #161616; color: #444;"
            " border: 1px solid #252525; border-radius: 5px; font-size: 11px; }"
            " QPushButton:hover { color: #888; border-color: #3a3a3a; }"
        )

    # ── WebView configuration ─────────────────────────────────────────────────

    def _configure_webview(self):
        s = self._webview.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)

        # Inject saved token as cookie so Twitch chat is already authenticated
        if self._twitch_token:
            self._inject_twitch_auth(self._twitch_token)

    def _center_on_screen(self):
        screen = QApplication.primaryScreen().availableGeometry()
        w = min(1280, screen.width())
        h = min(820, screen.height())
        self.resize(w, h)
        self.move((screen.width() - w) // 2, (screen.height() - h) // 2)

    # ── Login state toggle ────────────────────────────────────────────────────

    def _set_logged_in(self, logged_in: bool):
        self._login_banner.setVisible(not logged_in)
        self._channels_panel.setVisible(logged_in)

    # ── OAuth flow ────────────────────────────────────────────────────────────

    def _on_login_click(self):
        dlg = TwitchAuthDialog(self)
        dlg.token_received.connect(self._on_auth_done)
        dlg.exec()

    def _on_auth_done(self, token: str):
        self._twitch_token = token
        self._set_logged_in(True)
        self._ch_status_lbl.setText("Fetching channels…")
        self._inject_twitch_auth(token)
        threading.Thread(
            target=self._bg_fetch_user_and_channels, args=(token,), daemon=True
        ).start()

    def _bg_fetch_user_and_channels(self, token: str):
        """Background: resolve user ID then fetch live followed streams."""
        hdrs = {"Authorization": f"Bearer {token}", "Client-Id": TWITCH_CLIENT_ID}
        try:
            # 1 — Who is logged in?
            r = requests.get("https://api.twitch.tv/helix/users", headers=hdrs, timeout=10)
            if r.status_code == 401:
                self._sig.twitch_error.emit("expired")
                return
            r.raise_for_status()
            users = r.json().get("data", [])
            if not users:
                self._sig.twitch_error.emit("Could not fetch Twitch user info.")
                return
            user_id   = users[0]["id"]
            user_name = users[0]["display_name"]
            self._sig.user_ready.emit(user_id, user_name)

            # 2 — Which followed channels are live right now?
            r = requests.get(
                "https://api.twitch.tv/helix/streams/followed",
                params={"user_id": user_id, "first": 100},
                headers=hdrs,
                timeout=10,
            )
            r.raise_for_status()
            self._sig.channels_ready.emit(r.json().get("data", []))

        except requests.RequestException as exc:
            self._sig.twitch_error.emit(f"Network error: {exc}")

    def _on_user_ready(self, user_id: str, display_name: str):
        self._twitch_user_id = user_id
        self._twitch_name    = display_name
        self._save_settings()

    def _on_channels_ready(self, streams: list):
        self._populate_channels(streams)

    def _on_twitch_error(self, msg: str):
        if msg == "expired" or "401" in msg:
            # Token is dead — clear it and return to logged-out state
            self._twitch_token = self._twitch_user_id = self._twitch_name = ""
            self._save_settings()
            self._set_logged_in(False)
        else:
            self._ch_status_lbl.setText(msg)

    def _refresh_channels(self):
        if not self._twitch_token:
            return
        self._ch_status_lbl.setText("Refreshing…")
        self._channels_list.clear()
        threading.Thread(
            target=self._bg_fetch_user_and_channels,
            args=(self._twitch_token,),
            daemon=True,
        ).start()

    def _on_logout(self):
        self._twitch_token = self._twitch_user_id = self._twitch_name = ""
        self._save_settings()
        self._channels_list.clear()
        self._set_logged_in(False)

    # ── Channel list ──────────────────────────────────────────────────────────

    @staticmethod
    def _fmt_viewers(n: int) -> str:
        return f"{n / 1000:.1f}K" if n >= 1000 else str(n)

    def _populate_channels(self, streams: list):
        self._channels_list.clear()
        if not streams:
            self._ch_status_lbl.setText("No followed channels are live right now.")
            return

        n = len(streams)
        self._ch_status_lbl.setText(f"🟢 {n} channel{'s' if n != 1 else ''} live")

        for s in streams:
            name    = s.get("user_name", "?")
            login   = s.get("user_login", name.lower())
            game    = s.get("game_name", "")
            viewers = self._fmt_viewers(s.get("viewer_count", 0))
            text    = f"▶  {name}   ·   {game}   ·   {viewers} viewers"

            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, f"https://twitch.tv/{login}")
            self._channels_list.addItem(item)

        # Auto-select first entry so arrow keys + Enter work immediately (TV remote)
        self._channels_list.setCurrentRow(0)
        self._channels_list.setFocus()

    def _on_channel_activated(self, item: QListWidgetItem):
        """User pressed Enter or double-clicked a channel — load it."""
        url = item.data(Qt.ItemDataRole.UserRole)
        if not url:
            return
        self._url_input.blockSignals(True)
        self._url_input.setText(url)
        self._url_input.blockSignals(False)
        embed = self._embed_url(url, autoplay=True)
        if embed:
            self._webview.load(QUrl(embed))

    # ── Chat auth injection ───────────────────────────────────────────────────

    def _inject_twitch_auth(self, token: str):
        """
        Write the auth-token cookie into the WebEngine profile so that the
        Twitch embedded chat recognises the logged-in user and allows sending
        messages without a separate login step.
        """
        store = self._webview.page().profile().cookieStore()
        c = QNetworkCookie(b"auth-token", token.encode())
        c.setDomain(".twitch.tv")
        c.setPath("/")
        c.setSecure(True)
        store.setCookie(c, QUrl("https://www.twitch.tv"))

    # ── Quality ────────────────────────────────────────────────────────────────

    def _set_quality(self, key: str):
        self._selected_quality = key
        for k, btn in self._quality_btns.items():
            btn.setChecked(k == key)

    # ── URL pre-loading ───────────────────────────────────────────────────────

    def _on_url_changed(self, text: str):
        self._preload_timer.stop()
        if text.strip():
            self._preload_timer.start(800)

    def _do_preload(self):
        if self._btn_stop.isEnabled():  # stream already running — don't override
            return
        url = self._url_input.text().strip()
        embed = self._embed_url(url, autoplay=False)
        if embed:
            self._webview.load(QUrl(embed))

    # ── Embed URL builder ──────────────────────────────────────────────────────

    @staticmethod
    def _embed_url(url: str, autoplay: bool = True) -> str | None:
        auto = "true" if autoplay else "false"

        # Twitch — no quality param, Twitch picks best available
        m = re.search(r'twitch\.tv/([^/?#\s]+)', url)
        if m:
            ch = m.group(1)
            if ch not in {"videos", "directory", "settings", "login", "signup"}:
                return (
                    f"https://player.twitch.tv/?channel={ch}"
                    f"&parent=player.twitch.tv&autoplay={auto}&muted=false"
                )

        # YouTube watch / live / youtu.be
        m = re.search(
            r'(?:youtube\.com/(?:watch\?v=|live/)|youtu\.be/)([A-Za-z0-9_-]{11})', url
        )
        if m:
            return (
                f"https://www.youtube.com/embed/{m.group(1)}"
                f"?autoplay={1 if autoplay else 0}&rel=0"
            )

        # Kick
        m = re.search(r'kick\.com/([^/?#\s]+)', url)
        if m:
            return f"https://player.kick.com/{m.group(1)}?autoplay={auto}"

        return None

    # ── Start / Stop ───────────────────────────────────────────────────────────

    def _on_start(self):
        url = self._url_input.text().strip()
        if not url:
            self._url_input.setFocus()
            return
        self._btn_start.setEnabled(False)

        embed = self._embed_url(url, autoplay=True)
        if embed:
            self._webview.load(QUrl(embed))

        src  = self._lang_code(self._src.currentText())
        dest = self._lang_code(self._dest.currentText())
        threading.Thread(
            target=self._post_start,
            args=(url, src, dest, self._selected_quality),
            daemon=True,
        ).start()

    def _post_start(self, url: str, src: str, dest: str, quality: str):
        try:
            r = requests.post(
                f"{API}/start",
                json={"stream_url": url, "src_lang": src,
                      "dest_lang": dest, "quality": quality},
                timeout=5,
            )
            if r.ok:
                self._sig.start_ok.emit()
                return
        except Exception as exc:
            print(f"Start error: {exc}")
        self._sig.start_fail.emit()

    def _on_start_ok(self):
        self._apply_running(True)
        threading.Thread(target=self._fetch_latest, daemon=True).start()
        if not self._poll_timer.isActive():
            self._poll_timer.start(POLL_MS)

    def _on_start_fail(self):
        self._btn_start.setEnabled(True)

    def _on_stop(self):
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
        if not on:
            self._poll_timer.stop()

    # ── Translation polling ────────────────────────────────────────────────────

    def _fetch_latest(self):
        try:
            r = requests.get(f"{API}/latest", timeout=3)
            if r.ok:
                d = r.json()
                self._sig.translation.emit(
                    d.get("translation", ""), d.get("recognized_text", "")
                )
        except Exception:
            pass

    def _on_translation(self, text: str, original: str):
        text = text.strip()
        if not text or text == self._last_text or text in self._scheduled:
            return
        self._scheduled.add(text)

        def _show():
            self._scheduled.discard(text)
            self._last_text = text
            self._sub_lbl.setStyleSheet("color: #ffffff; font-size: 20px; font-weight: 700;")
            self._sub_lbl.setText(text)
            orig = original.strip()
            self._orig_lbl.setText(f"({orig})" if orig else "")

        QTimer.singleShot(self._subtitle_delay * 1000, _show)

    # ── Subtitle delay ─────────────────────────────────────────────────────────

    def _delay_minus(self):
        if self._subtitle_delay > 0:
            self._subtitle_delay -= 1
            self._delay_val_lbl.setText(f"Delay: {self._subtitle_delay}s")
            self._save_settings()

    def _delay_plus(self):
        if self._subtitle_delay < 10:
            self._subtitle_delay += 1
            self._delay_val_lbl.setText(f"Delay: {self._subtitle_delay}s")
            self._save_settings()

    # ── Settings persistence ───────────────────────────────────────────────────

    @staticmethod
    def _load_settings() -> dict:
        try:
            data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
            delay = max(0, min(10, int(data.get("subtitle_delay", 3))))
            return {
                "subtitle_delay": delay,
                "twitch_token":   str(data.get("twitch_token",   "") or ""),
                "twitch_user_id": str(data.get("twitch_user_id", "") or ""),
                "twitch_name":    str(data.get("twitch_name",    "") or ""),
            }
        except Exception:
            return {"subtitle_delay": 3, "twitch_token": "",
                    "twitch_user_id": "", "twitch_name": ""}

    def _save_settings(self):
        try:
            _SETTINGS_FILE.write_text(
                json.dumps({
                    "subtitle_delay": self._subtitle_delay,
                    "twitch_token":   self._twitch_token,
                    "twitch_user_id": self._twitch_user_id,
                    "twitch_name":    self._twitch_name,
                }, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ── Flask backend status restore ──────────────────────────────────────────

    def _fetch_status(self):
        try:
            r = requests.get(f"{API}/status", timeout=3)
            if r.ok:
                s = r.json()
                self._sig.status_restore.emit(
                    s.get("is_translating", False),
                    s.get("stream_url", ""),
                    s.get("src_lang", "es"),
                    s.get("dest_lang", "ru"),
                    s.get("quality", "480p"),
                )
        except Exception:
            pass

    def _on_status_restore(
        self, running: bool, url: str, src: str, dest: str, quality: str
    ):
        self._url_input.blockSignals(True)
        if url:
            self._url_input.setText(url)
        self._url_input.blockSignals(False)

        if sn := self._lang_name(src):
            self._src.setCurrentText(sn)
        if dn := self._lang_name(dest):
            self._dest.setCurrentText(dn)

        self._set_quality(quality if quality in self._quality_btns else "480p")

        if running:
            self._apply_running(True)
            self._poll_timer.start(POLL_MS)
            embed = self._embed_url(url, autoplay=True)
        elif url:
            embed = self._embed_url(url, autoplay=False)
        else:
            embed = None

        if embed:
            self._webview.load(QUrl(embed))

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _lang_code(name: str) -> str:
        return next((c for n, c in LANGUAGES if n == name), "en")

    @staticmethod
    def _lang_name(code: str) -> str:
        return next((n for n, c in LANGUAGES if c == code.lower()), "")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = WatchWindow()
    win.show()
    sys.exit(app.exec())
