"""Shortcut defaults, validation, and Tk/mpv key-name conversion."""

from __future__ import annotations

import re
from typing import Any


ACTION_LABELS = {
    "open_video": "打开视频",
    "open_subtitle": "添加字幕",
    "toggle_subtitles": "纯英文 / 双语",
    "play_pause": "播放 / 暂停",
    "seek_back": "后退 5 秒",
    "seek_forward": "前进 5 秒",
    "show_words": "打开选词窗口",
    "settings": "打开设置",
}

PLAYER_ACTIONS = ("toggle_subtitles", "play_pause", "seek_back", "seek_forward")
DEFAULT_SHORTCUTS = {
    "open_video": "Ctrl+O",
    "open_subtitle": "Ctrl+L",
    "toggle_subtitles": "Ctrl+B",
    "play_pause": "Space",
    "seek_back": "Left",
    "seek_forward": "Right",
    "show_words": "Ctrl+W",
    "settings": "F2",
}
SPECIAL_KEYS = {"Space", "Left", "Right", "Up", "Down", "Home", "End", "PageUp", "PageDown"}


def normalize_shortcut(value: str) -> str:
    tokens = [token.strip() for token in value.split("+")]
    if not tokens or any(not token for token in tokens):
        raise ValueError("快捷键格式无效")
    mods: set[str] = set()
    for part in tokens[:-1]:
        name = {"control": "Ctrl", "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift"}.get(part.lower())
        if name is None or name in mods:
            raise ValueError("仅支持 Ctrl、Alt、Shift 加字母、数字或功能键")
        mods.add(name)
    key = tokens[-1]
    canonical = {x.lower(): x for x in SPECIAL_KEYS}
    canonical.update({f"f{i}": f"F{i}" for i in range(1, 13)})
    if len(key) == 1 and key.isascii() and key.isalnum():
        key = key.upper()
    elif key.lower() in canonical:
        key = canonical[key.lower()]
    else:
        raise ValueError("请使用字母、数字、方向键、空格或 F1–F12")
    if len(key) == 1 and key.isalpha() and not ({"Ctrl", "Alt"} & mods):
        raise ValueError("字母键请搭配 Ctrl 或 Alt，避免影响播放器输入")
    return "+".join([x for x in ("Ctrl", "Alt", "Shift") if x in mods] + [key])


def validate_shortcuts(changes: dict[str, str]) -> dict[str, str]:
    if set(changes) != set(DEFAULT_SHORTCUTS):
        raise ValueError("快捷键项目不完整")
    normalized = {name: normalize_shortcut(changes[name]) for name in DEFAULT_SHORTCUTS}
    if len(set(normalized.values())) != len(normalized):
        raise ValueError("两个功能不能使用同一个快捷键")
    return normalized


def shortcut_from_event(event: Any) -> str | None:
    key = str(event.keysym)
    if re.match(r"^(?:Control|Shift|Alt|Meta|Super|Win)_[LR]$", key):
        return None
    alias = {"Prior": "PageUp", "Next": "PageDown", "space": "Space"}
    key = alias.get(key, key)
    modifiers = []
    if event.state & 0x4:
        modifiers.append("Ctrl")
    if event.state & 0x8:
        modifiers.append("Alt")
    if event.state & 0x1:
        modifiers.append("Shift")
    try:
        return normalize_shortcut("+".join([*modifiers, key]))
    except ValueError:
        return None


def tkinter_sequence(value: str) -> str:
    tokens = normalize_shortcut(value).split("+")
    key = tokens[-1]
    tk_key = {"Space": "space", "PageUp": "Prior", "PageDown": "Next"}.get(key, key.lower() if len(key) == 1 else key)
    mods = [{"Ctrl": "Control", "Alt": "Alt", "Shift": "Shift"}[x] for x in tokens[:-1]]
    return "<" + "-".join([*mods, tk_key]) + ">"


def mpv_key(value: str) -> str:
    tokens = normalize_shortcut(value).split("+")
    key = tokens[-1]
    key = {"Space": "SPACE", "PageUp": "PGUP", "PageDown": "PGDN"}.get(
        key, key.upper() if key in SPECIAL_KEYS else key.lower() if len(key) == 1 else key
    )
    return "+".join([*tokens[:-1], key])
