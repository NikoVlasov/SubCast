"""SubCast Server — GUI launcher.

Wraps the Flask server (app.py) with a desktop status window so users
can enter API keys, start/stop the server, and monitor activity — without
needing a terminal or Python installation (when distributed as an exe).
"""
from __future__ import annotations

import sys
import os
import socket
import threading
import queue
import logging
import tkinter as tk
from tkinter import scrolledtext

# ── Frozen-exe path setup ─────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    _MEIPASS = sys._MEIPASS  # type: ignore[attr-defined]
    # Directory that holds the exe (writable; .env lives here)
    APP_DIR = os.path.dirname(sys.executable)
    # Let 'import app' find the bundled app.py
    sys.path.insert(0, _MEIPASS)
    # Let subprocess calls find bundled ffmpeg
    os.environ["PATH"] = _MEIPASS + os.pathsep + os.environ.get("PATH", "")
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

ENV_PATH = os.path.join(APP_DIR, ".env")

# Pre-set placeholder keys so app.py module-level client init doesn't raise
# before the user's real keys are loaded from .env.
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder")
os.environ.setdefault("GROQ_API_KEY", "gsk-placeholder")

# Load saved keys from .env (overrides placeholders)
from dotenv import load_dotenv, dotenv_values, set_key
load_dotenv(ENV_PATH, override=True)

# Import server module now — all Python deps are bundled; clients stay lazy
import app as _server_module  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(("10.254.254.254", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        try:
            s.close()
        except Exception:
            pass


# ── Log capture ───────────────────────────────────────────────────────────────

class _QueueStream:
    """Redirect sys.stdout / sys.stderr writes into a queue."""

    def __init__(self, q: queue.Queue, orig):
        self._q    = q
        self._orig = orig

    def write(self, text: str) -> None:
        if text and text.strip():
            self._q.put(text)

    def flush(self) -> None:
        pass

    def fileno(self) -> int:
        try:
            return self._orig.fileno()
        except Exception:
            return -1


class _QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue) -> None:
        super().__init__()
        self._q = q

    def emit(self, record: logging.LogRecord) -> None:
        self._q.put(self.format(record))


# ── Flask server wrapper ───────────────────────────────────────────────────────

class _FlaskServer:
    def __init__(self, log_queue: queue.Queue) -> None:
        self._server = None
        self._lock   = threading.Lock()
        self._log_q  = log_queue

    def start(self) -> bool:
        with self._lock:
            if self._server is not None:
                return False
            try:
                from werkzeug.serving import make_server
                self._server = make_server("0.0.0.0", 5000, _server_module.app)
                threading.Thread(
                    target=self._server.serve_forever,
                    daemon=True,
                    name="subcast-http",
                ).start()
                self._log_q.put("🚀 HTTP server listening on 0.0.0.0:5000\n")
                return True
            except Exception as e:
                self._log_q.put(f"❌ Failed to start server: {e}\n")
                self._server = None
                return False

    def stop(self) -> None:
        with self._lock:
            srv = self._server
            self._server = None
        if srv is None:
            return
        # Stop active translation first
        try:
            import urllib.request
            urllib.request.urlopen(
                "http://127.0.0.1:5000/stop", data=b"", timeout=2
            )
        except Exception:
            pass
        # Shut down HTTP server in background (blocking call)
        threading.Thread(target=srv.shutdown, daemon=True).start()
        self._log_q.put("■ Server stopped\n")

    @property
    def running(self) -> bool:
        return self._server is not None


# ── Theme constants ───────────────────────────────────────────────────────────

DARK   = "#0f0f1a"
CARD   = "#1a1a2e"
ACCENT = "#4a90d9"
GREEN  = "#4CAF50"
RED    = "#e74c3c"
FG     = "#d0d0e0"
FG_DIM = "#888899"
MONO   = ("Consolas", 9)
BOLD   = ("Segoe UI", 10, "bold")


# ── Main window ───────────────────────────────────────────────────────────────

class SubCastApp(tk.Tk):

    def __init__(self) -> None:
        super().__init__()
        self.title("SubCast Server")
        self.geometry("740x620")
        self.minsize(640, 500)
        self.configure(bg=DARK)

        # ── Class-level Entry bindings ────────────────────────────────────────
        # bind_class replaces the Entry class binding, which means it fires
        # before the built-in <<Paste>> virtual event can swallow the keystroke.
        # Per-widget bind() fires AFTER the class binding — too late on Windows.
        self.bind_class("Entry", "<Control-v>", self._on_entry_paste)
        self.bind_class("Entry", "<Control-V>", self._on_entry_paste)
        self.bind_class("Entry", "<Control-a>", self._on_entry_select_all)
        self.bind_class("Entry", "<Control-A>", self._on_entry_select_all)

        # Shared right-click menu; _ctx_entry tracks whichever field was clicked
        self._ctx_entry: tk.Entry | None = None
        self._ctx_menu = tk.Menu(
            self, tearoff=0,
            bg="#1e1e32", fg=FG,
            activebackground=ACCENT, activeforeground="white",
            bd=0, relief="flat",
        )
        self._ctx_menu.add_command(label="Paste",      command=self._ctx_paste)
        self._ctx_menu.add_command(label="Select All", command=self._ctx_select_all)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="Clear",      command=self._ctx_clear)

        self._log_queue = queue.Queue()
        self._flask     = _FlaskServer(self._log_queue)
        self._local_ip  = get_local_ip()

        # Redirect stdout / stderr to log queue
        orig_out = sys.stdout
        orig_err = sys.stderr
        sys.stdout = _QueueStream(self._log_queue, orig_out)
        sys.stderr = _QueueStream(self._log_queue, orig_err)

        # Capture werkzeug / library loggers
        _handler = _QueueLogHandler(self._log_queue)
        _handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        for name in ("werkzeug", "openai", "groq", "httpx"):
            lg = logging.getLogger(name)
            lg.addHandler(_handler)
            lg.setLevel(logging.INFO)

        self._build_ui()
        self._poll_log()

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Header
        hdr = tk.Frame(self, bg=CARD, pady=14)
        hdr.pack(fill="x")
        tk.Label(
            hdr, text="SubCast Server",
            font=("Segoe UI", 22, "bold"), bg=CARD, fg="white",
        ).pack()
        tk.Label(
            hdr, text="Real-time stream translation server",
            font=("Segoe UI", 9), bg=CARD, fg=FG_DIM,
        ).pack()

        # ── Status row ────────────────────────────────────────────────────────
        sr = tk.Frame(self, bg=DARK, pady=8)
        sr.pack(fill="x", padx=20)

        tk.Label(sr, text="Status:", bg=DARK, fg=FG_DIM,
                 font=("Segoe UI", 10)).pack(side="left")
        self._dot = tk.Label(sr, text="●", bg=DARK, fg=RED,
                              font=("Segoe UI", 14))
        self._dot.pack(side="left", padx=(6, 2))
        self._status_lbl = tk.Label(sr, text="Stopped", bg=DARK, fg=RED,
                                     font=("Segoe UI", 10, "bold"))
        self._status_lbl.pack(side="left")

        tk.Label(sr, text="  │  TV App URL:", bg=DARK, fg=FG_DIM,
                 font=("Segoe UI", 10)).pack(side="left", padx=(20, 4))

        url_txt = f"http://{self._local_ip}:5000"
        self._ip_lbl = tk.Label(sr, text=url_txt, bg=DARK, fg=ACCENT,
                                  font=("Segoe UI", 10, "bold"), cursor="hand2")
        self._ip_lbl.pack(side="left")
        self._ip_lbl.bind("<Button-1>", self._copy_ip)

        tk.Label(sr, text="(click to copy)", bg=DARK, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(side="left", padx=(4, 0))

        # ── Divider ───────────────────────────────────────────────────────────
        tk.Frame(self, bg="#2a2a3e", height=1).pack(fill="x", padx=16, pady=2)

        # ── API Keys ──────────────────────────────────────────────────────────
        env = dotenv_values(ENV_PATH)
        kf = tk.LabelFrame(
            self, text=" API Keys ", bg=DARK, fg=FG_DIM,
            font=("Segoe UI", 9), padx=14, pady=10,
        )
        kf.pack(fill="x", padx=16, pady=6)
        kf.columnconfigure(1, weight=1)

        self._key_vars: dict[str, tk.StringVar] = {}
        for row, (label, key, required) in enumerate([
            ("Groq API Key  *required*", "GROQ_API_KEY", True),
            ("OpenAI API Key  (optional)", "OPENAI_API_KEY", False),
        ]):
            color = "#90c0ff" if required else "#80a0c0"
            tk.Label(kf, text=label, bg=DARK, fg=FG,
                     font=("Segoe UI", 9), anchor="w",
                     width=28).grid(row=row, column=0, sticky="w", pady=4)
            var = tk.StringVar(value=env.get(key, ""))
            self._key_vars[key] = var
            entry = tk.Entry(
                kf, textvariable=var, show="•",
                bg="#0d0d1f", fg=color, relief="flat",
                insertbackground="white", font=MONO,
            )
            entry.grid(row=row, column=1, sticky="ew", pady=4, padx=(8, 0))
            self._bind_paste(entry)

        # ── Buttons ───────────────────────────────────────────────────────────
        bf = tk.Frame(self, bg=DARK)
        bf.pack(pady=8)

        tk.Button(
            bf, text="💾  Save Keys", command=self._save_keys,
            bg="#2a2a4a", fg=FG, font=("Segoe UI", 9), relief="flat",
            padx=14, pady=6, cursor="hand2",
            activebackground="#3a3a6a", activeforeground="white",
        ).pack(side="left", padx=4)

        self._start_btn = tk.Button(
            bf, text="▶  Start Server", command=self._on_start,
            bg=GREEN, fg="white", font=BOLD, relief="flat",
            padx=20, pady=7, cursor="hand2",
            activebackground="#3d8b40", activeforeground="white",
        )
        self._start_btn.pack(side="left", padx=4)

        self._stop_btn = tk.Button(
            bf, text="■  Stop Server", command=self._on_stop,
            bg="#333", fg="#555", font=BOLD, relief="flat",
            padx=20, pady=7, cursor="hand2", state="disabled",
            activebackground="#9b2c2c", activeforeground="white",
        )
        self._stop_btn.pack(side="left", padx=4)

        # ── Divider ───────────────────────────────────────────────────────────
        tk.Frame(self, bg="#2a2a3e", height=1).pack(fill="x", padx=16, pady=(4, 0))

        # ── Log window ────────────────────────────────────────────────────────
        lf = tk.LabelFrame(
            self, text=" Server Log ", bg=DARK, fg=FG_DIM,
            font=("Segoe UI", 9), padx=8, pady=6,
        )
        lf.pack(fill="both", expand=True, padx=16, pady=(4, 16))

        self._log = scrolledtext.ScrolledText(
            lf, bg="#080810", fg="#7fff90", font=MONO,
            relief="flat", state="disabled", wrap="word",
        )
        self._log.pack(fill="both", expand=True)
        self._log.tag_config("err",  foreground="#ff6060")
        self._log.tag_config("warn", foreground="#ffd060")
        self._log.tag_config("info", foreground="#7fff90")

    # ── Entry paste support ───────────────────────────────────────────────────

    def _bind_paste(self, entry: tk.Entry) -> None:
        """Wire right-click on this entry to the shared context menu."""
        entry.bind("<Button-3>", self._show_ctx_menu)

    # ── Class-level Entry handlers (apply to every Entry in the window) ───────

    def _on_entry_paste(self, event: tk.Event) -> str:
        try:
            text = event.widget.tk.call("clipboard", "get")
            event.widget.delete(0, "end")
            event.widget.insert(0, text)
        except Exception:
            pass
        return "break"

    def _on_entry_select_all(self, event: tk.Event) -> str:
        event.widget.select_range(0, "end")
        event.widget.icursor("end")
        return "break"

    def _show_ctx_menu(self, event: tk.Event) -> None:
        self._ctx_entry = event.widget
        try:
            self._ctx_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._ctx_menu.grab_release()

    def _ctx_paste(self) -> None:
        if not self._ctx_entry:
            return
        try:
            text = self._ctx_entry.tk.call("clipboard", "get")
            self._ctx_entry.delete(0, "end")
            self._ctx_entry.insert(0, text)
        except Exception:
            pass

    def _ctx_select_all(self) -> None:
        if self._ctx_entry:
            self._ctx_entry.select_range(0, "end")
            self._ctx_entry.icursor("end")

    def _ctx_clear(self) -> None:
        if self._ctx_entry:
            self._ctx_entry.delete(0, "end")

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _log_append(self, text: str) -> None:
        self._log.configure(state="normal")
        tag = (
            "err"  if ("❌" in text or "Error" in text or " error" in text.lower()) else
            "warn" if ("⚠" in text or "warn" in text.lower()) else
            "info"
        )
        self._log.insert("end", text if text.endswith("\n") else text + "\n", tag)
        self._log.see("end")
        # Trim to 600 lines
        lines = int(self._log.index("end-1c").split(".")[0])
        if lines > 600:
            self._log.delete("1.0", f"{lines - 500}.0")
        self._log.configure(state="disabled")

    def _poll_log(self) -> None:
        try:
            while True:
                self._log_append(str(self._log_queue.get_nowait()))
        except queue.Empty:
            pass
        self.after(120, self._poll_log)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _copy_ip(self, _event=None) -> None:
        url = f"http://{self._local_ip}:5000"
        self.clipboard_clear()
        self.clipboard_append(url)
        self._log_append(f"📋 Copied {url} to clipboard\n")

    def _save_keys(self) -> None:
        if not os.path.exists(ENV_PATH):
            open(ENV_PATH, "w").close()
        saved = []
        for key, var in self._key_vars.items():
            value = var.get().strip()
            if value and not value.startswith("sk-placeholder") and not value.startswith("gsk-placeholder"):
                set_key(ENV_PATH, key, value)
                os.environ[key] = value
                saved.append(key)
        if saved:
            self._log_append(f"✅ Saved: {', '.join(saved)}\n")
        else:
            self._log_append("⚠️  No keys entered — fill in the fields above\n")

    def _on_start(self) -> None:
        self._save_keys()
        # Reload env so lazy clients pick up real keys on first use
        load_dotenv(ENV_PATH, override=True)

        if not os.environ.get("GROQ_API_KEY") or \
                os.environ["GROQ_API_KEY"] == "gsk-placeholder":
            self._log_append(
                "⚠️  Groq API Key is required — enter it and click Save Keys\n"
            )
            return

        # Reset lazy clients so they re-read the freshly set env vars
        _server_module._client      = None
        _server_module._groq_client = None
        _server_module._sl_session  = None

        ok = self._flask.start()
        if ok:
            self._set_status(running=True)

    def _on_stop(self) -> None:
        self._flask.stop()
        self._set_status(running=False)

    def _set_status(self, running: bool) -> None:
        if running:
            self._dot.configure(fg=GREEN)
            self._status_lbl.configure(text="Running", fg=GREEN)
            self._start_btn.configure(state="disabled", bg="#2d7a30")
            self._stop_btn.configure(state="normal", bg=RED, fg="white")
        else:
            self._dot.configure(fg=RED)
            self._status_lbl.configure(text="Stopped", fg=RED)
            self._start_btn.configure(state="normal", bg=GREEN, fg="white")
            self._stop_btn.configure(state="disabled", bg="#333", fg="#555")

    def on_close(self) -> None:
        try:
            self._flask.stop()
        except Exception:
            pass
        self.destroy()
        os._exit(0)  # Force-kill daemon threads (Flask, audio workers)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = SubCastApp()
    root.protocol("WM_DELETE_WINDOW", root.on_close)
    root.mainloop()
