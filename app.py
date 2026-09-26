"""Series English: run with `python app.py` (Python 3.10+)."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import tempfile
import threading
import tkinter as tk
import uuid
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from core import (WORD_RE, WordEntry, decode_subtitle, dictionary_links, import_ecdict_csv,
                  language_hint, lookup_word, normalize_word, split_bilingual_ass, suggest_tracks)
from mpv_client import MpvClient
from preferences import (
    ACTION_LABELS, DEFAULT_SHORTCUTS, PLAYER_ACTIONS, mpv_key, shortcut_from_event,
    tkinter_sequence, validate_shortcuts,
)


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path.home() / ".series_english.json"
LEGACY_CONFIG_PATH = Path.home() / ".series_english_demo.json"
COLORS = {
    "page": "#f4f5f3",
    "paper": "#ffffff",
    "ink": "#1f2925",
    "muted": "#68746e",
    "line": "#dfe4df",
    "accent": "#286b55",
    "accent_hover": "#1b513f",
    "light": "#e7f1ec",
}


def format_time(seconds: Any) -> str:
    try:
        n = max(0, int(float(seconds)))
    except (ValueError, TypeError):
        n = 0
    return f"{n // 60:02d}:{n % 60:02d}"


class SeriesEnglishApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("追剧学英语")
        self.root.geometry("800x590")
        self.root.minsize(720, 550)
        self.root.configure(bg=COLORS["page"])
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.client = MpvClient(self.events.put)
        self.tempdir = tempfile.TemporaryDirectory(prefix="series_english_subs_")
        config = self._read_config()
        self.video_path: str | None = None
        self.pending_video: str | None = None
        self.mpv_path = str(config.get("mpv") or shutil.which("mpv") or "")
        try:
            merged_shortcuts = {**DEFAULT_SHORTCUTS, **config.get("shortcuts", {})}
            self.shortcuts = validate_shortcuts(merged_shortcuts)
        except (ValueError, TypeError):
            self.shortcuts = DEFAULT_SHORTCUTS.copy()
        self.word_topmost = tk.BooleanVar(value=bool(config.get("word_topmost", False)))
        self.bound_shortcuts: set[str] = set()
        self.mpv_shortcuts: dict[str, str] = {}
        self.pending_subtitles: list[tuple[Any, ...]] = []
        self.tracks: list[dict[str, Any]] = []
        self.track_signature: tuple[Any, ...] = ()
        self.label_to_id: dict[str, int] = {}
        self.selected_en: int | None = None
        self.selected_zh: int | None = None
        self.preferred_en_path: str | None = None
        self.preferred_zh_path: str | None = None
        self.mode_bilingual = False
        self.applied_track_state: tuple[int | None, int | None, bool] | None = None
        self.english = ""
        self.chinese = ""
        self.pause = False
        self.duration = 0.0
        self.current_word_request = 0
        self.word_cache: dict[str, WordEntry] = {}
        self.selected_word = ""
        self._word_geometry_after: str | None = None
        self.word_size = str(config.get("word_size", ""))
        self._build_ui()
        self._build_word_window()
        self._install_shortcuts()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(80, self._drain_events)

    def _read_config(self) -> dict[str, Any]:
        for path in (CONFIG_PATH, LEGACY_CONFIG_PATH):
            try:
                content = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(content, dict):
                    return content
            except (OSError, ValueError, AttributeError):
                continue
        return {}

    def _save_config(self) -> None:
        try:
            temporary = CONFIG_PATH.with_suffix(".tmp")
            temporary.write_text(json.dumps({
                "mpv": self.mpv_path,
                "shortcuts": self.shortcuts,
                "word_topmost": self.word_topmost.get(),
                "word_size": self.word_size,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(CONFIG_PATH)
        except OSError:
            pass

    def _button(self, parent: tk.Misc, text: str, command: Any, *, primary: bool = False) -> tk.Button:
        return tk.Button(
            parent, text=text, command=command, relief="flat", bd=0, cursor="hand2",
            font=("Microsoft YaHei UI", 10, "bold" if primary else "normal"),
            padx=13, pady=9,
            bg=COLORS["accent"] if primary else COLORS["paper"],
            fg=COLORS["paper"] if primary else COLORS["ink"],
            activebackground=COLORS["accent_hover"] if primary else COLORS["light"],
            activeforeground=COLORS["paper"] if primary else COLORS["ink"],
        )

    def _build_ui(self) -> None:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Subtitle.TCombobox", padding=7, font=("Microsoft YaHei UI", 10),
                        fieldbackground=COLORS["paper"], background=COLORS["paper"],
                        foreground=COLORS["ink"], bordercolor=COLORS["line"])

        container = tk.Frame(self.root, bg=COLORS["page"], padx=26, pady=22)
        container.pack(fill="both", expand=True)
        header = tk.Frame(container, bg=COLORS["page"])
        header.pack(fill="x", pady=(0, 20))
        tk.Label(header, text="追剧学英语", font=("Microsoft YaHei UI", 21, "bold"),
                 bg=COLORS["page"], fg=COLORS["ink"]).pack(side="left")
        tk.Label(header, text="字幕学习", font=("Microsoft YaHei UI", 10),
                 bg=COLORS["page"], fg=COLORS["muted"]).pack(side="left", padx=14, pady=(10, 0))

        toolbar = tk.Frame(container, bg=COLORS["page"])
        toolbar.pack(fill="x", pady=(0, 16))
        self._button(toolbar, "打开视频", self.open_video, primary=True).pack(side="left", padx=(0, 8))
        self._button(toolbar, "添加字幕", self.open_subtitle).pack(side="left", padx=(0, 8))
        self._button(toolbar, "选词窗口", self.show_word_window).pack(side="left")
        self._button(toolbar, "设置", self.show_settings).pack(side="right")

        media = tk.Frame(container, bg=COLORS["paper"], padx=18, pady=13)
        media.pack(fill="x", pady=(0, 12))
        tk.Label(media, text="当前视频", bg=COLORS["paper"], fg=COLORS["muted"],
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        self.video_var = tk.StringVar(value="尚未选择视频")
        tk.Label(media, textvariable=self.video_var, bg=COLORS["paper"], fg=COLORS["ink"],
                 font=("Microsoft YaHei UI", 11), anchor="w", justify="left",
                 wraplength=680).pack(fill="x", pady=(4, 0))

        tracks_box = tk.Frame(container, bg=COLORS["paper"], padx=18, pady=15)
        tracks_box.pack(fill="x", pady=(0, 12))
        tracks_box.columnconfigure(1, weight=1)
        tk.Label(tracks_box, text="字幕轨道", font=("Microsoft YaHei UI", 12, "bold"),
                 bg=COLORS["paper"], fg=COLORS["ink"]).grid(row=0, column=0, sticky="w", pady=(0, 12))
        for row, title in ((1, "英文"), (2, "中文")):
            tk.Label(tracks_box, text=title, bg=COLORS["paper"], fg=COLORS["ink"],
                     font=("Microsoft YaHei UI", 10)).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=5)
        self.en_choice = ttk.Combobox(tracks_box, state="readonly", style="Subtitle.TCombobox")
        self.zh_choice = ttk.Combobox(tracks_box, state="readonly", style="Subtitle.TCombobox")
        self.en_choice.grid(row=1, column=1, sticky="ew", pady=5)
        self.zh_choice.grid(row=2, column=1, sticky="ew", pady=5)
        self.en_choice.bind("<<ComboboxSelected>>", self._pick_en)
        self.zh_choice.bind("<<ComboboxSelected>>", self._pick_zh)
        tk.Label(tracks_box, text="识别不准确时可手动选择；没有中文轨道可只看英文。",
                 bg=COLORS["paper"], fg=COLORS["muted"],
                 font=("Microsoft YaHei UI", 9)).grid(row=3, column=1, sticky="w", pady=(7, 0))

        controls = tk.Frame(container, bg=COLORS["paper"], padx=18, pady=13)
        controls.pack(fill="x")
        self.mode_button = self._button(controls, "切换到双语", self.toggle_mode, primary=True)
        self.mode_button.pack(side="left", padx=(0, 16))
        self.mode_button.configure(state="disabled")
        self._button(controls, "−5 秒", lambda: self.client.send("seek", -5, "relative")).pack(side="left")
        self.play_button = self._button(controls, "暂停", self.toggle_pause)
        self.play_button.pack(side="left", padx=5)
        self._button(controls, "+5 秒", lambda: self.client.send("seek", 5, "relative")).pack(side="left")
        self.time_label = tk.Label(controls, text="00:00 / 00:00", bg=COLORS["paper"],
                                   fg=COLORS["muted"], font=("Segoe UI", 10))
        self.time_label.pack(side="right")

        self.status_var = tk.StringVar(value="请先安装 mpv，再打开视频。")
        tk.Label(container, textvariable=self.status_var, bg=COLORS["page"], fg=COLORS["muted"],
                 font=("Microsoft YaHei UI", 9), anchor="w", wraplength=710).pack(
            side="bottom", fill="x", pady=(16, 0)
        )

    def _build_word_window(self) -> None:
        self.word_window = tk.Toplevel(self.root)
        self.word_window.title("选词 · 追剧学英语")
        word_x = min(850, max(50, self.root.winfo_screenwidth() - 600))
        match = re.fullmatch(r"(\d{3,4})x(\d{3,4})", self.word_size)
        word_width = min(max(int(match[1]), 400), 1200) if match else 550
        word_height = min(max(int(match[2]), 340), 1000) if match else 480
        self.word_window.geometry(f"{word_width}x{word_height}+{word_x}+130")
        self.word_window.minsize(400, 340)
        self.word_window.resizable(True, True)
        self.word_window.configure(bg=COLORS["paper"])
        self.word_window.attributes("-topmost", self.word_topmost.get())
        self.word_window.protocol("WM_DELETE_WINDOW", self.word_window.withdraw)
        panel = tk.Frame(self.word_window, bg=COLORS["paper"], padx=16, pady=12)
        panel.grid(row=0, column=0, sticky="nsew")
        self.word_window.rowconfigure(0, weight=1)
        self.word_window.columnconfigure(0, weight=1)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(4, weight=1, minsize=100)
        head = tk.Frame(panel, bg=COLORS["paper"])
        head.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        tk.Label(head, text="选词释义", bg=COLORS["paper"], fg=COLORS["ink"],
                 font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
        tk.Checkbutton(head, text="保持在最上层", variable=self.word_topmost,
                       command=self._set_word_topmost, relief="flat", bd=0,
                       bg=COLORS["paper"], activebackground=COLORS["paper"],
                       fg=COLORS["muted"], font=("Microsoft YaHei UI", 9)).pack(side="right")
        for label, steps in (("＋", 1), ("－", -1)):
            tk.Button(head, text=label, command=lambda amount=steps: self._resize_word_window(amount),
                      relief="flat", bd=0, width=2, cursor="hand2", bg=COLORS["page"],
                      fg=COLORS["ink"], activebackground=COLORS["light"],
                      font=("Segoe UI", 10)).pack(side="right", padx=(0, 5))
        subtitle_area = tk.Frame(panel, bg=COLORS["page"])
        subtitle_area.grid(row=1, column=0, sticky="ew")
        self.en_text = tk.Text(subtitle_area, height=2, wrap="word", font=("Segoe UI", 13),
                               padx=10, pady=5, relief="flat", bd=0,
                               background=COLORS["page"], foreground=COLORS["ink"], cursor="hand2")
        self.en_text.pack(side="left", fill="x", expand=True)
        subtitle_scroll = tk.Scrollbar(subtitle_area, command=self.en_text.yview)
        subtitle_scroll.pack(side="right", fill="y")
        self.en_text.configure(yscrollcommand=subtitle_scroll.set)
        self.en_text.tag_configure("picked", background=COLORS["light"])
        self.en_text.configure(state="disabled")
        self.en_text.bind("<ButtonRelease-1>", self._word_clicked)
        self.zh_var = tk.StringVar(value="中文将在双语模式下显示")
        zh_label = tk.Label(panel, textvariable=self.zh_var, bg=COLORS["paper"], fg=COLORS["muted"],
                            font=("Microsoft YaHei UI", 10), anchor="w", wraplength=500, justify="left")
        zh_label.grid(row=2, column=0, sticky="ew", pady=(5, 8))
        self.lookup_var = tk.StringVar(value="点击英文字幕中的单词查看释义")
        tk.Label(panel, textvariable=self.lookup_var, bg=COLORS["paper"], fg=COLORS["accent"],
                 font=("Microsoft YaHei UI", 11, "bold"), anchor="w").grid(
            row=3, column=0, sticky="ew", pady=(0, 6))
        result_area = tk.Frame(panel, bg=COLORS["paper"], highlightthickness=1,
                               highlightbackground=COLORS["line"])
        result_area.grid(row=4, column=0, sticky="nsew")
        self.definition_text = tk.Text(result_area, wrap="word", state="disabled", relief="flat",
                                       bd=0, font=("Microsoft YaHei UI", 10), bg=COLORS["paper"],
                                       fg=COLORS["ink"], padx=10, pady=8, spacing2=3)
        self.definition_text.pack(side="left", fill="both", expand=True)
        result_scroll = tk.Scrollbar(result_area, command=self.definition_text.yview)
        result_scroll.pack(side="right", fill="y")
        self.definition_text.configure(yscrollcommand=result_scroll.set)
        self.definition_text.tag_configure("section", foreground=COLORS["accent"],
                                           font=("Microsoft YaHei UI", 10, "bold"))
        self.definition_text.tag_configure("secondary", foreground=COLORS["muted"])
        self.definition_text.tag_configure("chinese", foreground=COLORS["accent"])
        links = tk.Frame(panel, bg=COLORS["paper"])
        links.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        tk.Label(links, text="详细释义", bg=COLORS["paper"], fg=COLORS["muted"],
                 font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(0, 12))
        for name in ("有道词典", "剑桥词典"):
            label = tk.Label(links, text=name + " ↗", bg=COLORS["paper"], fg=COLORS["accent"],
                             font=("Microsoft YaHei UI", 9, "underline"), cursor="hand2")
            label.pack(side="left", padx=(0, 13))
            label.bind("<Button-1>", lambda _event, provider=name: self._open_dictionary(provider))
        panel.bind("<Configure>", lambda event: zh_label.configure(wraplength=max(150, event.width - 32)))
        self.word_window.bind("<Configure>", self._remember_word_size)
        self.word_window.withdraw()

    def _remember_word_size(self, event: tk.Event) -> None:
        if event.widget is not self.word_window or self.word_window.state() != "normal":
            return
        size = f"{event.width}x{event.height}"
        if size == self.word_size or event.width < 400 or event.height < 340:
            return
        self.word_size = size
        if self._word_geometry_after is not None:
            self.root.after_cancel(self._word_geometry_after)
        self._word_geometry_after = self.root.after(450, self._save_config)

    def _resize_word_window(self, steps: int) -> None:
        window = self.word_window
        width = max(400, min(window.winfo_width() + 80 * steps, window.winfo_screenwidth() - 30))
        height = max(340, min(window.winfo_height() + 70 * steps, window.winfo_screenheight() - 70))
        window.geometry(f"{width}x{height}")

    def _open_dictionary(self, provider: str) -> None:
        if self.selected_word:
            webbrowser.open_new_tab(dictionary_links(self.selected_word)[provider])

    def _show_word_entry(self, entry: WordEntry) -> None:
        self.lookup_var.set(entry.word + (f"  /{entry.phonetic.strip('/')}/" if entry.phonetic else ""))
        widget = self.definition_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        if entry.chinese_groups:
            widget.insert("end", "中文词义（ECDICT）\n", "section")
            for group in entry.chinese_groups:
                widget.insert("end", f"\n{group.part_of_speech}\n", "section")
                for number, sense in enumerate(group.senses, 1):
                    widget.insert("end", f"{number}. {sense}\n", "chinese")
            widget.insert("end", "\n")
        if entry.chinese:
            widget.insert("end", "中文速览（参考）\n", "section")
            for chinese in entry.chinese:
                widget.insert("end", f"• {chinese}\n", "chinese")
            widget.insert("end", "\n")
        if entry.meanings:
            widget.insert("end", "按词性查看释义\n", "section")
            for group in entry.meanings:
                widget.insert("end", f"\n{group.part_of_speech}\n", "section")
                for number, sense in enumerate(group.senses, 1):
                    widget.insert("end", f"{number}. {sense.definition}\n")
                    if sense.chinese:
                        widget.insert("end", f"   ↳ {sense.chinese}（机器翻译参考）\n", "chinese")
                    if sense.example:
                        widget.insert("end", f"   例句：{sense.example}\n", "secondary")
        elif not entry.chinese and not entry.chinese_groups:
            widget.insert("end", "暂未找到释义。可点击下方词典继续查询。")
        widget.insert("end", f"\n来源：{entry.source}\n", "secondary")
        widget.configure(state="disabled")
        widget.yview_moveto(0)

    def _show_lookup_message(self, message: str) -> None:
        self.definition_text.configure(state="normal")
        self.definition_text.delete("1.0", "end")
        self.definition_text.insert("1.0", message)
        self.definition_text.configure(state="disabled")
        self.definition_text.yview_moveto(0)

    def show_word_window(self) -> None:
        self.word_window.deiconify()
        self.word_window.lift()

    def _set_word_topmost(self) -> None:
        self.word_window.attributes("-topmost", self.word_topmost.get())
        self._save_config()

    def _install_shortcuts(self) -> None:
        for sequence in self.bound_shortcuts:
            self.root.unbind_all(sequence)
        actions = {
            "open_video": self.open_video,
            "open_subtitle": self.open_subtitle,
            "toggle_subtitles": self.toggle_mode,
            "play_pause": self.toggle_pause,
            "seek_back": lambda: self.client.send("seek", -5, "relative"),
            "seek_forward": lambda: self.client.send("seek", 5, "relative"),
            "show_words": self.show_word_window,
            "settings": self.show_settings,
        }
        self.bound_shortcuts = set()
        for action, shortcut in self.shortcuts.items():
            sequence = tkinter_sequence(shortcut)
            def run(event: tk.Event, operation: str = action) -> str | None:
                if operation in {"play_pause", "seek_back", "seek_forward"} and isinstance(
                    event.widget, (tk.Entry, ttk.Entry, ttk.Combobox,
                                   tk.Button, ttk.Button, tk.Checkbutton, ttk.Checkbutton)
                ):
                    return None
                actions[operation]()
                return "break"
            self.root.bind_all(sequence, run)
            self.bound_shortcuts.add(sequence)
        self._update_mode_label()

    def _install_mpv_shortcuts(self) -> None:
        commands = {
            "toggle_subtitles": "cycle secondary-sub-visibility",
            "play_pause": "cycle pause",
            "seek_back": "seek -5 relative",
            "seek_forward": "seek 5 relative",
        }
        for action in PLAYER_ACTIONS:
            old = self.mpv_shortcuts.get(action)
            new = self.shortcuts[action]
            if old and old != new:
                self.client.send("keybind", mpv_key(old), "ignore")
        for action in PLAYER_ACTIONS:
            new = self.shortcuts[action]
            self.client.send("keybind", mpv_key(new), commands[action])
            self.mpv_shortcuts[action] = new

    def show_settings(self) -> None:
        if getattr(self, "settings_window", None) and self.settings_window.winfo_exists():
            self.settings_window.lift()
            return
        dialog = tk.Toplevel(self.root)
        self.settings_window = dialog
        dialog.title("设置")
        dialog.geometry("520x680")
        dialog.configure(bg=COLORS["paper"])
        dialog.transient(self.root)
        if self.word_topmost.get():
            dialog.attributes("-topmost", True)
        frame = tk.Frame(dialog, bg=COLORS["paper"], padx=24, pady=20)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="快捷键", bg=COLORS["paper"], fg=COLORS["ink"],
                 font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
        tk.Label(frame, text="点选右侧输入框，再按想要的组合键。播放操作在 mpv 窗口中也有效。",
                 bg=COLORS["paper"], fg=COLORS["muted"],
                 font=("Microsoft YaHei UI", 9), wraplength=470).pack(anchor="w", pady=(5, 15))
        rows = tk.Frame(frame, bg=COLORS["paper"])
        rows.pack(fill="x")
        rows.columnconfigure(1, weight=1)
        inputs: dict[str, tk.StringVar] = {}
        info = tk.StringVar(value="")
        for row, (action, name) in enumerate(ACTION_LABELS.items()):
            tk.Label(rows, text=name, bg=COLORS["paper"], fg=COLORS["ink"],
                     font=("Microsoft YaHei UI", 10)).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 14))
            value = tk.StringVar(value=self.shortcuts[action])
            inputs[action] = value
            field = tk.Entry(rows, textvariable=value, readonlybackground=COLORS["page"],
                             state="readonly", relief="flat", bd=0,
                             font=("Segoe UI", 11), fg=COLORS["ink"])
            field.grid(row=row, column=1, sticky="ew", ipady=7, pady=5)
            def capture(event: tk.Event, output: tk.StringVar = value) -> str:
                shortcut = shortcut_from_event(event)
                if shortcut:
                    output.set(shortcut)
                    info.set("")
                elif event.keysym not in {"Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R"}:
                    info.set("这个按键暂不支持，请换字母、数字、方向键或 F1–F12。")
                return "break"
            field.bind("<KeyPress>", capture)
        tk.Checkbutton(frame, text="选词窗口保持在最上层", variable=self.word_topmost,
                       command=self._set_word_topmost, relief="flat", bd=0,
                       bg=COLORS["paper"], activebackground=COLORS["paper"],
                       fg=COLORS["ink"], font=("Microsoft YaHei UI", 10)).pack(
            anchor="w", pady=(13, 0)
        )
        tk.Label(frame, text="英汉词库", bg=COLORS["paper"], fg=COLORS["ink"],
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(15, 3))
        tk.Label(frame, text="可导入 ECDICT CSV，在本机查询多词性中文释义。",
                 bg=COLORS["paper"], fg=COLORS["muted"],
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        self._button(frame, "导入 ECDICT CSV…", self.import_dictionary).pack(anchor="w", pady=(8, 0))
        tk.Label(frame, textvariable=info, bg=COLORS["paper"], fg="#a13939",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(12, 8))
        bottom = tk.Frame(frame, bg=COLORS["paper"])
        bottom.pack(fill="x", side="bottom")
        def save() -> None:
            try:
                self.shortcuts = validate_shortcuts({action: value.get() for action, value in inputs.items()})
            except ValueError as exc:
                info.set(str(exc))
                return
            self._save_config()
            self._install_shortcuts()
            if self.client.ready:
                self._install_mpv_shortcuts()
            dialog.destroy()
            self.status_var.set("快捷键已保存")
        self._button(bottom, "保存设置", save, primary=True).pack(side="right")
        self._button(bottom, "恢复默认", lambda: [inputs[k].set(v) for k, v in DEFAULT_SHORTCUTS.items()]).pack(side="left")
        self._button(bottom, "选择 mpv.exe", self.choose_mpv).pack(side="left", padx=8)

    def choose_mpv(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择 mpv.exe", filetypes=[("mpv 程序", "mpv.exe"), ("所有文件", "*")]
        )
        if selected:
            self.mpv_path = selected
            self._save_config()
            self.status_var.set("已设置 mpv：" + selected)

    def import_dictionary(self) -> None:
        filename = filedialog.askopenfilename(
            title="选择 ECDICT 词库 CSV", filetypes=[("CSV 词库", "*.csv"), ("所有文件", "*")]
        )
        if not filename:
            return
        self.status_var.set("正在导入英汉词库，可继续使用播放器……")

        def worker() -> None:
            try:
                count = import_ecdict_csv(Path(filename))
                self.events.put({"event": "dictionary-imported", "count": count})
            except (OSError, UnicodeError, ValueError) as exc:
                self.events.put({"event": "client-error", "message": f"导入英汉词库失败：{exc}"})

        threading.Thread(target=worker, daemon=True).start()

    def _ensure_mpv(self) -> bool:
        if self.client.process is not None and self.client.process.poll() is None:
            return True
        binary = self.mpv_path or shutil.which("mpv") or ""
        if not (shutil.which(binary) or (binary and Path(binary).is_file())):
            messagebox.showinfo("需要 mpv", "先从 mpv.io 安装 mpv，再点击“设置 mpv”选择 mpv.exe。")
            self.choose_mpv()
            binary = self.mpv_path
        if not binary:
            return False
        try:
            self.client.start(binary)
        except OSError as exc:
            messagebox.showerror("无法启动 mpv", str(exc))
            return False
        self.status_var.set("正在启动 mpv……")
        return True

    def open_video(self) -> None:
        filename = filedialog.askopenfilename(
            title="选择本地视频",
            filetypes=[("视频", "*.mkv *.mp4 *.avi *.webm *.mov"), ("所有文件", "*")],
            initialdir=str(APP_DIR / "samples"),
        )
        if filename:
            self.load_video(filename)

    def load_video(self, filename: str) -> None:
        if not Path(filename).is_file() or not self._ensure_mpv():
            return
        self.video_path = str(Path(filename).resolve())
        self.video_var.set(Path(self.video_path).name)
        self.pending_video = self.video_path
        self.pending_subtitles.clear()
        self.tracks = []
        self.track_signature = ()
        self.selected_en = self.selected_zh = None
        self.applied_track_state = None
        self.preferred_en_path = self.preferred_zh_path = None
        self.english = self.chinese = ""
        self._set_reading(self.en_text, "")
        self.zh_var.set("切换到双语后显示")
        self._render_choices()
        if self.client.ready:
            self._send_pending_video()

    def _send_pending_video(self) -> None:
        if self.pending_video:
            if self.client.send("loadfile", self.pending_video, "replace"):
                self.status_var.set("已打开：" + Path(self.pending_video).name + " · 等待字幕轨道")
                self.pending_video = None

    def open_subtitle(self) -> None:
        if not self.video_path:
            messagebox.showinfo("先打开视频", "请选择视频后再加载外挂字幕。")
            return
        filename = filedialog.askopenfilename(
            title="选择 ASS / SRT 字幕",
            filetypes=[("字幕", "*.ass *.ssa *.srt *.vtt"), ("所有文件", "*")],
            initialdir=str(Path(self.video_path).parent),
        )
        if filename:
            self.load_subtitle(filename)

    def load_subtitle(self, filename: str) -> None:
        path = Path(filename)
        if not path.is_file():
            return
        current_video = self.video_path
        self.status_var.set("正在读取字幕：" + path.name)

        def worker() -> None:
            try:
                if path.suffix.lower() in {".ass", ".ssa"}:
                    split = split_bilingual_ass(decode_subtitle(path.read_bytes()))
                    if split is not None:
                        en_path, zh_path = self._prepare_split_subtitles(split)
                        self.events.put({"event": "subtitle-split-ready", "path": current_video,
                                         "en": str(en_path), "zh": str(zh_path),
                                         "stem": path.stem, "count": split.bilingual_events})
                        return
                self.events.put({"event": "subtitle-ready", "path": current_video,
                                 "filename": str(path.resolve())})
            except (OSError, UnicodeError, ValueError) as exc:
                self.events.put({"event": "client-error", "message": f"读取字幕失败：{exc}"})

        threading.Thread(target=worker, daemon=True).start()

    def _load_single_subtitle(self, filename: str) -> None:
        path = Path(filename)
        hint = language_hint({"title": path.stem})
        if hint == "en":
            self.preferred_en_path = str(path.resolve())
        elif hint == "zh":
            self.preferred_zh_path = str(path.resolve())
        self._enqueue_subtitle(str(path.resolve()), path.name, hint or "und")
        self.status_var.set("已加载字幕。若语言没有自动识别，请从下拉框选择。")

    def _prepare_split_subtitles(self, split: Any) -> tuple[Path, Path]:
        unique = "split_" + uuid.uuid4().hex
        en_path = Path(self.tempdir.name) / (unique + "_en.ass")
        zh_path = Path(self.tempdir.name) / (unique + "_zh.ass")
        en_path.write_text(split.english, encoding="utf-8-sig")
        zh_path.write_text(split.chinese, encoding="utf-8-sig")
        return en_path, zh_path

    def _enqueue_subtitle(self, path: str, title: str, language: str) -> None:
        command = ("sub-add", path, "auto", title, language)
        if not self.client.send(*command):
            self.pending_subtitles.append(command)

    def _load_split_paths(self, en_path: str, zh_path: str, stem: str) -> None:
        self.preferred_en_path = str(en_path)
        self.preferred_zh_path = str(zh_path)
        self._enqueue_subtitle(str(en_path), stem + " · English", "en")
        self._enqueue_subtitle(str(zh_path), stem + " · 中文", "zh")

    def _render_choices(self) -> None:
        self.label_to_id.clear()
        labels = ["未选择"]
        for track in self.tracks:
            if track.get("type") != "sub" or not isinstance(track.get("id"), int):
                continue
            name = track.get("title") or Path(track.get("external-filename") or "").name or "无标题"
            lang = track.get("lang") or "未知"
            label = f"#{track['id']}  [{lang}]  {name}  ({track.get('codec') or 'subtitle'})"
            self.label_to_id[label] = track["id"]
            labels.append(label)
        self.en_choice["values"] = labels
        self.zh_choice["values"] = labels
        self.en_choice.set(next((s for s in labels if self.label_to_id.get(s) == self.selected_en), labels[0]))
        self.zh_choice.set(next((s for s in labels if self.label_to_id.get(s) == self.selected_zh), labels[0]))
        self.mode_button.configure(
            state="normal" if self.selected_en is not None and self.selected_zh is not None
            else "disabled"
        )

    def _update_tracks(self, tracks: Any) -> None:
        if not isinstance(tracks, list):
            return
        signature = tuple(
            (t.get("id"), t.get("type"), t.get("title"), t.get("lang"),
             t.get("codec"), t.get("external-filename"))
            for t in tracks if isinstance(t, dict)
        )
        if signature == self.track_signature:
            return
        self.track_signature = signature
        self.tracks = tracks
        available = {t.get("id") for t in tracks if t.get("type") == "sub"}
        guessed_en, guessed_zh = suggest_tracks(tracks)
        if self.selected_en not in available:
            self.selected_en = guessed_en
        elif self.selected_en is None and guessed_en is not None:
            self.selected_en = guessed_en
        if self.selected_zh not in available:
            self.selected_zh = guessed_zh
        elif self.selected_zh is None and guessed_zh is not None:
            self.selected_zh = guessed_zh
        for track in tracks:
            p = track.get("external-filename")
            if p and self.preferred_en_path and os.path.normcase(os.path.abspath(p)) == os.path.normcase(self.preferred_en_path):
                self.selected_en = track.get("id")
            if p and self.preferred_zh_path and os.path.normcase(os.path.abspath(p)) == os.path.normcase(self.preferred_zh_path):
                self.selected_zh = track.get("id")
        if self.selected_en == self.selected_zh:
            self.selected_zh = None
        self._render_choices()
        self._apply_tracks()

    def _pick_en(self, _: tk.Event) -> None:
        self.selected_en = self.label_to_id.get(self.en_choice.get())
        if self.selected_en == self.selected_zh:
            self.selected_zh = None
        self.preferred_en_path = None
        self._render_choices()
        self._apply_tracks()

    def _pick_zh(self, _: tk.Event) -> None:
        self.selected_zh = self.label_to_id.get(self.zh_choice.get())
        if self.selected_zh == self.selected_en:
            self.selected_zh = None
        self.preferred_zh_path = None
        self._render_choices()
        self._apply_tracks()

    def _apply_tracks(self) -> None:
        intended = (self.selected_en, self.selected_zh, self.mode_bilingual)
        if self.applied_track_state == intended:
            return
        self.applied_track_state = intended
        if self.selected_en is None:
            self.client.send("set_property", "sid", "no")
        else:
            self.client.send("set_property", "sid", self.selected_en)
        self.client.send("set_property", "secondary-sid", self.selected_zh or "no")
        self.client.send("set_property", "sub-visibility", True)
        self.client.send("set_property", "secondary-sub-visibility", bool(self.mode_bilingual and self.selected_zh))
        self._update_mode_label()

    def _update_mode_label(self) -> None:
        shortcut = self.shortcuts["toggle_subtitles"]
        self.mode_button.configure(text=("切换到纯英文" if self.mode_bilingual else "切换到双语") +
                                   "  ·  " + shortcut)
        self.zh_var.set(self.chinese if self.mode_bilingual and self.chinese else "中文将在双语模式下显示")

    def toggle_mode(self) -> None:
        if self.selected_en is None or self.selected_zh is None:
            self.status_var.set("请先指定英文与中文轨道；缺少字幕时可加载外挂 ASS/SRT。")
            return
        self.mode_bilingual = not self.mode_bilingual
        self._apply_tracks()
        self.status_var.set("双语字幕" if self.mode_bilingual else "纯英文字幕")

    def toggle_pause(self) -> None:
        self.client.send("set_property", "pause", not self.pause)

    def _set_reading(self, widget: tk.Text, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def _word_clicked(self, event: tk.Event) -> None:
        content = self.english
        if not content:
            return
        index = self.en_text.index(f"@{event.x},{event.y}")
        offset = self.en_text.count("1.0", index, "chars")[0]
        match = next((m for m in WORD_RE.finditer(content) if m.start() <= offset < m.end()), None)
        if match is None:
            return
        self.en_text.tag_remove("picked", "1.0", "end")
        start = self.en_text.index(f"1.0+{match.start()}c")
        end = self.en_text.index(f"1.0+{match.end()}c")
        self.en_text.tag_add("picked", start, end)
        word = match.group()
        self.selected_word = normalize_word(word)
        self.lookup_var.set(f"{word}  ·  正在查询……")
        self._show_lookup_message("正在获取多词性释义和中文参考……")
        self.current_word_request += 1
        ticket = self.current_word_request
        if word.lower() in self.word_cache:
            self._show_word_entry(self.word_cache[word.lower()])
            return

        def worker() -> None:
            try:
                entry = lookup_word(word)
                self.events.put({"event": "translation", "ticket": ticket,
                                 "word": word, "entry": entry})
            except Exception as exc:
                self.events.put({"event": "translation", "ticket": ticket,
                                 "word": word, "error": str(exc)})

        threading.Thread(target=worker, daemon=True).start()

    def _drain_events(self) -> None:
        try:
            for _ in range(150):
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                kind = event.get("event")
                if kind == "connected":
                    self.mpv_shortcuts.clear()
                    self._install_mpv_shortcuts()
                    self._send_pending_video()
                    for command in self.pending_subtitles:
                        self.client.send(*command)
                    self.pending_subtitles.clear()
                elif kind == "property-change":
                    name, data = event.get("name"), event.get("data")
                    if name == "track-list":
                        self._update_tracks(data)
                    elif name == "sub-text":
                        self.english = str(data or "").strip()
                        self._set_reading(self.en_text, self.english)
                    elif name == "secondary-sub-text":
                        self.chinese = str(data or "").strip()
                        if self.mode_bilingual:
                            self.zh_var.set(self.chinese)
                    elif name == "time-pos":
                        self.time_label.configure(text=f"{format_time(data)} / {format_time(self.duration)}")
                    elif name == "duration":
                        self.duration = float(data or 0)
                    elif name == "pause":
                        self.pause = bool(data)
                        self.play_button.config(text="播放" if self.pause else "暂停")
                    elif name == "secondary-sub-visibility":
                        if self.selected_en is not None and self.selected_zh is not None:
                            self.mode_bilingual = bool(data)
                            self.applied_track_state = (self.selected_en, self.selected_zh, self.mode_bilingual)
                            self._update_mode_label()
                elif kind == "translation" and event["ticket"] == self.current_word_request:
                    if "error" in event:
                        self.lookup_var.set(event["word"] + "  ·  查询失败")
                        self._show_lookup_message(str(event["error"]))
                    else:
                        self.word_cache[event["word"].lower()] = event["entry"]
                        self._show_word_entry(event["entry"])
                elif kind == "dictionary-imported":
                    self.word_cache.clear()
                    self.current_word_request += 1
                    self.lookup_var.set("词库已更新 · 重新点击单词查询")
                    self.status_var.set(f"英汉词库已导入 {event['count']} 个词条，重新选词即可使用。")
                elif kind == "subtitle-split-ready" and event["path"] == self.video_path:
                    self._load_split_paths(event["en"], event["zh"], event["stem"])
                    self.status_var.set(f"已拆分 {event['count']} 条双行英中字幕。")
                elif kind == "subtitle-ready" and event["path"] == self.video_path:
                    self._load_single_subtitle(event["filename"])
                elif kind == "client-error":
                    self.status_var.set(event.get("message", "发生错误"))
                elif kind == "disconnected":
                    self.status_var.set("mpv 已退出。再次打开视频可重新启动。")
        finally:
            self.root.after(80, self._drain_events)

    def close(self) -> None:
        self.client.stop()
        self.tempdir.cleanup()
        self.word_window.destroy()
        self.root.destroy()


if __name__ == "__main__":
    window = tk.Tk()
    SeriesEnglishApp(window)
    window.mainloop()
