"""Settings and file locations.

Program files (code, icon, bundled Tesseract) live where the app is
installed; each user's own data (settings.json, nameocr.log, debug_frames)
lives in %APPDATA%\\NameOCR so updates never touch it.
"""
import json
import os
import re
import shutil
import sys

VERSION = "1.0.3"
REPO = ""            # "owner/name" on GitHub, for the update check ("" = off)

HERE = os.path.dirname(os.path.abspath(__file__))
# bundled resources: next to the code, or inside the packaged app
RES = getattr(sys, "_MEIPASS", HERE)
# running from source (the "workshop" copy) rather than the installed app:
# its own taskbar identity and "(dev)" in the title, so the two never get mixed up
DEV = not getattr(sys, "frozen", False)
APP_ID = "NameOCR.Dev" if DEV else "NameOCR"


def _data_dir():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    d = os.path.join(base, "NameOCR")
    os.makedirs(d, exist_ok=True)
    return d


DATA = _data_dir()
PATH = os.path.join(DATA, "settings.json")
# the log and saved frames; a dev copy keeps its own next to the code, so test
# scans stay with the code being tested (settings are still shared)
LOGS = HERE if DEV else DATA
LOG = os.path.join(LOGS, "nameocr.log")
FRAMES = os.path.join(LOGS, "debug_frames")

# settings from before they moved to AppData (kept next to the code)
_old = os.path.join(HERE, "settings.json")
if not os.path.exists(PATH) and os.path.exists(_old):
    try:
        shutil.copyfile(_old, PATH)
    except OSError:
        pass

DEFAULTS = {
    "group_id": "",               # the group checked on the next scan ("" = none)
    "groups": [],                 # saved groups: [{"id": 123, "name": "..."}]
    "hotkey_scan": "F8",
    "hotkey_copy": "F9",
    "monitor": 1,                 # mss monitor index (1 = first display)
    "region": None,               # {"left","top","width","height"} in that display's pixels
    "tesseract_cmd": "",
    "exclude_not_in_group": True,
    "beep": True,
    "bring_to_front": True,
    "save_debug_frames": False,
    "show_tooltips": False,       # popups with details when hovering a name
    "show_scores": False,         # show each name's evidence score in the table
}

# Tuning knobs (Settings > Tuning). Each Settings tab has its own
# "Reset ... to defaults" that only touches that tab.
TUNING = {
    "scan_fps": 30,
    "filter_position": True,
    "min_frames": 2,
    "row_colour_tolerance": 3,
    "display_match": 80,            # % display-name similarity to auto-verify
    "avatar_tiebreak": True,
    "search_depth": 125,            # lookalike spellings tried per name
    "auto_stop_minutes": 5,         # a forgotten scan stops itself after this long
}
BEHAVIOUR = ("exclude_not_in_group", "beep", "bring_to_front", "save_debug_frames", "show_tooltips",
             "show_scores")
DEFAULTS.update(TUNING)


def load() -> dict:
    cfg = dict(DEFAULTS)
    cfg["groups"] = []
    try:
        with open(PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    # older settings had only one group: keep it as a saved group
    gid = parse_group_id(cfg["group_id"])
    if gid and not any(g["id"] == gid for g in cfg["groups"]):
        cfg["groups"].append({"id": gid, "name": ""})
    return cfg


def group_label(g: dict) -> str:
    return f"{g['name']}  ({g['id']})" if g.get("name") else f"Group {g['id']}"


def save(cfg: dict):
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, PATH)


def parse_group_id(text: str):
    """Accepts '1234567' or a group/community link; returns int or None."""
    m = re.search(r"\d{3,}", text or "")
    return int(m.group()) if m else None


def bundled_tesseract() -> str:
    p = os.path.join(RES, "tesseract", "tesseract.exe")
    return p if os.path.isfile(p) else ""


def find_tesseract(configured: str = "") -> str:
    cands = [configured, bundled_tesseract(), shutil.which("tesseract") or "",
             r"C:\Program Files\Tesseract-OCR\tesseract.exe",
             r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
             os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe")]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


# Qt key-sequence text ("Ctrl+Shift+S", "F8") -> pynput hotkey ("<ctrl>+<shift>+s")
_NAMED = {"space": "space", "ins": "insert", "del": "delete", "home": "home",
          "end": "end", "pgup": "page_up", "pgdown": "page_down",
          "pause": "pause", "scrolllock": "scroll_lock", "esc": "esc",
          "tab": "tab", "backspace": "backspace", "return": "enter",
          "enter": "enter", "up": "up", "down": "down", "left": "left",
          "right": "right", "print": "print_screen", "capslock": "caps_lock",
          "numlock": "num_lock", "menu": "menu"}
_MODS = {"ctrl": "<ctrl>", "shift": "<shift>", "alt": "<alt>", "meta": "<cmd>"}


def to_pynput(seq: str):
    if not seq:
        return None
    parts = []
    for tok in seq.split("+"):
        t = tok.strip().lower()
        if not t:
            continue
        if t in _MODS:
            parts.append(_MODS[t])
        elif re.fullmatch(r"f\d{1,2}", t):
            parts.append(f"<{t}>")
        elif t in _NAMED:
            parts.append(f"<{_NAMED[t]}>")
        elif len(t) == 1:
            parts.append(t)
        else:
            return None
    return "+".join(parts) if parts else None
