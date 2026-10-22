"""NameOCR - read every player's username from Roblox's Esc > People list.

Press the scan hotkey in Roblox, scroll the People list top to bottom, press
it again. Names are read, checked against Roblox, and shown space-separated
for copying. Click a name to leave it out of the copy.
"""
import logging
import os
import re
import sys
import time
import traceback

from PySide6.QtCore import QObject, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (QColor, QCursor, QDesktopServices, QFont, QIcon, QImage,
                           QKeySequence, QPalette, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                               QListWidget, QListWidgetItem, QTabWidget,
                               QScrollArea, QFrame,
                               QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QKeySequenceEdit,
                               QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QPushButton, QSplitter,
                               QTableWidget, QTableWidgetItem, QTextBrowser,
                               QToolButton, QToolTip, QVBoxLayout, QWidget)

import config

HERE = config.HERE
logging.basicConfig(filename=config.LOG, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nameocr")


def _excepthook(t, v, tb):
    log.error("Unhandled: %s", "".join(traceback.format_exception(t, v, tb)))
    sys.__excepthook__(t, v, tb)


sys.excepthook = _excepthook

import pytesseract  # noqa: E402

import tuning  # noqa: E402
import verify as V  # noqa: E402
from ui_extra import HowItWorks, SliderRow, info, with_info  # noqa: E402
from scanner import Scanner, read_players  # noqa: E402

STATUS = {   # key: (text colour, label, table tint)
    "verified": ("#e6e9f0", "Verified", None),
    "check": ("#f5b942", "Needs a look", "#3a3220"),
    "not_in_group": ("#f07575", "Not in group", "#3a2224"),
    "unread": ("#8c929c", "Not read", "#26282d"),
    "unverified": ("#c9cdd6", "Not verified", None),
    "edited": ("#7cc4ff", "Edited by you", "#1f2d3a"),
}
EXCLUDED = "#5d626c"


def pil_to_pixmap(img, size=32):
    img = img.convert("RGBA").resize((size, size))
    q = QImage(img.tobytes("raw", "RGBA"), size, size, 4 * size, QImage.Format_RGBA8888)
    return QPixmap.fromImage(q.copy())


def beep(kind="start"):
    try:
        import winsound
        winsound.Beep(880 if kind == "start" else 620, 120)
    except Exception:
        QApplication.beep()


# ---------------------------------------------------------------- hotkeys
class Hotkeys(QObject):
    """Global hotkeys (work while Roblox has focus) via pynput."""
    scan = Signal()
    copy = Signal()

    def __init__(self):
        super().__init__()
        self.listener = None
        self.error = None

    def apply(self, scan_seq, copy_seq):
        self.stop()
        self.error = None
        mapping = {}
        a, b = config.to_pynput(scan_seq), config.to_pynput(copy_seq)
        if a:
            mapping[a] = self.scan.emit
        if b and b != a:
            mapping[b] = self.copy.emit
        if not mapping:
            return
        try:
            from pynput import keyboard
            self.listener = keyboard.GlobalHotKeys(mapping)
            self.listener.daemon = True
            self.listener.start()
        except Exception as e:                # e.g. no display on a server
            self.error = str(e)
            log.warning("hotkeys unavailable: %s", e)

    def stop(self):
        if self.listener:
            try:
                self.listener.stop()
            except Exception:
                pass
            self.listener = None


# ---------------------------------------------------------------- worker
class Worker(QThread):
    progress = Signal(str)
    done = Signal(list)

    def __init__(self, tracker, cfg, api=None):
        super().__init__()
        self.tracker, self.cfg, self.api = tracker, cfg, api

    def run(self):
        players = read_players(
            self.tracker,
            progress=lambda d, t: self.progress.emit(f"Reading names… {d}/{t}"))
        try:
            V.verify(players, group_id=config.parse_group_id(self.cfg["group_id"]),
                     api=self.api, progress=self.progress.emit,
                     exclude_not_in_group=self.cfg["exclude_not_in_group"])
        except Exception as e:
            log.exception("verify failed")
            for p in players:
                if p.status == "unverified" and not p.unread:
                    p.note = f"couldn't check with Roblox ({type(e).__name__})"
        self.done.emit(players)


class UpdateCheck(QObject):
    """Asks GitHub for the latest release using Qt's own networking (no
    threads, nothing blocks); quietly does nothing on failure."""
    found = Signal(str, str)          # version, page url

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtNetwork import QNetworkAccessManager
        self.net = QNetworkAccessManager(self)
        self.net.finished.connect(self._done)

    def start(self):
        if not config.REPO:
            return
        from PySide6.QtNetwork import QNetworkRequest
        req = QNetworkRequest(QUrl(f"https://api.github.com/repos/{config.REPO}/releases/latest"))
        req.setRawHeader(b"Accept", b"application/vnd.github+json")
        req.setRawHeader(b"User-Agent", b"NameOCR")
        req.setTransferTimeout(8000)
        self.net.get(req)

    def _done(self, reply):
        try:
            import json
            if reply.error() != reply.NetworkError.NoError:
                log.info("update check skipped: %s", reply.errorString())
                return
            d = json.loads(bytes(reply.readAll()).decode("utf-8"))
            tag = (d.get("tag_name") or "").lstrip("vV")
            if tag and _ver(tag) > _ver(config.VERSION):
                self.found.emit(tag, d.get("html_url") or
                                f"https://github.com/{config.REPO}/releases/latest")
        except Exception as e:
            log.info("update check skipped: %s", e)
        finally:
            reply.deleteLater()


def _ver(s):
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3])


class GroupRecheck(QThread):
    done = Signal(list)

    def __init__(self, players, group_id, cfg, api=None):
        super().__init__()
        self.players, self.group_id, self.cfg, self.api = players, group_id, cfg, api

    def run(self):
        try:
            V.check_groups(self.players, self.group_id, api=self.api,
                           exclude_not_in_group=self.cfg["exclude_not_in_group"])
        except Exception:
            log.exception("group recheck failed")
        self.done.emit(self.players)


class GroupNames(QThread):
    """Fetches group names from Roblox in the background (best effort)."""
    found = Signal(int, str)

    def __init__(self, ids, api=None):
        super().__init__()
        self.ids, self.api = list(ids), api

    def run(self):
        try:
            api = self.api or V.RobloxAPI()
        except Exception:
            return
        for gid in self.ids:
            try:
                name = api.group_name(gid)
            except Exception as e:
                log.info("group name for %s unavailable: %s", gid, e)
                continue
            if name:
                self.found.emit(gid, name)


# ---------------------------------------------------------------- settings
def _wrap(layout):
    w = QWidget()
    layout.setContentsMargins(0, 0, 0, 0)
    w.setLayout(layout)
    return w


# Tuning settings: (section, key, label, kind, (lo, hi, step, fmt), tip)
TUNING_SPEC = [
    ("Finding rows", "row_colour_tolerance", "Row colour match", "slider",
     (1, 10, 1, "±{v}"),
     "How closely a pixel has to match Roblox's row colour to count as part of a row. "
     "Raise it if rows get missed on a busy background; lower it if the gaps between "
     "rows start counting as rows. Default ±3."),
    ("Finding rows", "scan_fps", "Capture rate", "slider", (5, 60, 5, "{v} fps"),
     "Screenshots per second while scanning. Higher keeps up with faster scrolling "
     "but uses more CPU (your PC may not reach the number set). Default 30."),
    ("Finding rows", "tracking_strictness", "Tracking strictness", "slider",
     (80, 98, 1, "{v}%"),
     "How alike a row has to look in two frames to be treated as the same player "
     "while the list moves. Too low can merge two players into one; too high can "
     "list one player twice. Default 90%."),
    ("Junk filter", "filter_position", "Ignore anything that isn't the People list",
     "check", None,
     "Rows are only accepted where the People list has been sitting during the scan, "
     "so loading screens or other menus can't add fake players. Turn this off if you "
     "ever think real names are being dropped. Default on."),
    ("Junk filter", "position_tolerance", "Position tolerance", "slider",
     (2, 40, 1, "{v} px"),
     "How far (in pixels) the list can seem to move and still count as the same list. "
     "Raise it if names get dropped; lower it if junk gets through. Default 5 px."),
    ("Junk filter", "min_frames", "Flicker filter", "slider",
     (1, 6, 1, lambda v: "off" if v == 1 else f"seen in {v}+ frames"),
     "A row has to appear in at least this many frames to count. Real rows stay on "
     "screen for many frames even when you scroll fast; one-frame flickers are "
     "usually junk. 1 turns it off. Default 2."),
    ("Reading names", "reads_per_name", "Readings per name", "slider",
     (1, 5, 1, "{v}"),
     "Each username is read this many ways (different sizes and contrast); all the "
     "readings are voted on and looked up. More gives better odds on hard names and "
     "takes slightly longer. Default 5."),
    ("Checking with Roblox", "display_match", "Display-name match", "slider",
     (50, 95, 1, "{v}%"),
     "How closely an account's display name has to match the one read from the row "
     "for the player to be marked Verified. Lower verifies more automatically; higher "
     "sends more to Needs a look. Default 80%."),
    ("Checking with Roblox", "lookalikes", "Try lookalike spellings", "check", None,
     "When a reading isn't a real account, try the most likely misreads: O↔0, S↔5, "
     "l↔I↔1, a stray extra character, a missing underscore and so on. Default on."),
    ("Checking with Roblox", "lookalike_limit", "Spellings to try", "slider",
     (20, 300, 10, "{v} per reading"),
     "How many lookalike spellings to try per reading, most likely first. Higher "
     "means more requests to Roblox. Default 120."),
    ("Checking with Roblox", "two_slip", "Also try two mistakes in one name", "check",
     None,
     "A second, larger round for names with two misread characters (like 5→S and "
     "0→O in the same name). Only runs for names that are still unresolved. "
     "Default on."),
    ("Checking with Roblox", "avatar_tiebreak", "Use profile pictures to break ties",
     "check", None,
     "When two accounts fit equally well, compare their profile pictures with the one "
     "in the list. It only decides if one is clearly closer (many players wear default "
     "avatars); otherwise the name goes to Needs a look. Default on."),
]

BEHAVIOUR_SPEC = [
    ("exclude_not_in_group", "Leave players who aren't in the group out of the copy",
     "They're still listed (in red) and you can click them back in."),
    ("beep", "Beep when a scan starts and stops", ""),
    ("bring_to_front", "Bring this window to the front when results are ready", ""),
    ("save_debug_frames", "Save scan frames (for troubleshooting)",
     "Saves every 5th frame of each scan in the data folder (debug_frames) so a scan "
     "can be replayed and checked. About 10 MB per scan; safe to delete any time."),
]


class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NameOCR settings")
        self.setMinimumWidth(600)
        self.cfg = dict(cfg)
        lay = QVBoxLayout(self)
        tabs = self.tabs = QTabWidget()
        lay.addWidget(tabs, 1)

        # ---------------- General
        gen = QWidget()
        gl = QVBoxLayout(gen)
        g = QGroupBox("Saved groups")
        v = QVBoxLayout(g)
        self.groups = QListWidget()
        self.groups.setMinimumHeight(96)
        self.groups.setMaximumHeight(140)
        self.groups.setToolTip("Double-click a name to rename it")
        for grp in self.cfg["groups"]:
            self._add_group_item(grp["id"], grp.get("name", ""))
        v.addWidget(self.groups)
        row = QHBoxLayout()
        self.group_in = QLineEdit()
        self.group_in.setPlaceholderText("Group ID or a link to the group page")
        self.group_in.returnPressed.connect(self._add_group)
        add = QPushButton("Add")
        add.clicked.connect(self._add_group)
        rem = QPushButton("Remove")
        rem.clicked.connect(self._remove_group)
        row.addWidget(self.group_in, 1)
        row.addWidget(add)
        row.addWidget(rem)
        v.addLayout(row)
        hint = QLabel("Pick which group to check from the dropdown in the main window. "
                      "Names are filled in from Roblox; double-click to rename.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8c929c")
        v.addWidget(hint)
        gl.addWidget(g)
        self._names = None

        g = QGroupBox("Hotkeys (work while Roblox is focused)")
        f = QFormLayout(g)
        self.hk_scan = self._key_edit(self.cfg["hotkey_scan"])
        self.hk_copy = self._key_edit(self.cfg["hotkey_copy"])
        f.addRow("Start / stop scan:", self._with_clear(self.hk_scan))
        f.addRow("Copy names:", self._with_clear(self.hk_copy))
        gl.addWidget(g)

        g = QGroupBox("Capture area")
        f = QFormLayout(g)
        self.monitor = QComboBox()
        for i, m in enumerate(self._monitors(), start=1):
            tag = "  (main)" if (m["left"], m["top"]) == (0, 0) else ""
            self.monitor.addItem(f"Display {i}: {m['width']}×{m['height']}{tag}", i)
        idx = self.monitor.findData(self.cfg["monitor"])
        self.monitor.setCurrentIndex(max(0, idx))
        self.monitor.currentIndexChanged.connect(self._monitor_changed)
        f.addRow("Display:", self.monitor)
        self.area = QLabel()
        row = QHBoxLayout()
        pick = QPushButton("Select area…")
        pick.clicked.connect(self._pick_area)
        reset = QPushButton("Use whole display")
        reset.clicked.connect(self._reset_area)
        row.addWidget(self.area, 1)
        row.addWidget(pick)
        row.addWidget(reset)
        f.addRow("Area:", row)
        hint = QLabel("Open the Esc menu on the People tab first, then drag a box "
                      "from just under the tabs to just above Leave / Respawn / Resume. "
                      "Whole display also works; it finds the menu itself.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8c929c")
        f.addRow("", hint)
        self._show_area()
        gl.addWidget(g)
        gl.addStretch(1)
        tabs.addTab(gen, "General")

        # ---------------- Tuning
        tun = QWidget()
        tun.setObjectName("tuningPage")
        tl = QVBoxLayout(tun)
        note = QLabel("These are tuned to work as they are. Change them if names are being "
                      "missed or wrongly added; hover ⓘ for what each one does. "
                      "Reset to defaults puts them back.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#8c929c")
        tl.addWidget(note)
        self.tune = {}
        forms = {}
        for section, key, label, kind, rng, tip in TUNING_SPEC:
            if section not in forms:
                box = QGroupBox(section)
                forms[section] = QFormLayout(box)
                tl.addWidget(box)
            fl = forms[section]
            if kind == "check":
                w = QCheckBox(label)
                w.setChecked(bool(self.cfg[key]))
                fl.addRow(with_info(w, tip))
            else:
                lo, hi, step, fmt = rng
                w = SliderRow(lo, hi, self.cfg[key], fmt, step)
                lab = QLabel(label + ":")
                fl.addRow(lab, with_info(w, tip))
            self.tune[key] = w
        tl.addStretch(1)
        # dependent controls
        self.tune["filter_position"].toggled.connect(
            lambda on: self.tune["position_tolerance"].setEnabled(on))
        self.tune["position_tolerance"].setEnabled(bool(self.cfg["filter_position"]))
        for k in ("lookalike_limit", "two_slip"):
            self.tune["lookalikes"].toggled.connect(self.tune[k].setEnabled)
            self.tune[k].setEnabled(bool(self.cfg["lookalikes"]))
        sc = QScrollArea()
        sc.setWidget(tun)
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        tabs.addTab(sc, "Tuning")

        # ---------------- Other
        oth = QWidget()
        ol = QVBoxLayout(oth)
        g = QGroupBox("Behaviour")
        v = QVBoxLayout(g)
        self.beh = {}
        for key, label, tip in BEHAVIOUR_SPEC:
            cb = QCheckBox(label)
            cb.setChecked(bool(self.cfg[key]))
            v.addWidget(with_info(cb, tip))
            self.beh[key] = cb
        ol.addWidget(g)
        g = QGroupBox("Tesseract OCR")
        f = QFormLayout(g)
        self.tess = QLineEdit(self.cfg["tesseract_cmd"] or config.find_tesseract())
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_tess)
        row = QHBoxLayout()
        row.addWidget(self.tess, 1)
        row.addWidget(browse)
        f.addRow("tesseract.exe:", with_info(_wrap(row),
                 "The OCR engine that reads the names. Found automatically if it's "
                 "installed in the usual place."))
        ol.addWidget(g)
        g = QGroupBox("Your data")
        v = QVBoxLayout(g)
        lab = QLabel(f"Settings, the log and saved scan frames are kept in:<br>"
                     f"<span style='color:#aab0bb'>{config.DATA}</span>")
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(lab)
        openb = QPushButton("Open data folder")
        openb.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(config.DATA)))
        row = QHBoxLayout()
        row.addWidget(openb)
        row.addWidget(info("If something goes wrong, send nameocr.log from this folder to "
                           "whoever gave you NameOCR. It lists the usernames from your scans "
                           "and nothing else about you."))
        row.addStretch(1)
        v.addLayout(row)
        ver = QLabel(f"NameOCR {config.VERSION}")
        ver.setStyleSheet("color:#7d838e")
        v.addWidget(ver)
        ol.addWidget(g)
        ol.addStretch(1)
        tabs.addTab(oth, "Other")

        # ---------------- buttons
        bottom = QHBoxLayout()
        rst = self.reset_btn = QPushButton()
        rst.clicked.connect(self._reset_defaults)
        tabs.currentChanged.connect(self._tab_changed)
        self.reset_note = QLabel()
        self.reset_note.setStyleSheet("color:#7fd18b")
        bottom.addWidget(rst)
        bottom.addWidget(self.reset_note, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        bottom.addWidget(bb)
        lay.addLayout(bottom)
        self.resize(640, 720)
        self._tab_changed(tabs.currentIndex())

    RESET_TIPS = [
        "Puts everything on the General tab back to its default: removes your saved "
        "groups, sets the capture area back to the whole of display 1, and the hotkeys "
        "back to F8 / F9. You'll be asked to confirm.",
        "Puts every setting on the Tuning tab back to its default. Nothing on the other "
        "tabs changes.",
        "Puts the Behaviour options back to their defaults and finds Tesseract "
        "automatically again. Nothing on the other tabs changes.",
    ]

    def _tab_changed(self, i):
        self.reset_btn.setText(f"Reset {self.tabs.tabText(i)} to defaults")
        self.reset_btn.setToolTip(f"<qt>{self.RESET_TIPS[i]}<br><br>Nothing is kept "
                                  "until you click Save; Cancel undoes it.</qt>")
        self.reset_note.clear()

    def _reset_defaults(self):
        """Reset only the tab that's showing."""
        i = self.tabs.currentIndex()
        d = config.DEFAULTS
        if i == 0:
            n = self.groups.count()
            ok = QMessageBox.question(
                self, "Reset General",
                f"This removes your {n} saved group{'s' if n != 1 else ''}, sets the capture "
                "area back to the whole of display 1, and the hotkeys back to F8 / F9."
                "\n\nNothing is kept until you click Save. Continue?")
            if ok != QMessageBox.Yes:
                return
            self.groups.clear()
            self.hk_scan.setKeySequence(QKeySequence(d["hotkey_scan"]))
            self.hk_copy.setKeySequence(QKeySequence(d["hotkey_copy"]))
            self.monitor.setCurrentIndex(max(0, self.monitor.findData(d["monitor"])))
            self.cfg["region"] = d["region"]
            self._show_area()
        elif i == 1:
            for key, w in self.tune.items():
                if isinstance(w, QCheckBox):
                    w.setChecked(bool(config.TUNING[key]))
                else:
                    w.setValue(config.TUNING[key])
        else:
            for key, cb in self.beh.items():
                cb.setChecked(bool(d[key]))
            self.tess.setText(config.find_tesseract())
        self.reset_note.setText(f"{self.tabs.tabText(i)} reset. Click Save to keep it.")

    # helpers
    def _key_edit(self, text):
        e = QKeySequenceEdit(QKeySequence(text))
        e.setMaximumSequenceLength(1) if hasattr(e, "setMaximumSequenceLength") else None
        return e

    def _with_clear(self, edit):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        b = QToolButton()
        b.setText("✕")
        b.setToolTip("No hotkey")
        b.clicked.connect(edit.clear)
        h.addWidget(b)
        return w

    @staticmethod
    def _monitors():
        try:
            import mss
            with mss.mss() as s:
                return s.monitors[1:]
        except Exception:
            return [{"left": 0, "top": 0, "width": 0, "height": 0}]

    def _add_group_item(self, gid, name):
        it = QListWidgetItem(name or f"Group {gid}")
        it.setData(Qt.UserRole, gid)
        it.setData(Qt.UserRole + 1, bool(name))      # has a real name
        it.setToolTip(f"Group ID {gid}")
        it.setFlags(it.flags() | Qt.ItemIsEditable)
        self.groups.addItem(it)
        return it

    def _add_group(self):
        gid = config.parse_group_id(self.group_in.text())
        if not gid:
            QMessageBox.information(self, "Add group", "Paste a group ID or the link to "
                                    "the group's page on roblox.com.")
            return
        for i in range(self.groups.count()):
            if self.groups.item(i).data(Qt.UserRole) == gid:
                self.groups.setCurrentRow(i)
                self.group_in.clear()
                return
        it = self._add_group_item(gid, "")
        self.groups.setCurrentItem(it)
        self.group_in.clear()
        self._names = GroupNames([gid], api=getattr(self.parent(), "api", None))
        self._names.found.connect(self._got_name)
        self._names.start()

    def _got_name(self, gid, name):
        for i in range(self.groups.count()):
            it = self.groups.item(i)
            if it.data(Qt.UserRole) == gid and not it.data(Qt.UserRole + 1):
                it.setText(name)
                it.setData(Qt.UserRole + 1, True)

    def _remove_group(self):
        for it in self.groups.selectedItems():
            self.groups.takeItem(self.groups.row(it))

    def saved_groups(self):
        out = []
        for i in range(self.groups.count()):
            it = self.groups.item(i)
            gid, text = it.data(Qt.UserRole), it.text().strip()
            named = it.data(Qt.UserRole + 1) or (text and text != f"Group {gid}")
            out.append({"id": gid, "name": text if named else ""})
        return out

    def _show_area(self):
        r = self.cfg["region"]
        self.area.setText(f"{r['width']}×{r['height']} at ({r['left']}, {r['top']})"
                          if r else "Whole display (finds the menu itself)")

    def _monitor_changed(self):
        self.cfg["monitor"] = self.monitor.currentData()
        self.cfg["region"] = None                     # area belongs to a display
        self._show_area()

    def _reset_area(self):
        self.cfg["region"] = None
        self._show_area()

    def _pick_area(self):
        from region_select import RegionSelector
        top = self.parent()
        self.hide()
        if top:
            top.hide()

        def launch():
            self.sel = RegionSelector(self.monitor.currentData(), self.cfg["region"])
            self.sel.picked.connect(self._picked)
            self.sel.destroyed.connect(restore)
            self.sel.show()

        def restore(*_):
            if top:
                top.show()
            self.show()
            self.raise_()
            self.activateWindow()
        QTimer.singleShot(350, launch)               # let our windows vanish first

    def _picked(self, box):
        self.cfg["region"] = box
        self._show_area()
        self.area.setStyleSheet("color:#7fd18b")       # flash green: it took
        QTimer.singleShot(2500, lambda: self.area.setStyleSheet(""))

    def _browse_tess(self):
        p, _ = QFileDialog.getOpenFileName(self, "Find tesseract.exe", self.tess.text(),
                                           "tesseract (tesseract*)")
        if p:
            self.tess.setText(p)

    def _save(self):
        groups = self.saved_groups()
        current = config.parse_group_id(self.cfg["group_id"])
        if current not in [g["id"] for g in groups]:          # it was removed
            current = groups[0]["id"] if groups else None
        self.cfg.update(
            groups=groups, group_id=str(current) if current else "",
            hotkey_scan=self.hk_scan.keySequence().toString(),
            hotkey_copy=self.hk_copy.keySequence().toString(),
            monitor=self.monitor.currentData() or 1,
            tesseract_cmd=self.tess.text().strip())
        for key, w in self.tune.items():
            self.cfg[key] = w.isChecked() if isinstance(w, QCheckBox) else w.value()
        for key, cb in self.beh.items():
            self.cfg[key] = cb.isChecked()
        for name, seq in (("scan", self.cfg["hotkey_scan"]), ("copy", self.cfg["hotkey_copy"])):
            if seq and not config.to_pynput(seq):
                QMessageBox.warning(self, "Hotkey", f"Can't use “{seq}” as the {name} hotkey.")
                return
        self.accept()


# ---------------------------------------------------------------- main window
class Main(QMainWindow):
    def __init__(self, api=None):
        super().__init__()
        self.api = api                      # injectable for testing
        self.cfg = config.load()
        self.players, self.scanner, self.worker = [], None, None
        self.state = "idle"
        self.setWindowTitle(f"NameOCR {config.VERSION}" + (" (dev)" if config.DEV else ""))
        self.setWindowIcon(QIcon(os.path.join(config.RES, "icon.png")))
        self.resize(720, 640)

        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        top = QHBoxLayout()
        self.dot = QLabel("●")
        self.status = QLabel()
        self.status.setWordWrap(True)
        top.addWidget(self.dot)
        top.addWidget(self.status, 1)
        how = QToolButton()
        how.setText("ⓘ  How it works")
        how.clicked.connect(lambda: HowItWorks(self).exec())
        top.addWidget(how)
        gear = QToolButton()
        gear.setText("⚙  Settings")
        gear.setToolButtonStyle(Qt.ToolButtonTextOnly)
        gear.clicked.connect(self.open_settings)
        top.addWidget(gear)
        lay.addLayout(top)

        grow = QHBoxLayout()
        glabel = QLabel("Group:")
        self.group_box = QComboBox()
        self.group_box.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.group_box.setMinimumWidth(220)
        self.group_box.activated.connect(self._group_picked)
        grow.addWidget(glabel)
        grow.addWidget(self.group_box)
        grow.addStretch(1)
        lay.addLayout(grow)

        self.scan_btn = QPushButton()
        self.scan_btn.setMinimumHeight(44)
        self.scan_btn.setFont(QFont(self.font().family(), 11, QFont.DemiBold))
        self.scan_btn.clicked.connect(self.toggle_scan)
        lay.addWidget(self.scan_btn)

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("background:#3a3220;color:#f5d58a;padding:8px;border-radius:6px")
        self.banner.hide()
        lay.addWidget(self.banner)

        head = QHBoxLayout()
        self.count = QLabel("Names")
        self.count.setFont(QFont(self.font().family(), 10, QFont.DemiBold))
        head.addWidget(self.count, 1)
        legend = QLabel(" ".join(f"<span style='color:{c}'>■</span> {t}"
                                 for k, (c, t, _) in STATUS.items()
                                 if k in ("verified", "check", "not_in_group"))
                        + f" <span style='color:{EXCLUDED}'>■</span> <s>Left out</s>")
        legend.setStyleSheet("color:#8c929c")
        head.addWidget(legend)
        lay.addLayout(head)

        split = QSplitter(Qt.Vertical)
        self.out = QTextBrowser()
        self.out.setOpenLinks(False)
        self.out.setFont(QFont("Consolas", 11))
        self.out.document().setDefaultStyleSheet("a { text-decoration: none; }")
        self.out.anchorClicked.connect(self._clicked)
        self.out.highlighted.connect(self._hover)
        self.out.setPlaceholderText("Names show up here after a scan. "
                                    "Click a name to leave it out of the copy.")
        split.addWidget(self.out)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Copy", "", "Username", "Display name", "Status"])
        self.table.verticalHeader().hide()
        self.table.setIconSize(QSize(28, 28))
        self.table.verticalHeader().setDefaultSectionSize(34)
        hh = self.table.horizontalHeader()
        for c, mode in enumerate([QHeaderView.ResizeToContents, QHeaderView.ResizeToContents,
                                  QHeaderView.Stretch, QHeaderView.Stretch,
                                  QHeaderView.Stretch]):
            hh.setSectionResizeMode(c, mode)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked |
                                   QAbstractItemView.EditKeyPressed)
        self.table.itemChanged.connect(self._table_edit)
        split.addWidget(self.table)
        split.setSizes([200, 300])
        lay.addWidget(split, 1)

        bottom = QHBoxLayout()
        self.copy_btn = QPushButton("Copy names")
        self.copy_btn.setMinimumHeight(38)
        self.copy_btn.clicked.connect(self.copy_names)
        self.copied = QLabel()
        self.copied.setStyleSheet("color:#7fd18b")
        bottom.addWidget(self.copy_btn)
        bottom.addWidget(self.copied, 1)
        tip = QLabel("Double-click a username in the table to fix it")
        tip.setStyleSheet("color:#8c929c")
        bottom.addWidget(tip)
        lay.addLayout(bottom)
        self.setCentralWidget(root)

        self.hotkeys = Hotkeys()
        self.hotkeys.scan.connect(self.toggle_scan)
        self.hotkeys.copy.connect(lambda: self.copy_names(from_hotkey=True))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.apply_settings()
        self._set_state("idle")
        self.update_bar = QLabel()
        self.update_bar.setOpenExternalLinks(True)
        self.update_bar.setStyleSheet("background:#1f2d3a;color:#cfe6ff;padding:8px;"
                                      "border-radius:6px")
        self.update_bar.hide()
        self.centralWidget().layout().insertWidget(1, self.update_bar)
        self._upd = UpdateCheck(self)
        self._upd.found.connect(self._update_found)
        self._upd.start()

    def _update_found(self, version, url):
        self.update_bar.setText(f"NameOCR {version} is available (you have {config.VERSION}). "
                                f"<a href='{url}' style='color:#7cc4ff'>Download it</a>, then run "
                                "the installer; your settings are kept.")
        self.update_bar.show()

    # ------------------------------------------------------------ settings
    def apply_settings(self):
        tess = config.find_tesseract(self.cfg["tesseract_cmd"]) or "tesseract"
        pytesseract.pytesseract.tesseract_cmd = tess
        if tess == config.bundled_tesseract():          # its language data is beside it
            os.environ["TESSDATA_PREFIX"] = os.path.join(os.path.dirname(tess), "tessdata")
        self.hotkeys.apply(self.cfg["hotkey_scan"], self.cfg["hotkey_copy"])
        tuning.apply(self.cfg)
        self._fill_groups()
        self._fetch_group_names()
        warn = []
        if not config.find_tesseract(self.cfg["tesseract_cmd"]):
            warn.append("Tesseract OCR wasn't found. Install it, or set its path in Settings.")
        if self.hotkeys.error:
            first = str(self.hotkeys.error).strip().splitlines()[0][:120]
            warn.append(f"Hotkeys aren't working: {first}")
        self.banner.setText("\n".join(warn))
        self.banner.setVisible(bool(warn))
        hk = self.cfg["hotkey_copy"]
        self.copy_btn.setText(f"Copy names ({hk})" if hk else "Copy names")
        self._set_state(self.state)

    def _fill_groups(self):
        self.group_box.blockSignals(True)
        self.group_box.clear()
        for g in self.cfg["groups"]:
            self.group_box.addItem(config.group_label(g), g["id"])
        self.group_box.addItem("No group check", None)
        cur = config.parse_group_id(self.cfg["group_id"])
        idx = self.group_box.findData(cur) if cur else self.group_box.count() - 1
        self.group_box.setCurrentIndex(max(0, idx))
        if not self.cfg["groups"]:
            self.group_box.setToolTip("Add groups in Settings")
        self.group_box.blockSignals(False)

    def _group_picked(self, idx):
        gid = self.group_box.itemData(idx)
        if self.cfg["group_id"] == (str(gid) if gid else ""):
            return
        self.cfg["group_id"] = str(gid) if gid else ""
        config.save(self.cfg)
        if self.players and self.state == "idle":     # recheck the current list
            self._set_state("working", "Checking the list against "
                            f"{self.group_box.currentText().strip()}…")
            self._recheck = GroupRecheck(self.players, gid, self.cfg, api=self.api)
            self._recheck.done.connect(self._rechecked)
            self._recheck.start()

    def _rechecked(self, players):
        self.render()
        name = self.group_box.currentText().strip()
        if not config.parse_group_id(self.cfg["group_id"]):
            self._set_state("idle", "Group check turned off for this list.")
            return
        checked = [p for p in players if p.user_id and p.group_ids is not None
                   and p.status != "edited"]
        out = sum(p.status == "not_in_group" for p in checked)
        failed = sum(1 for p in players if p.user_id and p.group_ids is None)
        msg = f"Checked against {name}: {len(checked) - out} in the group, {out} not."
        if failed:
            msg += f" Couldn't check {failed}."
        self._set_state("idle", msg)

    def _fetch_group_names(self):
        missing = [g["id"] for g in self.cfg["groups"] if not g.get("name")]
        if missing:
            self._names = GroupNames(missing, api=self.api)
            self._names.found.connect(self._got_group_name)
            self._names.start()

    def _got_group_name(self, gid, name):
        for g in self.cfg["groups"]:
            if g["id"] == gid and not g.get("name"):
                g["name"] = name
        config.save(self.cfg)
        self._fill_groups()

    def open_settings(self):
        if self.state == "scanning":
            return
        d = getattr(self, "_settings", None)
        if d is not None and d.isVisible():
            d.raise_()
            d.activateWindow()
            return
        # Not exec(): the area picker hides this dialog, and hiding an exec()'d
        # dialog ends it as if Cancel was pressed, losing everything.
        d = SettingsDialog(self.cfg, self)
        d.setWindowModality(Qt.ApplicationModal)
        d.accepted.connect(lambda: self._settings_saved(d))
        self._settings = d
        d.show()

    def _settings_saved(self, d):
        self.cfg = d.cfg
        config.save(self.cfg)
        self.apply_settings()

    # ------------------------------------------------------------ states
    def _set_state(self, state, msg=None):
        self.state = state
        hk = self.cfg["hotkey_scan"]
        key = f" ({hk})" if hk else ""
        colors = {"idle": "#7fd18b", "scanning": "#f06a6a", "working": "#f5b942"}
        if hasattr(self, "group_box"):
            self.group_box.setEnabled(state == "idle")
        self.dot.setStyleSheet(f"color:{colors.get(state, '#7fd18b')}; font-size:16px")
        if state == "idle":
            self.scan_btn.setText(f"Start scan{key}")
            self.scan_btn.setStyleSheet("")
            self.scan_btn.setEnabled(True)
            self.status.setText(msg or (
                f"Ready. In Roblox, open Esc → People (list view), press {hk or 'Start scan'}, "
                f"scroll slowly to the bottom, then press it again."))
        elif state == "scanning":
            self.scan_btn.setText(f"Stop scan{key}")
            self.scan_btn.setStyleSheet("background:#8a2f35; color:white")
            self.scan_btn.setEnabled(True)
            self.status.setText(msg or "Scanning… scroll the People list top to bottom.")
        elif state == "working":
            self.scan_btn.setText("Working…")
            self.scan_btn.setEnabled(False)
            self.status.setText(msg or "Working…")

    def toggle_scan(self):
        if self.state == "idle":
            self.start_scan()
        elif self.state == "scanning":
            self.stop_scan()

    def start_scan(self):
        debug = None
        if self.cfg["save_debug_frames"]:
            debug = os.path.join(config.FRAMES, time.strftime("%Y%m%d-%H%M%S"))
            os.makedirs(debug, exist_ok=True)
        self.scanner = Scanner(monitor=int(self.cfg["monitor"] or 1),
                               region=self.cfg["region"], debug_dir=debug,
                               fps=float(self.cfg["scan_fps"]))
        self.scanner.start()
        self.copied.clear()
        if self.cfg["beep"]:
            beep("start")
        self._set_state("scanning")
        self.timer.start(250)
        log.info("scan started (monitor %s, region %s)", self.cfg["monitor"], self.cfg["region"])

    def _tick(self):
        if self.state != "scanning" or not self.scanner:
            return
        s = self.scanner.stats()
        if s["error"]:
            self.timer.stop()
            self.scanner.stop()
            self._set_state("idle", f"Capture failed: {s['error']}")
            return
        if s["players"] == 0:
            msg = ("Scanning… can't see the People list yet. Open Esc → People "
                   "(list view)" + (" inside your capture area." if self.cfg["region"] else "."))
        else:
            msg = f"Scanning… {s['players']} players seen · {s['fps']:.0f} fps"
            if s["gaps"]:
                msg += f"  ·  ⚠ scrolled too fast in {s['gaps']} place(s); scroll back over it"
        self.status.setText(msg)

    def stop_scan(self):
        self.timer.stop()
        tracker = self.scanner.stop()
        if self.cfg["beep"]:
            beep("stop")
        log.info("scan stopped: %d frames, %d tracks, %d jumps",
                 tracker.frames, len(tracker.tracks), tracker.jumps)
        if not any(t.seen for t in tracker.tracks):
            self._set_state("idle", "Didn't find any players. Make sure the Esc menu is on "
                                    "People in list view, and check the capture area in Settings.")
            return
        self._set_state("working", "Reading names…")
        self.worker = Worker(tracker, self.cfg, api=self.api)
        self.worker.progress.connect(lambda m: self.status.setText(m))
        self.worker.done.connect(self._results)
        self.worker.start()

    def _results(self, players):
        self.players = players
        self.render()
        n = len(players)
        c = {k: sum(p.status == k for p in players) for k in STATUS}
        parts = [f"{c['verified']} verified"]
        if c["check"]:
            parts.append(f"{c['check']} need a look")
        if c["not_in_group"]:
            parts.append(f"{c['not_in_group']} not in group")
        if c["unread"]:
            parts.append(f"{c['unread']} not read")
        gaps = sum(p.gap_before for p in players)
        msg = f"Done: {n} players. " + ", ".join(parts) + "."
        if gaps:
            msg += f" ⚠ The list may have skipped rows in {gaps} place(s); rescan slower to be sure."
        self._set_state("idle", msg)
        if self.cfg["bring_to_front"]:
            self.showNormal()
            self.raise_()
            self.activateWindow()
            QApplication.alert(self)

    # ------------------------------------------------------------ output
    def render(self):
        bits = []
        for i, p in enumerate(self.players):
            if p.unread:
                continue
            color = EXCLUDED if p.excluded else STATUS.get(p.status, STATUS["unverified"])[0]
            name = (p.username or p.reading).replace("<", "&lt;")
            inner = f"<s>{name}</s>" if p.excluded else name
            gap = "<span style='color:#f5b942'>⋯ </span>" if p.gap_before else ""
            bits.append(f"{gap}<a href='p:{i}'><span style='color:{color}'>{inner}</span></a>")
        self.out.setHtml("<div style='line-height:150%'>" + " ".join(bits) + "</div>")
        kept = sum(1 for p in self.players if not p.excluded and not p.unread and p.username)
        total = sum(1 for p in self.players if not p.unread)
        self.count.setText(f"Names: {kept} of {total} will be copied")
        self._fill_table()

    def _fill_table(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.players))
        for r, p in enumerate(self.players):
            color, label, tint = STATUS.get(p.status, STATUS["unverified"])
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Unchecked if (p.excluded or p.unread) else Qt.Checked)
            if p.unread:
                chk.setFlags(Qt.ItemIsEnabled)
            pic = QTableWidgetItem()
            pic.setFlags(Qt.ItemIsEnabled)
            if p.avatar is not None:
                pic.setIcon(QIcon(pil_to_pixmap(p.avatar)))
            user = QTableWidgetItem(p.username or p.reading or "—")
            user.setFlags(Qt.ItemIsEnabled | Qt.ItemIsEditable)
            disp = QTableWidgetItem(p.display_name or p.display_reading)
            disp.setFlags(Qt.ItemIsEnabled)
            note = f"{label}" + (f": {p.note}" if p.note else "")
            if p.alternates and p.status == "check":
                note += f" (or: {', '.join(p.alternates)})"
            st = QTableWidgetItem(note)
            st.setFlags(Qt.ItemIsEnabled)
            st.setForeground(QColor(color))
            st.setToolTip(note)
            for c, it in enumerate((chk, pic, user, disp, st)):
                if tint:
                    it.setBackground(QColor(tint))
                if p.excluded and c >= 2:
                    it.setForeground(QColor(EXCLUDED))
                self.table.setItem(r, c, it)
        self.table.blockSignals(False)

    def _clicked(self, url: QUrl):
        s = url.toString()
        if s.startswith("p:"):
            p = self.players[int(s[2:])]
            p.excluded = not p.excluded
            p.user_choice = p.excluded                # sticks across group switches
            self.render()

    def _hover(self, url: QUrl):
        s = url.toString()
        if not s.startswith("p:"):
            QToolTip.hideText()
            return
        p = self.players[int(s[2:])]
        label = STATUS.get(p.status, STATUS["unverified"])[1]
        lines = [f"<b>{p.username}</b>  ·  {p.display_name or p.display_reading}",
                 label + (f": {p.note}" if p.note else "")]
        if p.alternates:
            lines.append("Other possibilities: " + ", ".join(p.alternates))
        lines.append("<i>Click to " + ("put back in" if p.excluded else "leave out of") +
                     " the copy</i>")
        QToolTip.showText(QCursor.pos(), "<br>".join(lines), self.out)

    def _table_edit(self, item):
        p = self.players[item.row()]
        if item.column() == 0:
            p.excluded = item.checkState() != Qt.Checked
            p.user_choice = p.excluded
        elif item.column() == 2:
            new = item.text().strip().lstrip("@")
            if new and new != p.username:
                p.username = new
                p.status, p.note = "edited", "typed in by you"
                p.excluded = False
        self.render()

    def copy_names(self, from_hotkey=False):
        names = [p.username for p in self.players
                 if not p.excluded and not p.unread and p.username]
        if not names:
            self.copied.setText("Nothing to copy yet")
            return
        QApplication.clipboard().setText(" ".join(names))
        self.copied.setText(f"Copied {len(names)} names ✓")
        if from_hotkey and self.cfg["beep"]:
            beep("start")

    def closeEvent(self, e):
        self.hotkeys.stop()
        if self.scanner:
            self.scanner.stop()
        # Let background work finish (or stop it) so Qt doesn't crash on exit
        sd = getattr(self, "_settings", None)
        for t in (getattr(self, "_names", None), getattr(sd, "_names", None),
                  getattr(self, "_recheck", None), self.worker):
            if isinstance(t, QThread) and t.isRunning():
                if not t.wait(1500):
                    t.terminate()
                    t.wait(500)
        super().closeEvent(e)


def dark_palette(app):
    app.setStyle("Fusion")
    p = QPalette()
    c = {QPalette.Window: "#1b1d22", QPalette.WindowText: "#e6e9f0",
         QPalette.Base: "#202329", QPalette.AlternateBase: "#25282f",
         QPalette.ToolTipBase: "#2b2f37", QPalette.ToolTipText: "#e6e9f0",
         QPalette.Text: "#e6e9f0", QPalette.Button: "#2b2f37",
         QPalette.ButtonText: "#e6e9f0", QPalette.Highlight: "#3d6fd6",
         QPalette.HighlightedText: "#ffffff", QPalette.Link: "#7cc4ff",
         QPalette.PlaceholderText: "#7d838e"}
    for role, col in c.items():
        p.setColor(role, QColor(col))
    app.setPalette(p)
    app.setStyleSheet("""
        QGroupBox { border:1px solid #2f333b; border-radius:8px; margin-top:12px; padding:8px; }
        QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 4px; color:#aab0bb; }
        QPushButton { padding:6px 14px; border-radius:6px; background:#2b2f37; border:1px solid #3a3f49; }
        QPushButton:hover { background:#333843; }
        QPushButton:disabled { color:#7d838e; }
        QTextBrowser, QTableWidget, QLineEdit, QComboBox, QKeySequenceEdit {
            border:1px solid #2f333b; border-radius:6px; }
        QHeaderView::section { background:#25282f; color:#aab0bb; border:none; padding:4px; }
        QTabWidget::pane { border:1px solid #2f333b; border-radius:6px; background:#1b1d22; top:-1px; }
        QTabWidget > QStackedWidget > QWidget, QScrollArea > QWidget > QWidget#tuningPage {
            background:#1b1d22; }
        QTabBar::tab { background:#22252b; color:#aab0bb; padding:6px 14px; border:1px solid #2f333b;
            border-bottom:none; border-top-left-radius:6px; border-top-right-radius:6px; margin-right:2px; }
        QTabBar::tab:selected { background:#1b1d22; color:#e6e9f0; }
        QTabBar::tab:hover { color:#e6e9f0; }
        QToolTip { border:1px solid #3a3f49; padding:6px; }
    """)


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(config.APP_ID)
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("NameOCR")
    dark_palette(app)
    w = Main()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
