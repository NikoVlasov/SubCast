import tkinter as tk
import threading
import requests

API = "http://localhost:5000"

# (display name, deep-translator / Google Translate code)
LANGUAGES = [
    ("Afrikaans", "af"),
    ("Arabic", "ar"),
    ("Bulgarian", "bg"),
    ("Chinese (Simplified)", "zh-CN"),
    ("Chinese (Traditional)", "zh-TW"),
    ("Croatian", "hr"),
    ("Czech", "cs"),
    ("Danish", "da"),
    ("Dutch", "nl"),
    ("English", "en"),
    ("Estonian", "et"),
    ("Finnish", "fi"),
    ("French", "fr"),
    ("German", "de"),
    ("Greek", "el"),
    ("Hebrew", "he"),
    ("Hungarian", "hu"),
    ("Indonesian", "id"),
    ("Italian", "it"),
    ("Japanese", "ja"),
    ("Korean", "ko"),
    ("Latvian", "lv"),
    ("Lithuanian", "lt"),
    ("Norwegian", "no"),
    ("Polish", "pl"),
    ("Portuguese", "pt"),
    ("Romanian", "ro"),
    ("Russian", "ru"),
    ("Serbian", "sr"),
    ("Slovak", "sk"),
    ("Slovenian", "sl"),
    ("Spanish", "es"),
    ("Swedish", "sv"),
    ("Thai", "th"),
    ("Turkish", "tr"),
    ("Ukrainian", "uk"),
    ("Vietnamese", "vi"),
]

BG         = "#111111"
BAR_BG     = "#1a1a1a"
ENTRY_BG   = "#1c1c1c"
FG         = "#ffffff"
FG_DIM     = "#666666"
FG_HINT    = "#555555"
ACCENT     = "#22c55e"
ACCENT_ACT = "#16a34a"
DANGER     = "#ef4444"

FONT       = "Segoe UI"
POLL_MS    = 3000


class TranslatorOverlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Ultra Translator")
        self.root.overrideredirect(True)       # frameless
        self.root.wm_attributes("-topmost", True)
        self.root.wm_attributes("-alpha", 0.88)
        self.root.configure(bg=BG)
        self.root.resizable(False, False)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w, h = 680, 210
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{sh - h - 80}")

        self._running = False
        self._poll_scheduled = False
        self._last_text = ""
        self._drag_x = self._drag_y = 0
        self._placeholder = "Paste Twitch or YouTube Live URL…"

        self._build_ui()
        self._sync_status()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_titlebar()
        self._build_controls()
        tk.Frame(self.root, bg="#222222", height=1).pack(fill=tk.X)
        self._build_subtitle_area()

    def _build_titlebar(self):
        bar = tk.Frame(self.root, bg=BAR_BG, height=26, cursor="fleur")
        bar.pack(fill=tk.X)
        bar.pack_propagate(False)

        tk.Label(bar, text="  ULTRA TRANSLATOR", bg=BAR_BG, fg="#444444",
                 font=(FONT, 8, "bold")).pack(side=tk.LEFT, pady=4)

        self._dot = tk.Label(bar, text="●", bg=BAR_BG, fg="#333333",
                             font=(FONT, 10))
        self._dot.pack(side=tk.RIGHT, padx=(0, 6))

        close = tk.Label(bar, text=" ✕ ", bg=BAR_BG, fg=FG_DIM,
                         font=(FONT, 9), cursor="hand2")
        close.pack(side=tk.RIGHT)
        close.bind("<Button-1>", lambda _: self.root.destroy())
        close.bind("<Enter>", lambda _: close.configure(bg=DANGER, fg=FG))
        close.bind("<Leave>", lambda _: close.configure(bg=BAR_BG, fg=FG_DIM))

        bar.bind("<ButtonPress-1>", self._drag_start)
        bar.bind("<B1-Motion>",     self._drag_move)

    def _build_controls(self):
        ctrl = tk.Frame(self.root, bg=BG)
        ctrl.pack(fill=tk.X, padx=10, pady=(7, 0))

        # ── URL row ──
        url_row = tk.Frame(ctrl, bg=BG)
        url_row.pack(fill=tk.X)

        self._url_var = tk.StringVar()
        self._url_entry = tk.Entry(
            url_row, textvariable=self._url_var,
            bg=ENTRY_BG, fg="#888888", insertbackground=FG,
            relief=tk.FLAT, font=(FONT, 10),
            highlightthickness=1, highlightbackground="#2a2a2a",
            highlightcolor="#3b82f6", bd=0,
        )
        self._url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
        self._url_entry.insert(0, self._placeholder)
        self._url_entry.bind("<FocusIn>",  self._placeholder_clear)
        self._url_entry.bind("<FocusOut>", self._placeholder_restore)
        self._url_entry.bind("<<Paste>>",  self._on_paste)
        self._url_entry.bind("<Button-3>", self._show_context_menu)

        self._btn_start = tk.Button(
            url_row, text="Start", bg=ACCENT, fg="#000000",
            font=(FONT, 9, "bold"), relief=tk.FLAT, padx=10, pady=5,
            cursor="hand2", command=self._on_start,
            activebackground=ACCENT_ACT, activeforeground="#000000",
        )
        self._btn_start.pack(side=tk.LEFT, padx=(6, 0))

        self._btn_stop = tk.Button(
            url_row, text="Stop", bg="#2a2a2a", fg=FG_DIM,
            font=(FONT, 9, "bold"), relief=tk.FLAT, padx=10, pady=5,
            cursor="hand2", state=tk.DISABLED, command=self._on_stop,
            activebackground=DANGER, activeforeground=FG,
        )
        self._btn_stop.pack(side=tk.LEFT, padx=(4, 0))

        # ── Language row ──
        lang_row = tk.Frame(ctrl, bg=BG)
        lang_row.pack(fill=tk.X, pady=(6, 0))

        lang_names = [name for name, _ in LANGUAGES]

        tk.Label(lang_row, text="From", bg=BG, fg=FG_HINT,
                 font=(FONT, 9)).pack(side=tk.LEFT)

        self._src_var = tk.StringVar(value="Spanish")
        self._make_menu(lang_row, self._src_var, lang_names).pack(side=tk.LEFT, padx=(4, 0))

        tk.Label(lang_row, text="→", bg=BG, fg="#3a3a3a",
                 font=(FONT, 11)).pack(side=tk.LEFT, padx=5)

        self._dest_var = tk.StringVar(value="Russian")
        self._make_menu(lang_row, self._dest_var, lang_names).pack(side=tk.LEFT)

    def _make_menu(self, parent, variable, options):
        btn = tk.OptionMenu(parent, variable, *options)
        btn.configure(
            bg=ENTRY_BG, fg="#888888",
            activebackground="#2a2a2a", activeforeground=FG,
            relief=tk.FLAT, bd=0, font=(FONT, 9),
            highlightthickness=0, cursor="hand2",
            indicatoron=True,
        )
        btn["menu"].configure(
            bg=ENTRY_BG, fg=FG,
            activebackground="#2563eb", activeforeground=FG,
            font=(FONT, 9), relief=tk.FLAT, bd=0,
        )
        return btn

    def _build_subtitle_area(self):
        sub = tk.Frame(self.root, bg=BG)
        sub.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        self._translation_lbl = tk.Label(
            sub, text="Waiting for translation…",
            bg=BG, fg="#2a2a2a", font=(FONT, 15, "bold"),
            wraplength=650, justify=tk.LEFT, anchor=tk.W,
        )
        self._translation_lbl.pack(fill=tk.X, anchor=tk.W)

        self._original_lbl = tk.Label(
            sub, text="",
            bg=BG, fg="#3a3a3a", font=(FONT, 9, "italic"),
            wraplength=650, justify=tk.LEFT, anchor=tk.W,
        )
        self._original_lbl.pack(fill=tk.X, anchor=tk.W, pady=(3, 0))

    # ── Drag ─────────────────────────────────────────────────────────────────

    def _drag_start(self, event):
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()

    def _drag_move(self, event):
        self.root.geometry(f"+{event.x_root - self._drag_x}+{event.y_root - self._drag_y}")

    # ── Placeholder ───────────────────────────────────────────────────────────

    def _placeholder_clear(self, _event):
        if self._url_entry.get() == self._placeholder:
            self._url_entry.delete(0, tk.END)
            self._url_entry.configure(fg=FG)

    def _placeholder_restore(self, _event):
        if not self._url_entry.get():
            self._url_entry.insert(0, self._placeholder)
            self._url_entry.configure(fg="#888888")

    def _on_paste(self, _event):
        # Clear placeholder before the default paste handler inserts clipboard text
        if self._url_entry.get() == self._placeholder:
            self._url_entry.delete(0, tk.END)
            self._url_entry.configure(fg=FG)

    def _show_context_menu(self, event):
        menu = tk.Menu(self.root, tearoff=0,
                       bg=ENTRY_BG, fg=FG,
                       activebackground="#2563eb", activeforeground=FG,
                       relief=tk.FLAT, bd=0, font=(FONT, 9))

        has_selection = bool(self._url_entry.selection_present()
                             if self._url_entry.get() != self._placeholder else False)

        menu.add_command(label="Cut",
                         state=tk.NORMAL if has_selection else tk.DISABLED,
                         command=lambda: self._url_entry.event_generate("<<Cut>>"))
        menu.add_command(label="Copy",
                         state=tk.NORMAL if has_selection else tk.DISABLED,
                         command=lambda: self._url_entry.event_generate("<<Copy>>"))
        menu.add_command(label="Paste",
                         command=self._paste_from_menu)
        menu.add_separator()
        menu.add_command(label="Select All",
                         command=lambda: (self._url_entry.focus(),
                                         self._url_entry.select_range(0, tk.END)))

        menu.tk_popup(event.x_root, event.y_root)

    def _paste_from_menu(self):
        # Focus the entry so clipboard content lands in the right widget,
        # clear the placeholder if present, then trigger the built-in paste.
        self._url_entry.focus()
        if self._url_entry.get() == self._placeholder:
            self._url_entry.delete(0, tk.END)
            self._url_entry.configure(fg=FG)
        self._url_entry.event_generate("<<Paste>>")

    # ── Language helpers ──────────────────────────────────────────────────────

    def _code_of(self, name: str) -> str:
        return next((c for n, c in LANGUAGES if n == name), "en")

    def _name_of(self, code: str) -> str:
        code_l = code.lower()
        return next((n for n, c in LANGUAGES if c == code_l), "English")

    # ── Start / Stop ──────────────────────────────────────────────────────────

    def _on_start(self):
        url = self._url_var.get().strip()
        if not url or url == self._placeholder:
            self._url_entry.focus()
            return

        src  = self._code_of(self._src_var.get())
        dest = self._code_of(self._dest_var.get())
        self._btn_start.configure(state=tk.DISABLED)

        def task():
            try:
                resp = requests.post(
                    f"{API}/start",
                    json={"stream_url": url, "src_lang": src, "dest_lang": dest},
                    timeout=5,
                )
                ok = resp.ok
            except Exception as e:
                print(f"❌ {e}")
                ok = False
            self.root.after(0, lambda: (
                self._set_running(True) if ok
                else self._btn_start.configure(state=tk.NORMAL)
            ))

        threading.Thread(target=task, daemon=True).start()

    def _on_stop(self):
        self._btn_stop.configure(state=tk.DISABLED)

        def task():
            try:
                requests.post(f"{API}/stop", timeout=5)
            except Exception:
                pass
            self.root.after(0, lambda: self._set_running(False))

        threading.Thread(target=task, daemon=True).start()

    def _set_running(self, on: bool):
        self._running = on
        if on:
            self._btn_start.configure(state=tk.DISABLED)
            self._btn_stop.configure(state=tk.NORMAL, bg=DANGER, fg=FG)
            self._dot.configure(fg=ACCENT)
            if not self._poll_scheduled:
                self._poll_scheduled = True
                self._schedule_poll()
        else:
            self._running = False
            self._poll_scheduled = False
            self._btn_start.configure(state=tk.NORMAL)
            self._btn_stop.configure(state=tk.DISABLED, bg="#2a2a2a", fg=FG_DIM)
            self._dot.configure(fg="#333333")

    # ── Polling ───────────────────────────────────────────────────────────────

    def _schedule_poll(self):
        if not self._poll_scheduled:
            return
        threading.Thread(target=self._fetch_latest, daemon=True).start()
        self.root.after(POLL_MS, self._schedule_poll)

    def _fetch_latest(self):
        try:
            resp = requests.get(f"{API}/latest", timeout=3)
            if resp.ok:
                d = resp.json()
                self.root.after(0, self._update_text,
                                d.get("translation", ""),
                                d.get("recognized_text", ""))
                self.root.after(0, lambda: self._dot.configure(fg=ACCENT))
            else:
                self.root.after(0, lambda: self._dot.configure(fg=DANGER))
        except Exception:
            self.root.after(0, lambda: self._dot.configure(fg=DANGER))

    def _update_text(self, translation: str, original: str):
        if not translation or translation == self._last_text:
            return
        self._last_text = translation
        self._translation_lbl.configure(text=translation, fg=FG)
        self._original_lbl.configure(text=f"({original})" if original else "")

    # ── Startup sync ──────────────────────────────────────────────────────────

    def _sync_status(self):
        """Restore running state if the Flask backend is already active."""
        def task():
            try:
                resp = requests.get(f"{API}/status", timeout=3)
                if resp.ok:
                    s = resp.json()
                    if s.get("is_translating"):
                        self.root.after(0, self._restore_running,
                                        s.get("stream_url", ""),
                                        self._name_of(s.get("src_lang", "es")),
                                        self._name_of(s.get("dest_lang", "ru")))
            except Exception:
                pass

        threading.Thread(target=task, daemon=True).start()

    def _restore_running(self, url: str, src_name: str, dest_name: str):
        self._url_entry.delete(0, tk.END)
        self._url_entry.insert(0, url)
        self._url_entry.configure(fg=FG)
        self._src_var.set(src_name)
        self._dest_var.set(dest_name)
        self._set_running(True)

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    TranslatorOverlay().run()