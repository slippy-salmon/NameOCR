"""Scan loop: capture frames while the user scrolls the People list, follow
each player across frames, then OCR the best crops and vote.

Tracking idea: the visible rows are always a contiguous window of one list.
Each frame's rows are slotted by their y-position (so a row that wasn't
detected just leaves a hole), then the window is slid along the
list of players seen so far until the row fingerprints line up.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from PIL import Image

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

    def feed(self, img: Image.Image) -> int:
        """Add one frame. Returns how many rows it contributed."""
        self.frames += 1
        found = [r for r in R.detect_rows(img)
                 if r.complete and r.user_box and r.display_box]
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

    def _process(self, img, found, geo) -> int:
        full_h = found[0].full_h
        tops = [r.top for r in found]
        pitch = R.row_pitch(tops, full_h)
        slots = [round((t - tops[0]) / pitch) for t in tops]
        if len(set(slots)) != len(slots):           # two rows in one slot
            self.skipped += 1
            return 0
        sigs = [R.signature(img, r) for r in found]

        if not self.tracks:
            self._ensure(0, slots[-1])
            off = 0
        else:
            off = self._align(slots, sigs)
            if off is None:
                fixed = self._repair_gap(slots, sigs)
                if fixed is not None:
                    off = fixed
                elif self._matches_any(sigs):
                    self.skipped += 1          # ambiguous frame: ignore it
                    return 0
                else:
                    # Nothing on screen matches anything seen: the list
                    # jumped (scrolled faster than we capture). Append the
                    # rows after a gap marker.
                    self.jumps += 1
                    off = len(self.tracks)
                    self._ensure(off, off + slots[-1])
                    self.tracks[off].gap_before = True
        lo = off + slots[0]
        if lo < 0:                                    # new rows above
            self.tracks[:0] = [Track() for _ in range(-lo)]
            off -= lo
        self._ensure(0, off + slots[-1])
        for s, r, g in zip(slots, found, sigs):
            self.tracks[off + s].add(img, r, g)
            self.tracks[off + s].geoms[geo] += 1
        # Two neighbours seen together in one frame: nothing can be missing
        # between them, so any gap warning there is resolved.
        present = set(slots)
        for s in slots:
            if s - 1 in present:
                self.tracks[off + s].gap_before = False
        self.used += 1
        return len(found)

    def _ensure(self, lo, hi):
        while len(self.tracks) <= hi:
            self.tracks.append(Track())

    def _align(self, slots, sigs):
        n, best = len(self.tracks), None
        for off in range(-slots[-1], n):
            sims = []
            for s, g in zip(slots, sigs):
                j = off + s
                if g is not None and 0 <= j < n and self.tracks[j].sig is not None:
                    sims.append(R.similarity(g, self.tracks[j].sig))
            if not sims or min(sims) < MATCH:
                continue
            key = (len(sims), sum(sims))
            if best is None or key > best[0]:
                best = (key, off)
        return None if best is None else best[1]

    def _fits(self, idx, sig):
        t = self.tracks[idx] if 0 <= idx < len(self.tracks) else None
        return t is not None and (sig is None or t.sig is None
                                  or R.similarity(sig, t.sig) >= MATCH)

    def _repair_gap(self, slots, sigs):
        """The user scrolled back over rows a jump skipped. If part of this
        frame continues the list on one side of a gap, insert the unseen rows
        into the gap (closing it if the frame reaches the other side).
        Returns the offset to use after insertion, or None."""
        for g in range(1, len(self.tracks)):
            if not self.tracks[g].gap_before:
                continue
            # A: top of the frame continues the segment that ends at g-1
            for off in range(g - 1 - slots[-1], g):
                pre = [(s, sg) for s, sg in zip(slots, sigs) if off + s < g]
                post = [(s, sg) for s, sg in zip(slots, sigs) if off + s >= g]
                if not pre or not post or not all(self._fits(off + s, sg) for s, sg in pre):
                    continue
                if not any(sg is not None and self.tracks[off + s].sig is not None
                           for s, sg in pre):
                    continue                    # need at least one real match
                hit = next((s for s, sg in post
                            if sg is not None and self.tracks[g].sig is not None and
                            R.similarity(sg, self.tracks[g].sig) >= MATCH), None)
                if hit is not None:
                    ins = off + hit - g         # rows between = new
                    after_ok = all(self._fits(off + s - ins, sg)
                                   for s, sg in post if s > hit)
                    if not after_ok:
                        continue
                else:
                    ins = off + post[-1][0] - g + 1
                self.tracks[g:g] = [Track() for _ in range(ins)]
                if hit is not None:
                    self.tracks[g + ins].gap_before = False   # gap closed
                else:
                    self.tracks[g + ins].gap_before = True
                return off
            # B: bottom of the frame continues the segment that starts at g
            for off in range(g - slots[-1], g + 1):
                post = [(s, sg) for s, sg in zip(slots, sigs) if off + s >= g]
                pre = [(s, sg) for s, sg in zip(slots, sigs) if off + s < g]
                if not pre or not post or not all(self._fits(off + s, sg) for s, sg in post):
                    continue
                if not any(sg is not None and self.tracks[off + s].sig is not None
                           for s, sg in post):
                    continue
                ins = g - (off + pre[0][0])
                self.tracks[g:g] = [Track() for _ in range(ins)]
                self.tracks[g].gap_before = True          # still a gap above
                self.tracks[g + ins].gap_before = False
                return off + ins
        return None

    def _matches_any(self, sigs):
        ts = [t.sig for t in self.tracks if t.sig is not None]
        return any(R.similarity(g, t) >= MATCH
                   for g in sigs if g is not None for t in ts)


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
