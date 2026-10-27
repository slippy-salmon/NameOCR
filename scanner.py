"""Scan loop: capture frames while the user scrolls the People list, follow
each player across frames, then OCR the best crops and vote.

Tracking: each row is recognised by what it looks like (its two text lines,
and its profile picture once loaded), never by where it sits among its
neighbours. A row on screen is a player already seen or a new one; players
leaving or joining mid-scan, or the list jumping, need no special handling.
Position only decides the order players are listed in.
"""
from __future__ import annotations

import hashlib
import copy
import logging
import os
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from PIL import Image

import numpy as np
import rows as R

log = logging.getLogger(__name__)

MATCH = 0.75            # fingerprint similarity that counts as the same row
MAX_CROPS = 40          # distinct crops kept per player per line
USER_OCR_N = 5          # sharpest distinct username crops OCR'd per player
MAX_SAVED_FRAMES = 300  # "Save scan frames": at most this many per scan (~50 MB)
DISP_OCR_N = 3


# ----------------------------------------------------------------- tracking
@dataclass
class Track:
    sig: tuple | None = None
    user_crops: dict = field(default_factory=dict)   # hash -> [img, n, sharp]
    disp_crops: dict = field(default_factory=dict)
    avatar: Image.Image | None = None
    avatar_sharp: float = -1.0
    seen: int = 0
    gap_before: bool = False      # list jumped here: rows may have been missed
    geoms: Counter = field(default_factory=Counter)   # menu geometry it was seen in

    def add(self, img: Image.Image, row: R.Row, sig: tuple):
        self.seen += 1
        self.sig = sig if self.sig is None else R.blend(self.sig, sig)
        _keep(self.user_crops, img.crop(row.user_box))
        _keep(self.disp_crops, img.crop(row.display_box))
        av = img.crop(R.avatar_box(row))
        s = R.sharpness(av)
        if s > self.avatar_sharp:
            self.avatar, self.avatar_sharp = av, s


def _keep(store: dict, crop: Image.Image):
    key = hashlib.blake2b(crop.tobytes(), digest_size=8).digest() + \
        bytes(str(crop.size), "ascii")
    if key in store:
        store[key][1] += 1
        return
    store[key] = [crop, 1, R.sharpness(crop)]
    if len(store) > MAX_CROPS:                       # drop the blurriest
        worst = min(store, key=lambda k: store[k][2])
        del store[worst]


def geometry(found) -> tuple:
    """Where the list sits in this frame: (left edge, right edge, row height)
    in pixels. The People list keeps the same geometry for the whole scan;
    things like a loading screen don't."""
    r = found[0]
    return (int(r.left), int(r.right), float(r.full_h))


GEOM_TOL = (5, 5, 3)    # pixels of jitter that still count as the same place


GEOM_LOCK = 3           # frames in one geometry before others are rejected
TEXT_SURE = 0.95        # row text this close is the same player
AVATAR_FROM_TEXT = 0.21  # display line's top sits this far (x row height) below the card's
MIN_SEEN = 2            # a real row stays on screen for many frames
FILTER_POSITION = True  # ignore "rows" that aren't where the People list is


class Tracker:
    def __init__(self):
        self.tracks: list[Track] = []
        self.frames = 0
        self.used = 0           # frames that contributed rows
        self.skipped = 0        # frames with rows that didn't line up
        self.jumps = 0
        self.geo_counts: Counter = Counter()
        self.rejected = 0       # frames whose "rows" weren't the People list
        self._held = deque(maxlen=30)   # recently rejected frames, in case
        self._last_dom = None           # they turn out to be the real list
        self._geo_reps: list[tuple] = []   # one per distinct list position

    def _geo_id(self, found) -> int:
        """Group frames whose list sits in (nearly) the same place."""
        g = geometry(found)
        for i, rep in enumerate(self._geo_reps):
            if all(abs(a - b) <= t for a, b, t in zip(g, rep, GEOM_TOL)):
                return i
        self._geo_reps.append(g)
        return len(self._geo_reps) - 1

    def dominant_geometry(self):
        if not self.geo_counts:
            return None
        g, n = self.geo_counts.most_common(1)[0]
        return g if n >= GEOM_LOCK else None

    def feed(self, img: Image.Image, rows=None) -> int:
        """Add one frame. Returns how many rows it contributed. `rows`:
        R.detect_rows(img) if already done."""
        self.frames += 1
        rows = R.detect_rows(img) if rows is None else rows
        found = [r for r in rows if r.complete and r.user_box and r.display_box]
        if not found:
            return 0
        geo = self._geo_id(found)
        self.geo_counts[geo] += 1
        dom = self.dominant_geometry()
        if FILTER_POSITION and dom is not None and geo != dom:
            self.rejected += 1           # not the People list (loading screen etc.)
            self._held.append((geo, img, found))
            return 0
        if dom is not None and dom != self._last_dom:
            # The list's position just became clear. If frames of it were set
            # aside while something else looked dominant, use them now.
            self._last_dom = dom
            held = [h for h in self._held if h[0] == dom]
            self._held = deque((h for h in self._held if h[0] != dom),
                               maxlen=self._held.maxlen)
            for g, im, fd in held:
                self.rejected -= 1
                self._process(im, fd, g)
        return self._process(img, found, geo)

    @staticmethod
    def _sig(img, r, h):
        # the picture is placed from the text too, not the card's top edge:
        # near the list's end the card's top can be a guess that's several
        # pixels off while the text sits elsewhere, which made the same
        # player's picture look like another one (0.27) and split them in two
        q = copy.copy(r)
        q.top = r.display_box[1] - int(round(AVATAR_FROM_TEXT * h))
        q.full_h, q.bottom = h, q.top + h
        av, _, loaded = R.signature(img, q)       # picture part as before
        return (av, _text_sig(img, r, h), loaded)

    @staticmethod
    def _sim(a, b):
        """Same player? The two text lines decide: usernames are unique, so
        text this close (same player 0.98+, different <= 0.80) is the same
        player even while the profile picture is still loading and changing
        between frames (0.6). The picture only vetoes when it's clearly
        another one; in the grey zone the usual rule applies."""
        t = float(a[1] @ b[1])
        if t >= TEXT_SURE:
            if a[2] and b[2] and float(a[0] @ b[0]) < 0.3:
                return 0.0
            return t
        return R.similarity(a, b)

    def _process(self, img, found, geo) -> int:
        # fingerprint every row at this scan's one row height: the detected
        # height wobbles by a pixel or two (75 vs 76.5), which stretches the
        # text image enough to make a player miss themselves (0.80 vs 1.00)
        h = self._geo_reps[geo][2] if geo is not None and geo < len(self._geo_reps) \
            else found[0].full_h
        sigs = [self._sig(img, r, h) for r in found]
        # best match for each row; a player can only be one row per frame
        pairs = sorted(((self._sim(g, t.sig), i, j)
                        for i, g in enumerate(sigs)
                        for j, t in enumerate(self.tracks) if t.sig is not None),
                       reverse=True)
        mine, taken = {}, set()
        for sim, i, j in pairs:
            if sim < MATCH:
                break
            if i not in mine and j not in taken:
                mine[i], taken = j, taken | {j}
        if self.tracks and not mine:
            self.jumps += 1            # nothing on screen seen before: rows skipped
            gap = True
        else:
            gap = False
        order = sorted(range(len(found)), key=lambda i: found[i].top)
        for k, i in enumerate(order):
            if i in mine:
                continue
            # new player: place it right after the nearest known row above it in
            # this frame, else right before the nearest known row below, else last
            above = [mine[x] for x in order[:k] if x in mine]
            below = [mine[x] for x in order[k + 1:] if x in mine]
            t = Track(gap_before=gap and not above)
            gap = False
            if above:
                pos = above[-1] + 1
            elif below:
                pos = below[0]
            else:
                pos = len(self.tracks)
            self.tracks.insert(pos, t)
            mine = {x: (y + 1 if y >= pos else y) for x, y in mine.items()}
            mine[i] = pos
        for i, r in enumerate(found):
            t = self.tracks[mine[i]]
            t.add(img, r, sigs[i])
            t.geoms[geo] += 1
        # two list neighbours on screen together: nothing can be missing
        # between them, so a skipped-rows marker there is resolved
        for a, b in zip(order, order[1:]):
            if mine[b] == mine[a] + 1:
                self.tracks[mine[b]].gap_before = False
        self.used += 1
        return len(found)


def _text_sig(img, r, h):
    """The two text lines as a fixed-size picture, anchored on the text itself
    (its left edge and the display line's top), not on the card's edges, which
    detection places a pixel or two differently from frame to frame
    (same player: card-anchored 0.83, text-anchored 1.00). Everything right
    of the text is left out (different players then <= 0.80 over all scans)."""
    x, y = min(r.display_box[0], r.user_box[0]), r.display_box[1]
    g = np.asarray(img.crop((x, y, int(x + 6 * h), int(y + 0.68 * h))).convert("L"), float)
    # only the text itself: voice-chat buttons appear right of short names and
    # change (speaker, muted, talking), which split one player in two (0.83).
    # Past the text everything is set to the text area's own average, so it
    # adds nothing to the comparison either way
    cut = max(r.display_box[2], r.user_box[2]) - x + int(round(0.1 * h))
    m = g[:, :cut].mean() if 0 < cut < g.shape[1] else g.mean()
    if 0 < cut < g.shape[1]:
        g = g.copy()
        g[:, cut:] = m
    tx = np.asarray(Image.fromarray(np.clip(g, 0, 255).astype(np.uint8))
                    .resize((240, 24), Image.BILINEAR), float).ravel() - m
    return tx / (np.linalg.norm(tx) or 1.0)


# ----------------------------------------------------------------- capture
class Scanner:
    """Grabs the capture area as fast as it can (capped) on a thread."""

    def __init__(self, monitor: int = 1, region: dict | None = None,
                 fps: float = 30, debug_dir: str | None = None):
        self.monitor, self.region, self.fps = monitor, region, fps
        self.debug_dir = debug_dir
        self.tracker = Tracker()
        self._stop = threading.Event()
        self._thread = None
        self.error: str | None = None

    def start(self):
        self.tracker = Tracker()
        self._stop.clear()
        self.error = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> Tracker:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        return self.tracker

    def stats(self) -> dict:
        t = self.tracker
        return {"frames": t.frames, "players": sum(1 for x in t.tracks if x.seen >= MIN_SEEN),
                "gaps": sum(1 for x in t.tracks if x.gap_before),
                "fps": self._fps, "error": self.error}

    _fps = 0.0

    def _run(self):
        try:
            import mss
            with mss.mss() as sct:
                mons = sct.monitors
                mon = mons[self.monitor] if self.monitor < len(mons) else mons[1]
                if self.region:              # region is relative to the monitor
                    full = {"left": mon["left"] + self.region["left"],
                            "top": mon["top"] + self.region["top"],
                            "width": self.region["width"],
                            "height": self.region["height"]}
                else:
                    full = dict(mon)
                box, misses, n = full, 0, 0
                period = 1.0 / self.fps
                t_last = time.perf_counter()
                while not self._stop.is_set():
                    t0 = time.perf_counter()
                    shot = sct.grab(box)
                    img = Image.frombytes("RGB", shot.size, shot.rgb)
                    got = self.tracker.feed(img)
                    if self.debug_dir and n % 5 == 0 and n // 5 < MAX_SAVED_FRAMES:
                        img.save(os.path.join(self.debug_dir, f"frame_{n:05d}.png"))
                    n += 1
                    # Auto-crop to the menu once it's been found: much faster.
                    if got and box is full and not self.region:
                        box = self._menu_box(img, full) or full
                    elif not got and box is not full:
                        misses += 1
                        if misses > 15:        # menu moved/closed: look again
                            box, misses = full, 0
                    if got:
                        misses = 0
                    now = time.perf_counter()
                    self._fps = 0.8 * self._fps + 0.2 / max(1e-3, now - t_last)
                    t_last = now
                    dt = now - t0
                    if dt < period:
                        self._stop.wait(period - dt)
        except Exception as e:                       # surfaced in the UI
            self.error = f"{type(e).__name__}: {e}"

    @staticmethod
    def _menu_box(img, full):
        rows = R.detect_rows(img)
        if not rows:
            return None
        h = rows[0].full_h
        x0 = max(0, int(rows[0].left - 0.5 * h))
        x1 = min(img.width, int(rows[0].right + 0.5 * h))
        y0 = max(0, int(min(r.top for r in rows) - 1.5 * h))
        y1 = min(img.height, int(max(r.bottom for r in rows) + 1.5 * h))
        return {"left": full["left"] + x0, "top": full["top"] + y0,
                "width": x1 - x0, "height": y1 - y0}


# ----------------------------------------------------------------- reading
@dataclass
class Player:
    index: int
    user_votes: list = field(default_factory=list)     # [(reading, votes)]
    display_votes: list = field(default_factory=list)
    avatar: Image.Image | None = None
    gap_before: bool = False
    unread: bool = False
    # filled in by verify.py
    username: str = ""            # what gets copied
    display_name: str = ""        # actual display name if verified, else OCR
    user_id: int | None = None
    status: str = "unverified"    # verified | check | not_in_group | unread
    note: str = ""
    alternates: list = field(default_factory=list)
    evidence: dict | None = None  # why this account: see verify.evidence()
    excluded: bool = False
    # group checks (so switching groups can re-apply without a rescan)
    base_status: str = ""         # status before any group check
    base_note: str = ""
    group_ids: set | None = None  # every group the account is in (None = unknown)
    user_choice: bool | None = None  # you clicked it in (False) / out (True)

    @property
    def reading(self):
        return self.user_votes[0][0] if self.user_votes else ""

    @property
    def display_reading(self):
        return self.display_votes[0][0] if self.display_votes else ""


def vote(readings: list[str]) -> list[tuple[str, int]]:
    """Rank readings by how many frames agree. Also adds a per-character
    consensus of the most common length, which can fix a reading that no
    single frame got fully right."""
    readings = [r for r in readings if r]
    if not readings:
        return []
    counts = Counter(readings)
    ranked = counts.most_common()
    mode_len = Counter(len(r) for r in readings).most_common(1)[0][0]
    same = [r for r in readings if len(r) == mode_len]
    if len(same) >= 3:
        cons = "".join(Counter(c).most_common(1)[0][0] for c in zip(*same))
        if cons not in counts:
            ranked.insert(1, (cons, 0))
    return ranked


def _pick(store: dict, n: int):
    """Sharpest n distinct crops, preferring ones seen in several frames."""
    items = sorted(store.values(), key=lambda v: (v[2] * min(v[1], 3)), reverse=True)
    return [v[0] for v in items[:n]]


def real_tracks(tracker: Tracker) -> list[Track]:
    """Drop flickers (seen in one frame) and anything only ever seen outside
    the People list's position. A dropped row's gap warning moves to the next."""
    dom = tracker.dominant_geometry() or (
        tracker.geo_counts.most_common(1)[0][0] if tracker.geo_counts else None)
    out, carry = [], False
    for t in tracker.tracks:
        keep = t.seen == 0 or (t.seen >= MIN_SEEN and (
            not FILTER_POSITION or dom is None or t.geoms[dom] > 0))
        if keep:
            if carry and out:
                t.gap_before = True
            out.append(t)
            carry = False
        else:
            carry = carry or t.gap_before
    # unseen placeholders at the very ends are noise too
    while out and out[-1].seen == 0:
        out.pop()
    while out and out[0].seen == 0:
        out.pop(0)
    if out:
        out[0].gap_before = False        # nothing can be missing above the first
    return out


def read_players(tracker: Tracker, progress=None) -> list[Player]:
    t0 = time.time()
    jobs, players = [], []
    for i, t in enumerate(real_tracks(tracker)):
        p = Player(index=i, avatar=t.avatar, gap_before=t.gap_before,
                   unread=not t.user_crops)
        players.append(p)
        for c in _pick(t.user_crops, USER_OCR_N):
            jobs.append((p, "u", c))
        for c in _pick(t.disp_crops, DISP_OCR_N):
            jobs.append((p, "d", c))

    results: dict[int, dict[str, list]] = {p.index: {"u": [], "d": []} for p in players}
    done = 0

    def run(job):
        p, kind, crop = job
        if kind == "u":
            return p.index, kind, [R.ocr_username_crop(crop, h, th) for h, th in R.USER_READS]
        return p.index, kind, [R.ocr_display_crop(crop)]

    with ThreadPoolExecutor(max_workers=max(2, (os.cpu_count() or 4))) as ex:
        for idx, kind, txts in ex.map(run, jobs):
            results[idx][kind].extend(txts)
            done += 1
            if progress and done % 4 == 0:
                progress(done, len(jobs))
    log.info("OCR: %d players, %d pictures read in %.1fs", len(players), len(jobs), time.time() - t0)
    for p in players:
        p.user_votes = vote(results[p.index]["u"])
        p.display_votes = vote(results[p.index]["d"])
        p.username = p.reading
        p.display_name = p.display_reading
        if p.unread or not p.user_votes:
            p.status, p.unread = "unread", True
            p.note = "row never fully visible - scroll slower over this part"
    return players
