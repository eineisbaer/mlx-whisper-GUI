#!/usr/bin/env python3
"""
Transcriber – yt-dlp + mlx-whisper, packaged as a macOS app via py2app.

Setup:
    pip install customtkinter tkinterdnd2 py2app yt-dlp mlx-whisper

Test:
    python3 transkribierer_app.py

Build:
    python3 setup.py py2app

tkinterdnd2 is optional: without it the drop zone still works as a click-to-browse
button, you just lose actual drag & drop.
"""

import customtkinter as ctk
from tkinter import filedialog, messagebox
from dataclasses import dataclass, field
import subprocess
import threading
import codecs
import os
import sys
import json
import queue
import shutil
import signal
import time
import re

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False
    DND_FILES = None

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

MODELS = [
    "mlx-community/whisper-large-v3-turbo",
    "mlx-community/whisper-large-v3-mlx",
    "mlx-community/whisper-medium-mlx",
    "mlx-community/whisper-small-mlx",
    "mlx-community/whisper-base-mlx",
]

LANGUAGES = ["Auto-Detect", "German", "English", "French", "Spanish", "Italian"]
FORMATS = ["txt", "srt", "vtt", "json"]
TASKS = ["transcribe", "translate"]

NAME_TEMPLATES = [
    "%(title)s",
    "%(upload_date)s_%(title)s",
    "%(uploader)s - %(title)s",
    "%(id)s",
]

MEDIA_EXT = {
    ".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac", ".wma", ".aiff",
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v",
}

HISTORY_MAX = 25

PAGES = ("Queue", "Settings", "Result", "History", "Log")

CONFIG_DIR = os.path.expanduser("~/Library/Application Support/Transcriber")
CONFIG_PATH = os.path.join(CONFIG_DIR, "settings.json")
HISTORY_PATH = os.path.join(CONFIG_DIR, "history.json")
HF_CACHE = os.path.expanduser("~/.cache/huggingface/hub")

# ---------- Palette ----------
BG_ROOT = "#101013"
BG_CARD = "#1A1A20"
BG_INPUT = "#25252E"
BG_INPUT_HOVER = "#32323D"
BG_DROP = "#1F1F27"
BG_LOG = "#0C0C0F"
ACCENT = "#5B8DEF"
ACCENT_HOVER = "#4A78D6"
SUCCESS = "#3FB950"
DANGER = "#E5534B"
DANGER_HOVER = "#C9463F"
WARN = "#D2A24C"
TRACK = "#2A2A33"
TEXT = "#E9E9EF"
TEXT_DIM = "#9A9AA8"
TEXT_FAINT = "#63636F"

PAD_X = 24
CARD_PAD = 18

STATUS_COLORS = {
    "queued": TEXT_FAINT,
    "running": ACCENT,
    "done": SUCCESS,
    "failed": DANGER,
    "cancelled": WARN,
}

# Where tools may live, checked in addition to the inherited PATH. pipx (used by
# install.sh for yt-dlp and mlx_whisper) installs into ~/.local/bin; Homebrew
# uses the two prefixes below on Apple Silicon and Intel respectively.
EXTRA_BIN_DIRS = (
    os.path.expanduser("~/.local/bin"),
    "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
)


def resolve_tool(name):
    """Find an executable without relying on the inherited PATH.

    A py2app bundle launched from Finder inherits a far smaller PATH than a
    login shell — it will not include ~/.local/bin or the Homebrew prefixes — so
    a plain shutil.which() misses the tools. Checking next to sys.executable and
    in the usual install locations covers launching from Finder, from a venv, or
    from a plain shell.
    """
    candidates = [
        os.path.join(os.path.dirname(sys.executable), name),
        shutil.which(name),
    ]
    candidates += [os.path.join(d, name) for d in EXTRA_BIN_DIRS]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _child_env():
    """Environment for spawned tools, cleaned of this process's Python settings.

    Inside a py2app bundle the interpreter runs with PYTHONHOME/PYTHONPATH (etc.)
    pointing at the bundle's stripped-down stdlib. yt-dlp and mlx_whisper are
    separate Python programs with their own interpreter; if they inherit those
    variables they load the bundle's incomplete stdlib and crash (e.g.
    "No module named 'optparse'"). Strip them so each tool uses its own Python.

    We also make sure the tool install dirs are on PATH, since a Finder-launched
    bundle inherits almost none.
    """
    env = os.environ.copy()
    for var in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE",
                "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE", "PYTHONSTARTUP"):
        env.pop(var, None)
    existing = env.get("PATH", "")
    parts = [d for d in EXTRA_BIN_DIRS if d not in existing.split(os.pathsep)]
    env["PATH"] = os.pathsep.join(parts + ([existing] if existing else []))
    return env


CHILD_ENV = _child_env()


@dataclass
class Job:
    kind: str            # "url" or "file"
    source: str
    label: str
    status: str = "queued"
    outputs: list = field(default_factory=list)
    row: object = None
    dot: object = None
    text: object = None


if DND_AVAILABLE:
    class _Base(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.TkdndVersion = TkinterDnD._require(self)
else:
    _Base = ctk.CTk


class TranscriberApp(_Base):
    def __init__(self):
        super().__init__()
        self.withdraw()  # build and measure everything off-screen first
        self.title("Transcriber")
        self.configure(fg_color=BG_ROOT)

        cfg = self._load_json(CONFIG_PATH, {})

        self.output_dir = ctk.StringVar(
            value=cfg.get("output_dir", os.path.expanduser("~/Desktop/Transcripts")))
        self.model = ctk.StringVar(value=cfg.get("model", MODELS[0]))
        self.language = ctk.StringVar(value=cfg.get("language", LANGUAGES[0]))
        self.task = ctk.StringVar(value=cfg.get("task", TASKS[0]))
        self.name_template = ctk.StringVar(value=cfg.get("name_template", NAME_TEMPLATES[0]))
        self.keep_audio = ctk.BooleanVar(value=cfg.get("keep_audio", False))
        self.word_timestamps = ctk.BooleanVar(value=cfg.get("word_timestamps", False))
        self.condition_on_previous = ctk.BooleanVar(value=cfg.get("condition_on_previous", True))
        self.notify = ctk.BooleanVar(value=cfg.get("notify", True))
        self.autopaste = ctk.BooleanVar(value=cfg.get("autopaste", True))
        self.format_vars = {
            fmt: ctk.BooleanVar(value=cfg.get("formats", {}).get(fmt, fmt == "txt"))
            for fmt in FORMATS
        }

        for var in [self.output_dir, self.model, self.language, self.task,
                    self.name_template, self.keep_audio, self.word_timestamps,
                    self.condition_on_previous, self.notify, self.autopaste,
                    *self.format_vars.values()]:
            var.trace_add("write", lambda *_: self._save_config())

        self.jobs = []
        self.history = self._load_json(HISTORY_PATH, [])
        self.log_queue = queue.Queue()
        self.last_output_dir = None
        self.proc = None
        self.cancelled = False
        self.running = False
        self._caffeinate = None
        self.tools = {name: resolve_tool(name)
                      for name in ("yt-dlp", "mlx_whisper", "ffmpeg", "ffprobe",
                                   "caffeinate", "osascript", "open")}

        self._build_ui()
        self._register_dnd()
        self._bind_keys()
        self._refresh_history()
        self._layout_pages()
        self._fit_window()
        self.deiconify()
        self._poll_log_queue()

        required = [n for n in ("yt-dlp", "mlx_whisper") if not self.tools[n]]
        if required:
            self._log(f"⚠️  Required tool(s) not found: {', '.join(required)}")
            self._log("    Install them with: pip install yt-dlp mlx-whisper")
        if not self.tools["ffmpeg"]:
            self._log("⚠️  ffmpeg not found — downloads cannot be converted to mp3.")
            self._log("    Install it with: brew install ffmpeg")
        if not DND_AVAILABLE:
            self._log("ℹ️  tkinterdnd2 not installed — drag & drop disabled.")
        if not required and self.tools["ffmpeg"]:
            self._log("Ready.")

        if self.autopaste.get():
            self.after(200, self._try_autopaste)

    # ---------- Persistence ----------
    @staticmethod
    def _load_json(path, default):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return default

    @staticmethod
    def _write_json(path, data):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass

    def _save_config(self):
        self._write_json(CONFIG_PATH, {
            "output_dir": self.output_dir.get(),
            "model": self.model.get(),
            "language": self.language.get(),
            "task": self.task.get(),
            "name_template": self.name_template.get(),
            "keep_audio": self.keep_audio.get(),
            "word_timestamps": self.word_timestamps.get(),
            "condition_on_previous": self.condition_on_previous.get(),
            "notify": self.notify.get(),
            "autopaste": self.autopaste.get(),
            "formats": {fmt: v.get() for fmt, v in self.format_vars.items()},
        })

    # ---------- Window ----------
    def _fit_window(self):
        """Size the window to its natural content height. Only if the display is
        too small does the content area get squeezed."""
        self.update_idletasks()
        needed_w = max(self.winfo_reqwidth(), 780)
        needed_h = self.winfo_reqheight()

        budget = int(self.winfo_screenheight() * 0.92)
        if needed_h > budget:
            # Give the shortfall back by shrinking the content area.
            overflow = needed_h - budget
            current = self.content.cget("height")
            self.content.configure(height=max(240, current - overflow))
            self.update_idletasks()
            needed_h = min(self.winfo_reqheight(), budget)

        self.geometry(f"{needed_w}x{needed_h}")
        self.minsize(needed_w, needed_h)

    def _bind_keys(self):
        self.bind("<Return>", lambda e: None if self.running else self._start())
        self.bind("<Escape>", lambda e: self._cancel() if self.running else None)
        self.bind("<Command-o>", lambda e: self._browse_file())

    def _try_autopaste(self):
        try:
            text = self.clipboard_get().strip()
        except Exception:
            return
        if re.match(r"https?://(www\.)?(youtube\.com|youtu\.be)/", text):
            if not self.url_entry.get().strip():
                self.url_entry.insert(0, text)
                self._log("📋 Pasted YouTube URL from clipboard.\n")

    # ---------- UI builders ----------
    def _dropdown(self, parent, variable, values, width=360):
        return ctk.CTkOptionMenu(
            parent, variable=variable, values=values, width=width, height=32,
            corner_radius=8, fg_color=BG_INPUT, button_color=BG_INPUT,
            button_hover_color=BG_INPUT_HOVER, text_color=TEXT,
            dropdown_fg_color=BG_INPUT, dropdown_hover_color=BG_INPUT_HOVER,
            font=ctk.CTkFont(size=13), dropdown_font=ctk.CTkFont(size=13))

    def _checkbox(self, parent, text, variable):
        return ctk.CTkCheckBox(
            parent, text=text, variable=variable,
            checkbox_width=17, checkbox_height=17, corner_radius=5,
            border_width=2, border_color=TRACK,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=13), text_color=TEXT)

    def _small_button(self, parent, text, command, width=96, danger=False):
        return ctk.CTkButton(
            parent, text=text, width=width, height=32, corner_radius=8,
            fg_color=BG_INPUT, hover_color=DANGER_HOVER if danger else BG_INPUT_HOVER,
            text_color=TEXT_DIM, font=ctk.CTkFont(size=12), command=command)

    def _section(self, parent, text):
        ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=TEXT_FAINT, anchor="w").pack(anchor="w", pady=(14, 8))

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=PAD_X, pady=(22, 14))
        ctk.CTkLabel(header, text="Transcriber",
                     font=ctk.CTkFont(size=24, weight="bold"),
                     text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(header, text="YouTube, playlists or local media → text, all on-device",
                     font=ctk.CTkFont(size=12), text_color=TEXT_FAINT
                     ).pack(anchor="w", pady=(3, 0))

        # ---- Navigation ----
        self.page_var = ctk.StringVar(value=PAGES[0])
        self.tabbar = ctk.CTkSegmentedButton(
            self, values=list(PAGES), variable=self.page_var, command=self._show_page,
            height=34, corner_radius=9, font=ctk.CTkFont(size=13),
            fg_color=BG_CARD, selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            unselected_color=BG_CARD, unselected_hover_color=BG_INPUT,
            text_color=TEXT, text_color_disabled=TEXT_FAINT)
        self.tabbar.pack(fill="x", padx=PAD_X, pady=(0, 12))

        # ---- Pages ----
        self.content = ctk.CTkFrame(self, corner_radius=14, fg_color=BG_CARD)
        self.content.pack(fill="both", expand=True, padx=PAD_X, pady=(0, 12))

        self.pages = {}
        builders = {
            "Queue": self._build_queue_tab,
            "Settings": self._build_settings_tab,
            "Result": self._build_result_tab,
            "History": self._build_history_tab,
            "Log": self._build_log_tab,
        }
        for name in PAGES:
            page = ctk.CTkFrame(self.content, fg_color="transparent")
            builders[name](page)
            self.pages[name] = page

        # ---- Progress ----
        prog = ctk.CTkFrame(self, corner_radius=14, fg_color=BG_CARD)
        prog.pack(fill="x", padx=PAD_X, pady=(0, 12))
        inner = ctk.CTkFrame(prog, fg_color="transparent")
        inner.pack(fill="x", padx=CARD_PAD, pady=14)

        self.job_label = ctk.CTkLabel(
            inner, text="No job running", font=ctk.CTkFont(size=12, weight="bold"),
            text_color=TEXT_DIM, anchor="w")
        self.job_label.pack(fill="x", pady=(0, 9))

        self.md_bar, self.md_label = self._progress_row(inner, "Model")
        self.dl_bar, self.dl_label = self._progress_row(inner, "Download", top_pad=9)
        self.tr_bar, self.tr_label = self._progress_row(inner, "Transcription", top_pad=9)

        # ---- Actions ----
        action = ctk.CTkFrame(self, fg_color="transparent")
        action.pack(fill="x", padx=PAD_X, pady=(0, 20))
        self.start_btn = ctk.CTkButton(
            action, text="Start Queue", height=44, corner_radius=11,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self._start)
        self.start_btn.pack(side="left", fill="x", expand=True)
        self.cancel_btn = ctk.CTkButton(
            action, text="Cancel", width=100, height=44, corner_radius=11,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=BG_INPUT, hover_color=DANGER_HOVER, text_color=TEXT_DIM,
            command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(10, 0))

    # ---------- Page switching / sizing ----------
    def _show_page(self, name):
        for page in self.pages.values():
            page.pack_forget()
        self.pages[name].pack(fill="both", expand=True, padx=CARD_PAD, pady=CARD_PAD)
        self.page_var.set(name)

    def _layout_pages(self):
        """Measure every page while hidden, then pin the content area to the
        tallest one so the window never needs to scroll and never jumps when
        switching pages."""
        needed_h, needed_w = 0, 0
        for page in self.pages.values():
            page.pack(fill="both", expand=True, padx=CARD_PAD, pady=CARD_PAD)
            self.update_idletasks()
            needed_h = max(needed_h, page.winfo_reqheight())
            needed_w = max(needed_w, page.winfo_reqwidth())
            page.pack_forget()

        self.content.configure(height=needed_h + 2 * CARD_PAD)
        self.content.pack_propagate(False)
        self._show_page(PAGES[0])

    # ---------- Tab: Queue ----------
    def _build_queue_tab(self, tab):
        add_row = ctk.CTkFrame(tab, fg_color="transparent")
        add_row.pack(fill="x", pady=(6, 0))
        self.url_entry = ctk.CTkEntry(
            add_row, placeholder_text="Paste a YouTube video or playlist URL…",
            height=38, corner_radius=9, font=ctk.CTkFont(size=13),
            fg_color=BG_INPUT, border_width=0, text_color=TEXT)
        self.url_entry.pack(side="left", fill="x", expand=True)
        self.url_entry.bind("<Return>", lambda e: self._add_url())
        ctk.CTkButton(
            add_row, text="Add", width=76, height=38, corner_radius=9,
            fg_color=BG_INPUT, hover_color=BG_INPUT_HOVER, text_color=TEXT,
            font=ctk.CTkFont(size=13), command=self._add_url
        ).pack(side="left", padx=(8, 0))

        self.drop_zone = ctk.CTkFrame(
            tab, corner_radius=10, fg_color=BG_DROP,
            border_width=1, border_color=TRACK, height=54)
        self.drop_zone.pack(fill="x", pady=8)
        self.drop_zone.pack_propagate(False)
        self.drop_label = ctk.CTkLabel(
            self.drop_zone,
            text=("Drop audio or video files here  ·  click to browse  ·  ⌘O"
                  if DND_AVAILABLE else "Click to choose audio or video files  ·  ⌘O"),
            font=ctk.CTkFont(size=12), text_color=TEXT_FAINT)
        self.drop_label.pack(expand=True)
        for w in (self.drop_zone, self.drop_label):
            w.bind("<Button-1>", lambda e: self._browse_file())

        head = ctk.CTkFrame(tab, fg_color="transparent")
        head.pack(fill="x", pady=(6, 4))
        self.queue_count = ctk.CTkLabel(
            head, text="Queue is empty", font=ctk.CTkFont(size=11, weight="bold"),
            text_color=TEXT_FAINT, anchor="w")
        self.queue_count.pack(side="left")
        self._small_button(head, "Clear done", self._clear_finished, width=94).pack(side="right")
        self._small_button(head, "Clear all", self._clear_queue, width=84,
                           danger=True).pack(side="right", padx=(0, 8))

        self.queue_frame = ctk.CTkScrollableFrame(
            tab, fg_color=BG_LOG, corner_radius=9, height=340)
        self.queue_frame.pack(fill="both", expand=True, pady=(0, 8))

    # ---------- Tab: Settings ----------
    def _build_settings_tab(self, tab):
        scroll = ctk.CTkFrame(tab, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        grid = ctk.CTkFrame(scroll, fg_color="transparent")
        grid.pack(fill="x", pady=(6, 0))
        grid.grid_columnconfigure(1, weight=1)

        rows = [("Model", self.model, MODELS),
                ("Language", self.language, LANGUAGES),
                ("Task", self.task, TASKS)]
        for i, (label, var, values) in enumerate(rows):
            ctk.CTkLabel(grid, text=label, anchor="w", width=88,
                         font=ctk.CTkFont(size=13), text_color=TEXT_DIM
                         ).grid(row=i, column=0, sticky="w", pady=5)
            self._dropdown(grid, var, values, width=400).grid(
                row=i, column=1, sticky="w", pady=5)

        ctk.CTkLabel(grid, text="transcribe keeps the original language · translate outputs English",
                     font=ctk.CTkFont(size=11), text_color=TEXT_FAINT, anchor="w"
                     ).grid(row=3, column=1, sticky="w", pady=(0, 4))

        ctk.CTkLabel(grid, text="Filename", anchor="w", width=88,
                     font=ctk.CTkFont(size=13), text_color=TEXT_DIM
                     ).grid(row=4, column=0, sticky="w", pady=5)
        name_box = ctk.CTkComboBox(
            grid, variable=self.name_template, values=NAME_TEMPLATES, width=400, height=32,
            corner_radius=8, fg_color=BG_INPUT, border_width=0, button_color=BG_INPUT,
            button_hover_color=BG_INPUT_HOVER, text_color=TEXT,
            dropdown_fg_color=BG_INPUT, dropdown_hover_color=BG_INPUT_HOVER,
            font=ctk.CTkFont(size=13), dropdown_font=ctk.CTkFont(size=13))
        name_box.grid(row=4, column=1, sticky="w", pady=5)
        ctk.CTkLabel(grid, text="yt-dlp output template · applies to downloads only",
                     font=ctk.CTkFont(size=11), text_color=TEXT_FAINT, anchor="w"
                     ).grid(row=5, column=1, sticky="w", pady=(0, 4))

        ctk.CTkLabel(grid, text="Formats", anchor="w", width=88,
                     font=ctk.CTkFont(size=13), text_color=TEXT_DIM
                     ).grid(row=6, column=0, sticky="w", pady=5)
        fmt_row = ctk.CTkFrame(grid, fg_color="transparent")
        fmt_row.grid(row=6, column=1, sticky="w", pady=5)
        for fmt in FORMATS:
            self._checkbox(fmt_row, fmt, self.format_vars[fmt]).pack(side="left", padx=(0, 18))

        self._section(scroll, "OPTIONS")
        self._checkbox(scroll, "Word-level timestamps", self.word_timestamps).pack(anchor="w", pady=3)
        self._checkbox(scroll, "Keep context between segments", self.condition_on_previous).pack(anchor="w", pady=3)
        self._checkbox(scroll, "Keep downloaded audio (local files are never deleted)",
                       self.keep_audio).pack(anchor="w", pady=3)
        self._checkbox(scroll, "Notify me when the queue finishes", self.notify).pack(anchor="w", pady=3)
        self._checkbox(scroll, "Paste YouTube URLs from clipboard on launch",
                       self.autopaste).pack(anchor="w", pady=3)

        self._section(scroll, "OUTPUT FOLDER")
        out_row = ctk.CTkFrame(scroll, fg_color="transparent")
        out_row.pack(fill="x", pady=(0, 10))
        ctk.CTkEntry(out_row, textvariable=self.output_dir, height=36, corner_radius=9,
                     fg_color=BG_INPUT, border_width=0, font=ctk.CTkFont(size=13),
                     text_color=TEXT).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(out_row, text="Choose…", width=94, height=36, corner_radius=9,
                      fg_color=BG_INPUT, hover_color=BG_INPUT_HOVER, text_color=TEXT,
                      font=ctk.CTkFont(size=13), command=self._choose_dir
                      ).pack(side="left", padx=(10, 0))

    # ---------- Tab: Result ----------
    def _build_result_tab(self, tab):
        head = ctk.CTkFrame(tab, fg_color="transparent")
        head.pack(fill="x", pady=(6, 6))
        self.result_picker = ctk.CTkOptionMenu(
            head, values=["—"], width=340, height=30, corner_radius=8,
            fg_color=BG_INPUT, button_color=BG_INPUT, button_hover_color=BG_INPUT_HOVER,
            text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_hover_color=BG_INPUT_HOVER,
            font=ctk.CTkFont(size=12), dropdown_font=ctk.CTkFont(size=12),
            command=self._show_result)
        self.result_picker.pack(side="left")
        self._small_button(head, "Copy", self._copy_result, width=76).pack(side="right")
        self._small_button(head, "Open file", self._open_result, width=88
                           ).pack(side="right", padx=(0, 8))

        self.result_text = ctk.CTkTextbox(
            tab, corner_radius=9, fg_color=BG_LOG, wrap="word", height=440,
            font=ctk.CTkFont(size=12), text_color=TEXT_DIM, border_width=0)
        self.result_text.pack(fill="both", expand=True, pady=(0, 8))
        self.result_text.insert("1.0", "No transcript yet.")
        self.result_text.configure(state="disabled")
        self._result_files = {}

    # ---------- Tab: History ----------
    def _build_history_tab(self, tab):
        head = ctk.CTkFrame(tab, fg_color="transparent")
        head.pack(fill="x", pady=(6, 4))
        ctk.CTkLabel(head, text="RECENT TRANSCRIPTIONS",
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=TEXT_FAINT, anchor="w").pack(side="left")
        self._small_button(head, "Clear", self._clear_history, width=70,
                           danger=True).pack(side="right")
        self.history_frame = ctk.CTkScrollableFrame(
            tab, fg_color=BG_LOG, corner_radius=9, height=440)
        self.history_frame.pack(fill="both", expand=True, pady=(0, 8))

    # ---------- Tab: Log ----------
    def _build_log_tab(self, tab):
        head = ctk.CTkFrame(tab, fg_color="transparent")
        head.pack(fill="x", pady=(6, 4))
        ctk.CTkLabel(head, text="OUTPUT", font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=TEXT_FAINT, anchor="w").pack(side="left")
        self.open_folder_btn = ctk.CTkButton(
            head, text="Open Folder", width=100, height=26, corner_radius=7,
            fg_color=BG_INPUT, hover_color=BG_INPUT_HOVER, text_color=TEXT_DIM,
            font=ctk.CTkFont(size=12), command=self._open_output_folder, state="disabled")
        self.open_folder_btn.pack(side="right")
        self._small_button(head, "Clear", self._clear_log, width=64
                           ).pack(side="right", padx=(0, 8))
        self.log_text = ctk.CTkTextbox(
            tab, corner_radius=9, fg_color=BG_LOG, height=440,
            font=ctk.CTkFont(family="Menlo", size=11),
            text_color=TEXT_DIM, border_width=0)
        self.log_text.pack(fill="both", expand=True, pady=(0, 8))
        self.log_text.configure(state="disabled")

    def _progress_row(self, parent, name, top_pad=0):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(top_pad, 0))
        head = ctk.CTkFrame(row, fg_color="transparent")
        head.pack(fill="x")
        ctk.CTkLabel(head, text=name, font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=TEXT_DIM, anchor="w").pack(side="left")
        status = ctk.CTkLabel(head, text="idle", font=ctk.CTkFont(size=11),
                              text_color=TEXT_FAINT, anchor="e")
        status.pack(side="right")
        bar = ctk.CTkProgressBar(row, height=5, corner_radius=3,
                                 fg_color=TRACK, progress_color=ACCENT)
        bar.pack(fill="x", pady=(6, 0))
        bar.set(0)
        return bar, status

    # ---------- Drag & drop ----------
    def _register_dnd(self):
        if not DND_AVAILABLE:
            return
        for widget in (self.drop_zone, self.drop_label):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)
            widget.dnd_bind("<<DragEnter>>", self._on_drag_enter)
            widget.dnd_bind("<<DragLeave>>", self._on_drag_leave)

    def _on_drag_enter(self, event):
        self.drop_zone.configure(border_color=ACCENT, fg_color=BG_INPUT)
        return event.action

    def _on_drag_leave(self, event):
        self.drop_zone.configure(border_color=TRACK, fg_color=BG_DROP)
        return event.action

    def _on_drop(self, event):
        self._on_drag_leave(event)
        for path in self.tk.splitlist(event.data):
            self._add_file(path)

    def _browse_file(self):
        paths = filedialog.askopenfilenames(
            title="Choose audio or video files",
            filetypes=[("Media files", " ".join(f"*{e}" for e in sorted(MEDIA_EXT))),
                       ("All files", "*.*")])
        for path in paths:
            self._add_file(path)

    # ---------- Queue management ----------
    def _add_file(self, path):
        if not os.path.isfile(path):
            return
        ext = os.path.splitext(path)[1].lower()
        if ext not in MEDIA_EXT and not messagebox.askyesno(
                "Unusual file type",
                f"{ext or 'This file'} is not a typical media extension. Add it anyway?"):
            return
        self._append_job(Job(kind="file", source=path, label=os.path.basename(path)))

    def _add_url(self):
        url = self.url_entry.get().strip()
        if not url:
            return
        self.url_entry.delete(0, "end")
        if re.search(r"[?&]list=|/playlist\?|/@[\w.-]+/?$", url):
            self._log(f"\n🔎 Expanding playlist: {url}\n")
            threading.Thread(target=self._expand_playlist, args=(url,), daemon=True).start()
        else:
            self._append_job(Job(kind="url", source=url, label=url))

    def _expand_playlist(self, url):
        try:
            out = subprocess.run(
                [self.tools["yt-dlp"] or "yt-dlp", "--flat-playlist",
                 "--print", "%(id)s\t%(title)s", url],
                capture_output=True, text=True, timeout=120, env=CHILD_ENV)
        except (subprocess.SubprocessError, OSError) as e:
            self._log(f"❌ Could not read playlist: {e}\n")
            return
        entries = [l for l in out.stdout.strip().splitlines() if l.strip()]
        if not entries:
            self._log("⚠️  No entries found — adding the URL as a single job.\n")
            self.after(0, lambda: self._append_job(Job(kind="url", source=url, label=url)))
            return
        for line in entries:
            vid, _, title = line.partition("\t")
            job = Job(kind="url", source=f"https://www.youtube.com/watch?v={vid}",
                      label=title or vid)
            self.after(0, lambda j=job: self._append_job(j))
        self._log(f"✅ Added {len(entries)} videos from the playlist.\n")

    def _append_job(self, job):
        self.jobs.append(job)
        self._render_job(job)
        self._refresh_queue_count()

    def _render_job(self, job):
        row = ctk.CTkFrame(self.queue_frame, fg_color="transparent", height=30)
        row.pack(fill="x", pady=2, padx=4)
        dot = ctk.CTkLabel(row, text="●", width=16, font=ctk.CTkFont(size=13),
                           text_color=STATUS_COLORS[job.status])
        dot.pack(side="left")
        text = ctk.CTkLabel(row, text=job.label, anchor="w",
                            font=ctk.CTkFont(size=12), text_color=TEXT_DIM)
        text.pack(side="left", fill="x", expand=True, padx=(4, 8))
        ctk.CTkButton(row, text="✕", width=24, height=22, corner_radius=6,
                      fg_color="transparent", hover_color=DANGER_HOVER,
                      text_color=TEXT_FAINT, font=ctk.CTkFont(size=12),
                      command=lambda: self._remove_job(job)).pack(side="right")
        job.row, job.dot, job.text = row, dot, text

    def _set_job_status(self, job, status):
        job.status = status

        def apply():
            if job.dot is not None:
                job.dot.configure(text_color=STATUS_COLORS[status])
            if job.text is not None:
                job.text.configure(text_color=TEXT if status == "running" else TEXT_DIM)
        self.after(0, apply)

    def _remove_job(self, job):
        if job.status == "running":
            return
        if job.row is not None:
            job.row.destroy()
        if job in self.jobs:
            self.jobs.remove(job)
        self._refresh_queue_count()

    def _clear_finished(self):
        for job in [j for j in self.jobs if j.status in ("done", "failed", "cancelled")]:
            self._remove_job(job)

    def _clear_queue(self):
        for job in list(self.jobs):
            self._remove_job(job)

    def _refresh_queue_count(self):
        pending = sum(1 for j in self.jobs if j.status == "queued")
        if not self.jobs:
            self.queue_count.configure(text="Queue is empty")
        else:
            self.queue_count.configure(
                text=f"{len(self.jobs)} job(s) · {pending} pending")

    # ---------- Result / history ----------
    def _register_result(self, label, path):
        self._result_files[label] = path

        def apply():
            values = list(self._result_files.keys())
            self.result_picker.configure(values=values)
            self.result_picker.set(label)
            self._show_result(label)
        self.after(0, apply)

    def _show_result(self, label):
        path = self._result_files.get(label)
        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", "end")
        if path and os.path.exists(path):
            try:
                with open(path, "r", errors="replace") as f:
                    self.result_text.insert("1.0", f.read())
            except OSError as e:
                self.result_text.insert("1.0", f"Could not read file:\n{e}")
        else:
            self.result_text.insert("1.0", "No transcript yet.")
        self.result_text.configure(state="disabled")

    def _copy_result(self):
        text = self.result_text.get("1.0", "end").strip()
        if text and text != "No transcript yet.":
            self.clipboard_clear()
            self.clipboard_append(text)
            self._log("📋 Transcript copied to clipboard.\n")

    def _open_result(self):
        path = self._result_files.get(self.result_picker.get())
        if path and os.path.exists(path):
            subprocess.run([self.tools["open"] or "open", path], env=CHILD_ENV)

    def _add_history(self, label, out_dir, files):
        self.history.insert(0, {
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "label": label, "dir": out_dir, "files": files,
        })
        del self.history[HISTORY_MAX:]
        self._write_json(HISTORY_PATH, self.history)
        self.after(0, self._refresh_history)

    def _refresh_history(self):
        for child in self.history_frame.winfo_children():
            child.destroy()
        if not self.history:
            ctk.CTkLabel(self.history_frame, text="Nothing yet.",
                         font=ctk.CTkFont(size=12), text_color=TEXT_FAINT
                         ).pack(anchor="w", padx=10, pady=10)
            return
        for entry in self.history:
            row = ctk.CTkFrame(self.history_frame, fg_color="transparent")
            row.pack(fill="x", pady=2, padx=4)
            info = ctk.CTkFrame(row, fg_color="transparent")
            info.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(info, text=entry["label"], anchor="w",
                         font=ctk.CTkFont(size=12), text_color=TEXT_DIM
                         ).pack(anchor="w")
            ctk.CTkLabel(info, text=entry["time"], anchor="w",
                         font=ctk.CTkFont(size=10), text_color=TEXT_FAINT
                         ).pack(anchor="w")
            first = entry["files"][0] if entry.get("files") else None
            ctk.CTkButton(
                row, text="Open", width=58, height=24, corner_radius=6,
                fg_color=BG_INPUT, hover_color=BG_INPUT_HOVER, text_color=TEXT_DIM,
                font=ctk.CTkFont(size=11),
                command=lambda p=first, d=entry["dir"]: subprocess.run(
                    [self.tools["open"] or "open",
                     p if p and os.path.exists(p) else d], env=CHILD_ENV)
            ).pack(side="right")

    def _clear_history(self):
        self.history = []
        self._write_json(HISTORY_PATH, self.history)
        self._refresh_history()

    # ---------- Misc ----------
    def _choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.output_dir.get() or os.path.expanduser("~"))
        if d:
            self.output_dir.set(d)

    def _open_output_folder(self):
        if self.last_output_dir and os.path.isdir(self.last_output_dir):
            subprocess.run([self.tools["open"] or "open", self.last_output_dir],
                           env=CHILD_ENV)

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _log(self, text):
        self.log_queue.put(text)

    def _poll_log_queue(self):
        try:
            while True:
                text = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", text if text.endswith("\n") else text + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(120, self._poll_log_queue)

    def _notify_done(self, title, message):
        if not self.notify.get():
            return
        try:
            subprocess.run([self.tools["osascript"] or "osascript", "-e",
                            f"display notification {json.dumps(message)} "
                            f"with title {json.dumps(title)}"], timeout=5, env=CHILD_ENV)
        except (subprocess.SubprocessError, OSError):
            pass

    def _set_bar(self, bar, label, fraction, text, state="run"):
        fraction = max(0.0, min(1.0, fraction))
        color = {"run": ACCENT, "done": SUCCESS, "fail": DANGER, "skip": TEXT_FAINT}[state]
        tcol = {"run": TEXT_DIM, "done": SUCCESS, "fail": DANGER, "skip": TEXT_FAINT}[state]

        def apply():
            bar.set(fraction)
            bar.configure(progress_color=color)
            label.configure(text=text, text_color=tcol)
        self.after(0, apply)

    def _reset_bars(self):
        def apply():
            for bar, label in ((self.md_bar, self.md_label),
                               (self.dl_bar, self.dl_label),
                               (self.tr_bar, self.tr_label)):
                bar.set(0)
                bar.configure(progress_color=ACCENT)
                label.configure(text="idle", text_color=TEXT_FAINT)
        self.after(0, apply)

    @staticmethod
    def _fmt_duration(seconds):
        seconds = int(seconds)
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m {seconds % 60:02d}s"
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"

    def _model_is_cached(self):
        folder = "models--" + self.model.get().replace("/", "--")
        return os.path.isdir(os.path.join(HF_CACHE, folder))

    # ---------- Run control ----------
    def _start(self):
        if self.running:
            return
        url = self.url_entry.get().strip()
        if url:
            self._add_url()
        pending = [j for j in self.jobs if j.status == "queued"]
        if not pending:
            messagebox.showwarning("Empty queue",
                                   "Add a URL or drop a media file first.")
            return
        if not any(v.get() for v in self.format_vars.values()):
            messagebox.showwarning("Missing input", "Select at least one output format.")
            return
        try:
            os.makedirs(self.output_dir.get(), exist_ok=True)
        except OSError as e:
            messagebox.showerror("Folder error", f"Cannot create output folder:\n{e}")
            return

        self.running = True
        self.cancelled = False
        self.start_btn.configure(state="disabled", text="Running…")
        self.cancel_btn.configure(state="normal", fg_color=DANGER, text_color=TEXT)
        self.open_folder_btn.configure(state="disabled")
        self._start_caffeinate()
        threading.Thread(target=self._run_queue, daemon=True).start()

    def _cancel(self):
        if not self.running:
            return
        self.cancelled = True
        self.cancel_btn.configure(state="disabled", text="Stopping…")
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGTERM)
            except OSError:
                pass

    def _start_caffeinate(self):
        try:
            self._caffeinate = subprocess.Popen(
                [self.tools["caffeinate"] or "caffeinate", "-i"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=CHILD_ENV)
        except (OSError, subprocess.SubprocessError):
            self._caffeinate = None

    def _stop_caffeinate(self):
        if self._caffeinate and self._caffeinate.poll() is None:
            try:
                self._caffeinate.terminate()
            except OSError:
                pass
        self._caffeinate = None

    # ---------- Subprocess plumbing ----------
    def _stream_lines(self, stream):
        """Yield lines split on BOTH \\n and \\r.

        tqdm (HuggingFace downloads) and yt-dlp redraw their progress with a
        carriage return, so plain line iteration would block until the whole
        download is finished.
        """
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        fd = stream.fileno()
        buf = ""
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += decoder.decode(chunk)
            parts = re.split(r"[\r\n]", buf)
            buf = parts.pop()
            for part in parts:
                if part.strip():
                    yield part
        if buf.strip():
            yield buf

    def _run_stream(self, cmd, line_callback=None):
        self._log(f"$ {' '.join(cmd)}")
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
            env=CHILD_ENV)
        for line in self._stream_lines(self.proc.stdout):
            handled = line_callback(line) if line_callback else False
            if not handled:
                self._log(line)
        self.proc.wait()
        rc = self.proc.returncode
        self.proc = None
        return rc

    @staticmethod
    def _parse_timecode(tc):
        seconds = 0.0
        for part in tc.split(":"):
            seconds = seconds * 60 + float(part)
        return seconds

    def _get_audio_duration(self, path):
        try:
            out = subprocess.run(
                [self.tools["ffprobe"] or "ffprobe", "-v", "error",
                 "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=20, env=CHILD_ENV)
            return float(out.stdout.strip())
        except (subprocess.SubprocessError, ValueError, OSError):
            return None

    # ---------- Pipeline ----------
    def _run_queue(self):
        pending = [j for j in self.jobs if j.status == "queued"]
        total = len(pending)
        completed = 0
        try:
            for index, job in enumerate(pending, start=1):
                if self.cancelled:
                    self._set_job_status(job, "cancelled")
                    continue
                self.after(0, lambda i=index, j=job: self.job_label.configure(
                    text=f"Job {i} of {total} · {j.label}", text_color=TEXT))
                self._reset_bars()
                self._set_job_status(job, "running")
                ok = self._run_job(job)
                self._set_job_status(job, "cancelled" if self.cancelled
                                     else ("done" if ok else "failed"))
                completed += 1 if ok else 0
                self.after(0, self._refresh_queue_count)

            if self.cancelled:
                self._log("\n⏹  Queue cancelled.")
            else:
                self._log(f"\n✅ Queue finished — {completed} of {total} succeeded.")
                self._notify_done("Transcription queue finished",
                                  f"{completed} of {total} jobs succeeded")
        finally:
            self._stop_caffeinate()

            def reset():
                self.running = False
                self.proc = None
                self.start_btn.configure(state="normal", text="Start Queue")
                self.cancel_btn.configure(state="disabled", text="Cancel",
                                          fg_color=BG_INPUT, text_color=TEXT_DIM)
                self.job_label.configure(text="No job running", text_color=TEXT_DIM)
                if self.last_output_dir:
                    self.open_folder_btn.configure(state="normal")
            self.after(0, reset)

    def _run_job(self, job):
        out_dir = self.output_dir.get()
        dl_re = re.compile(r"\[download\]\s+([\d.]+)%")
        pct_re = re.compile(r"(\d{1,3})%\|")
        ts_re = re.compile(
            r"\[(\d{2}:\d{2}(?::\d{2})?\.\d{3})\s*-->\s*(\d{2}:\d{2}(?::\d{2})?\.\d{3})\]")
        downloaded_audio = None

        def on_download_line(line):
            m = dl_re.search(line)
            if not m:
                return False
            pct = float(m.group(1))
            self._set_bar(self.dl_bar, self.dl_label, pct / 100.0, f"{pct:.1f}%")
            return True  # shown in the bar, keep it out of the log

        try:
            # ---- Phase 1: obtain audio ----
            if job.kind == "file":
                audio_path = job.source
                self._set_bar(self.dl_bar, self.dl_label, 1.0, "local file", state="skip")
                self._log(f"\nUsing local file: {audio_path}")
            else:
                self._set_bar(self.dl_bar, self.dl_label, 0, "starting…")
                template = os.path.join(out_dir, self.name_template.get() + ".%(ext)s")
                # Remember when we started so a leftover mp3 from an earlier job
                # (kept via "keep downloaded audio") can't be picked up by mistake.
                start_mtime = time.time() - 1
                rc = self._run_stream(
                    [self.tools["yt-dlp"] or "yt-dlp", "-x", "--audio-format", "mp3",
                     "--no-playlist", "-o", template, job.source],
                    line_callback=on_download_line)
                if self.cancelled:
                    self._set_bar(self.dl_bar, self.dl_label, 0, "cancelled", state="fail")
                    return False
                if rc != 0:
                    self._set_bar(self.dl_bar, self.dl_label, 0, "failed", state="fail")
                    self._log("❌ yt-dlp failed.")
                    return False
                mp3s = sorted(
                    (f for f in os.listdir(out_dir)
                     if f.lower().endswith(".mp3")
                     and os.path.getmtime(os.path.join(out_dir, f)) >= start_mtime),
                    key=lambda f: os.path.getmtime(os.path.join(out_dir, f)))
                if not mp3s:
                    self._log("❌ No mp3 file found after download.")
                    return False
                audio_path = downloaded_audio = os.path.join(out_dir, mp3s[-1])
                self._set_bar(self.dl_bar, self.dl_label, 1.0, "done", state="done")
                self.after(0, lambda: job.text.configure(
                    text=os.path.basename(audio_path)) if job.text else None)

            # ---- Phase 2: transcribe ----
            if self._model_is_cached():
                self._set_bar(self.md_bar, self.md_label, 1.0, "cached", state="skip")
            else:
                self._set_bar(self.md_bar, self.md_label, 0, "downloading…")

            duration = self._get_audio_duration(audio_path)
            if duration is None:
                self._log("ℹ️  ffprobe unavailable — no percentage for transcription.")
            started = time.monotonic()
            state = {"transcribing": False}

            def on_whisper_line(line):
                m = ts_re.search(line)
                if m:
                    state["transcribing"] = True
                    if not duration:
                        return False
                    frac = min(self._parse_timecode(m.group(2)) / duration, 1.0)
                    elapsed = time.monotonic() - started
                    if frac > 0.02:
                        eta = elapsed / frac - elapsed
                        label = f"{int(frac * 100)}% · {self._fmt_duration(eta)} left"
                    else:
                        label = f"{int(frac * 100)}%"
                    self._set_bar(self.tr_bar, self.tr_label, frac, label)
                    return False
                # Model download progress from huggingface_hub's tqdm bars.
                if not state["transcribing"]:
                    p = pct_re.search(line)
                    if p:
                        frac = int(p.group(1)) / 100.0
                        self._set_bar(self.md_bar, self.md_label, frac, f"{p.group(1)}%")
                        return True  # shown in the bar, keep it out of the log
                return False

            formats = [f for f, v in self.format_vars.items() if v.get()]
            fmt_arg = formats[0] if len(formats) == 1 else "all"
            cmd = [self.tools["mlx_whisper"] or "mlx_whisper", audio_path,
                   "--model", self.model.get(),
                   "--output-dir", out_dir,
                   "--task", self.task.get(),
                   "--output-format", fmt_arg]
            if self.language.get() != "Auto-Detect":
                cmd += ["--language", self.language.get()]
            if self.word_timestamps.get():
                cmd += ["--word-timestamps", "True"]
            if not self.condition_on_previous.get():
                cmd += ["--condition-on-previous-text", "False"]

            self._set_bar(self.tr_bar, self.tr_label, 0, "loading model…")
            rc = self._run_stream(cmd, line_callback=on_whisper_line)

            if self.cancelled:
                self._set_bar(self.tr_bar, self.tr_label, 0, "cancelled", state="fail")
                return False
            if rc != 0:
                self._set_bar(self.tr_bar, self.tr_label, 0, "failed", state="fail")
                self._log("❌ mlx_whisper failed.")
                return False

            self._set_bar(self.md_bar, self.md_label, 1.0, "ready", state="done")
            total = self._fmt_duration(time.monotonic() - started)
            self._set_bar(self.tr_bar, self.tr_label, 1.0, f"done in {total}", state="done")

            # ---- Collect outputs ----
            base = os.path.splitext(os.path.basename(audio_path))[0]
            if fmt_arg == "all":
                for ext in {"txt", "srt", "vtt", "json", "tsv"} - set(formats):
                    stale = os.path.join(out_dir, f"{base}.{ext}")
                    if os.path.exists(stale):
                        os.remove(stale)
            produced = [os.path.join(out_dir, f"{base}.{ext}") for ext in formats
                        if os.path.exists(os.path.join(out_dir, f"{base}.{ext}"))]
            job.outputs = produced

            # Only ever delete what we downloaded ourselves.
            if downloaded_audio and not self.keep_audio.get():
                try:
                    os.remove(downloaded_audio)
                    self._log(f"🗑️  Deleted downloaded audio: {os.path.basename(downloaded_audio)}")
                except OSError:
                    pass

            preview = next((p for p in produced if p.endswith(".txt")),
                           produced[0] if produced else None)
            if preview:
                self._register_result(base, preview)
            self._add_history(base, out_dir, produced)

            self.last_output_dir = out_dir
            self._log(f"✅ {base} — done in {total}.")
            return True

        except FileNotFoundError as e:
            self._log(f"❌ Command not found: {e}")
            return False
        except OSError as e:
            self._log(f"❌ {e}")
            return False


if __name__ == "__main__":
    app = TranscriberApp()
    app.mainloop()
