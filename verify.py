"""Turn OCR readings into real Roblox accounts.

For each player:
  1. Look up every voted username reading (case-insensitive, batched).
  2. Score each account found by how well its display name matches the
     display name read from the same row (display names are bigger text and
     read far more reliably than the @username line).
  3. If nothing fits, try lookalike variants (l/i/1, o/0, rn/m, ii/i, a
     dropped underscore, ...) and score those too.
  4. If several accounts fit equally, compare profile pictures.
  5. Check group membership for the chosen account.
Anything still uncertain is marked "check" for a human.
"""
from __future__ import annotations

import copy
import difflib
import logging
import re
import time
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import numpy as np
from PIL import Image

USERS_URL = "https://users.roblox.com/v1/usernames/users"
GROUPS_V2 = "https://groups.roblox.com/v2/users/{uid}/groups/roles"
GROUPS_V1 = "https://groups.roblox.com/v1/users/{uid}/groups/roles"
HEADSHOT_URL = "https://thumbnails.roblox.com/v1/users/avatar-headshot"

log = logging.getLogger("nameocr")

VALID = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)?$")       # checked lowercased


# Accounts made without choosing a username get "roblox_user_<number>", which
# breaks the normal rules (longer than 20, two underscores).
AUTO_NAME = re.compile(r"^roblox_user_(\d{4,})$")


def valid_username(s: str) -> bool:
    s = s.lower()
    return bool(AUTO_NAME.match(s)) or (3 <= len(s) <= 20 and bool(VALID.match(s)))


# ------------------------------------------------------------------ network
def requests_error(code):
    import requests
    r = requests.Response()
    r.status_code = code
    return requests.HTTPError(f"{code} (stopped)", response=r)


class RobloxAPI:
    """Thin wrapper over the public endpoints (no login needed)."""

    REMEMBER_NAMES = 30 * 60     # seconds a lookup (found or not) is remembered

    def __init__(self, timeout=10):
        import requests
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "NameOCR/1.0"
        self.timeout = timeout
        # Rescanning the same server asks about the same names again; Roblox
        # rate-limits that quickly, so whether a username exists (and its
        # display name) is remembered while the app is open. Only in memory,
        # never saved. Group membership is always asked fresh, so someone who
        # just joined the group shows up on a rescan.
        self._names: dict[str, tuple[float, dict | None]] = {}
        self.record = None        # dev: a dict to collect every answer in (for replaying scans)
        self.wait_limit = None    # set by verify(): seconds it may spend waiting out rate limits
        self.waited = 0.0
        self.rate_limited = False
        self.stop = False         # "Stop checking": give up on the rate limit now
        self.on_wait = None       # set by verify(): told how long it's been waiting

    def _mem(self, which):
        return self.__dict__.setdefault(which, {})

    def _pause(self, secs):
        """Wait out the rate limit (in steps, so "Stop checking" is quick),
        telling verify() how long it's been waiting so it can show it."""
        self.waited = getattr(self, "waited", 0.0) + secs
        on_wait = getattr(self, "on_wait", None)
        if on_wait:
            on_wait(self.waited)
        left = secs
        while left > 0 and not getattr(self, "stop", False):
            step = min(0.2, left)
            time.sleep(step)
            left -= step

    @staticmethod
    def _budget(r):
        """Roblox's own rate-limit numbers, when it sends them: (requests left
        in this window, seconds until it resets). The first value counts when
        there are several windows ("30, 30;w=60")."""
        def first(h):
            try:
                return float(str(r.headers.get(h, "")).split(",")[0].split(";")[0])
            except ValueError:
                return None
        return first("x-ratelimit-remaining"), first("x-ratelimit-reset")

    def _req(self, method, url, retry=True, **kw):
        """A request that rides out Roblox's rate limit, going by what Roblox
        says: when its budget for this endpoint runs out it pauses until the
        reset it gives (instead of running into the limit), and after a 429 it
        waits as long as Roblox asks (retry-after, else the reset, else a
        growing backoff). During a verify (wait_limit set) it keeps going
        until that many seconds have been spent waiting in all (no limit by
        default) or "Stop checking" is pressed, then sets rate_limited."""
        key = re.sub(r"/\d+/", "/{id}/", url.split("?")[0])     # one budget per endpoint
        hold = self._mem("_hold")                 # endpoint -> time its budget is back
        seen = self._mem("_seen_limits")
        delay, attempts = 1.0, 0
        limit = getattr(self, "wait_limit", None)
        while True:
            if limit is not None and hold.get(key, 0) > time.time():
                if getattr(self, "stop", False):
                    self.rate_limited = True
                    raise requests_error(429)
                self._pause(hold.pop(key) - time.time())
            r = self.s.request(method, url, timeout=self.timeout, **kw)
            rem, reset = self._budget(r)
            if r.headers.get("x-ratelimit-limit") and key not in seen:
                seen[key] = r.headers["x-ratelimit-limit"]
                log.info("Roblox's rate limit for %s: %s", key, seen[key])
            if rem is not None and rem <= 0 and reset:
                hold[key] = time.time() + reset
            if r.status_code != 429 and r.status_code < 500:
                return r
            if not retry:
                return r                 # the caller adapts (see lookup)
            attempts += 1
            ra = r.headers.get("Retry-After")
            try:
                # a 429 while Roblox still reports budget left (it does: "494
                # of 500 left, resets in 56 s") is some other limit, so its
                # reset time means nothing here: short growing waits instead
                spent = rem is not None and rem <= 0
                wait = float(ra) if ra else ((reset or delay) if spent else delay)
            except ValueError:
                wait = delay
            wait = max(0.5, min(wait, 60.0))
            waited = getattr(self, "waited", 0.0)
            if limit is not None and (waited + wait > limit or getattr(self, "stop", False)):
                self.rate_limited = True
                log.info("Roblox said %s (rate limit); already waited %.0fs, stopping", r.status_code, waited)
                break
            if limit is None and attempts > 5:
                break
            log.info("Roblox said %s; waiting %.0fs (retry-after %s, remaining %s, reset %s)",
                     r.status_code, wait, ra, rem, reset)
            self._pause(wait)
            delay = min(delay * 2, 30.0)
        r.raise_for_status()
        return r

    def lookup(self, names: list[str], progress=None) -> dict[str, dict]:
        """lowercased username -> {id, name, displayName}. A failed batch is
        logged and skipped rather than stopping everything."""
        out, now = {}, time.time()
        names = list(dict.fromkeys(n.lower() for n in names if valid_username(n)))
        ask = []
        for n in names:                                  # remembered from a recent scan?
            hit = self._mem('_names').get(n)
            if hit and now - hit[0] < self.REMEMBER_NAMES:
                if hit[1]:
                    out[n] = hit[1]
                if getattr(self, "record", None) is not None:
                    self.record.setdefault("names", {})[n] = hit[1]
            else:
                ask.append(n)
        names = ask
        self.failed = 0          # batches Roblox refused this call (rate limit, network)
        # Roblox turns username lookups down well before the request budget
        # it reports runs out (it seems to go by names asked: ~10 a second
        # however they're batched). So: full batches, and after a refusal a
        # short wait and try again (2 s, growing to 10 s while it lasts)
        pending, backoff = list(names), 2.0
        limit = getattr(self, "wait_limit", None)
        limit = 30.0 if limit is None else limit
        while pending:
            chunk, chunk_done = pending[:100], False
            if progress:
                progress(len(names) - len(pending), len(names))
            try:
                r = self._req("POST", USERS_URL, retry=False,
                              json={"usernames": chunk, "excludeBannedUsers": True})
                if r.status_code == 429:
                    waited = getattr(self, "waited", 0.0)
                    if waited + backoff > limit or getattr(self, "stop", False):
                        self.rate_limited = True
                        self.failed += 1
                        log.info("username lookups stopped (rate limit), %d not asked", len(pending))
                        break
                    log.info("Roblox turned down a lookup; trying again in %.0fs (remaining %s)",
                             backoff, r.headers.get("x-ratelimit-remaining"))
                    self._pause(backoff)
                    backoff = min(backoff * 2, 10.0)
                    continue
                backoff = 2.0
                pending = pending[len(chunk):]
                chunk_done = True
                r.raise_for_status()
                got = {}
                for d in r.json().get("data", []):
                    got[d["name"].lower()] = {"id": d["id"], "name": d["name"],
                                              "displayName": d.get("displayName", "")}
                out.update(got)
                t = time.time()
                for n in chunk:                          # remember hits and misses
                    self._mem('_names')[n] = (t, got.get(n))
                rec = getattr(self, "record", None)
                if rec is not None:
                    rec.setdefault("names", {}).update({n: got.get(n) for n in chunk})
            except Exception as e:
                self.failed += 1
                if not chunk_done:
                    pending = pending[len(chunk):]
                log.warning("username lookup batch failed: %s", e)
        # roblox_user_<number>: if the batch lookup didn't return it, the number
        # is normally the account ID, so look that up and check the name matches
        for n in names:
            m = AUTO_NAME.match(n)
            if m and n not in out:
                try:
                    r = self._req("GET", f"https://users.roblox.com/v1/users/{m.group(1)}")
                    if r.status_code == 200:
                        d = r.json()
                        if d.get("name", "").lower() == n:
                            out[n] = {"id": d["id"], "name": d["name"],
                                      "displayName": d.get("displayName", "")}
                            self._mem("_names")[n] = (time.time(), out[n])
                            if getattr(self, "record", None) is not None:
                                self.record.setdefault("names", {})[n] = out[n]
                except Exception as e:
                    log.warning("lookup by id for %s failed: %s", n, e)
        return out

    def groups(self, uid: int) -> set[int]:
        for url in (GROUPS_V2, GROUPS_V1):
            r = self._req("GET", url.format(uid=uid))
            if r.status_code == 404:
                continue
            r.raise_for_status()
            ids = {d["group"]["id"] for d in r.json().get("data", [])}
            if getattr(self, "record", None) is not None:
                self.record.setdefault("groups", {})[str(uid)] = sorted(ids)
            return ids
        return set()

    def group_name(self, gid: int) -> str:
        r = self._req("GET", f"https://groups.roblox.com/v1/groups/{gid}")
        r.raise_for_status()
        return r.json().get("name", "")

    def headshots(self, uids: list[int]) -> dict[int, Image.Image]:
        """Profile pictures that could be fetched and opened; any that can't
        (odd image format, blocked decoder, network) are just left out."""
        if not uids:
            return {}
        r = self._req("GET", HEADSHOT_URL, params={
            "userIds": ",".join(map(str, uids)), "size": "48x48",
            "format": "Png", "isCircular": "false"})
        r.raise_for_status()
        out = {}
        for d in r.json().get("data", []):
            if d.get("state") != "Completed" or not d.get("imageUrl"):
                continue
            try:
                img = self._req("GET", d["imageUrl"],
                                headers={"Accept": "image/png,image/jpeg;q=0.9"})
                out[d["targetId"]] = Image.open(BytesIO(img.content)).convert("RGBA")
                if getattr(self, "record", None) is not None:
                    self.record.setdefault("headshots", {})[d["targetId"]] = out[d["targetId"]]
            except Exception as e:
                log.warning("profile picture for %s unusable (%s, %s): %s",
                            d.get("targetId"), img.headers.get("Content-Type", "?")
                            if "img" in locals() else "?", type(e).__name__, e)
        return out


# ------------------------------------------------------------------ variants
# Lowercase confusions Tesseract makes on this font (both directions tried),
# with how likely each is (lower = tried sooner). The first group are the
# ones actually seen on real scans.
COMMON = [("o", "0"), ("q", "0"), ("s", "5"), ("l", "1"), ("i", "1"), ("l", "i"),
          ("ii", "i"), ("s", "8"), ("t", "1")]
RARER = [("b", "8"), ("z", "2"), ("g", "9"), ("q", "9"), ("rn", "m"),
         ("nn", "m"), ("vv", "w"), ("cl", "d"), ("ll", "l"), ("ii", "u"),
         ("e", "c"), ("a", "e"), ("h", "b"), ("t", "f"), ("j", "i"), ("u", "v"),
         ("0", "8"), ("6", "b"), ("7", "1"), ("3", "8")]
PAIRS = [(a, b, 1.0) for a, b in COMMON] + [(a, b, 2.0) for a, b in RARER]


def _edits(s: str):
    """Every single OCR slip that could be undone: (cost, start, end, text)."""
    out = []
    n = len(s)
    for a, b, base in PAIRS:
        # priced like the scoring model (slips()): a blind spot (O/0, l/1/i,
        # S/5) almost free, so two of them (ChronoXXVIII read ChronoxXVill)
        # come before one real misread. Ranked only by how often OCR confuses
        # the two, never by guesses about what usernames look like (digits
        # next to digits etc.): SilentRobinkip05 was once pushed out, at 295th
        cost = (BLIND if len(a) == len(b) == 1 and any({a, b} <= g for g in _BLIND_SETS)
                else SLIP_COMMON if base == 1.0 else SLIP_RARE)
        for x, y in ((a, b), (b, a)):
            k = s.find(x)
            while k != -1:
                out.append((cost, k, k + len(x), y))
                k = s.find(x, k + 1)
    for k in range(n):                                   # stray extra character
        too_long = n > 20 and not s.startswith("roblox_user")
        out.append((0.5 if too_long else ADDED * (1.0 if 0 < k < n - 1 else 1.2), k, k + 1, ""))
    for k in range(1, n):                                # underscore not seen
        if s[k - 1] != "_" and s[k] != "_":              # (also a 2nd one, which
            out.append((0.3, k, k, "_"))                 # only special names have)
    return out


def _apply(s, edits):
    for _, a, b, t in sorted(edits, key=lambda e: -e[1]):   # right to left
        s = s[:a] + t + s[b:]
    return s


def blind_swaps(reading: str) -> list[str]:
    """The reading with every copy of a blind-spot character swapped at once
    (all O as 0, all l as 1, ...), and two such swaps together. The OCR reads
    the same glyph the same way each time, so a name full of zeros comes out
    full of O's: BL00DYL0V3RSS read BLOODYLOV3RSS needs three swaps, more
    than the one- and two-slip spellings ever try (JR0001V read JROOOIV too)."""
    s = reading.lower()
    swaps = [(c, alt) for g in _BLIND_SETS for c in sorted(g) if c in s
             for alt in sorted(g) if alt != c]
    out = []
    for k, (c, alt) in enumerate(swaps):
        one = s.replace(c, alt)
        out.append(one)
        for c2, alt2 in swaps[k + 1:]:
            if c2 != c and c2 != alt:
                out.append(one.replace(c2, alt2))
    return [v for v in dict.fromkeys(out) if v != s and valid_username(v)]


def variants(reading: str, limit=250, two=True) -> list[str]:
    """Plausible usernames one OCR slip away (or exactly two, with two=True),
    most likely first."""
    s = reading.lower()
    eds = _edits(s)
    if not two:
        cands = [(c, _apply(s, [e])) for e in eds for c in [e[0]]]
    else:
        cands = []
        for x in range(len(eds)):
            for y in range(x + 1, len(eds)):
                e, f = eds[x], eds[y]
                if e[1] < f[2] and f[1] < e[2] or (e[1] == f[1] == e[2] == f[2]):
                    continue                             # overlapping
                if e[1] == e[2] and f[1] == f[2]:
                    continue                             # two underscores: no
                extra = 1.5 if e[3] == "" and f[3] == "" else 0.0   # two strays: rare
                cands.append((e[0] + f[0] + extra, _apply(s, [e, f])))
    cands.sort(key=lambda c: c[0])
    seen, out = {s}, []
    for _, v in cands:
        if v not in seen:
            seen.add(v)
            if valid_username(v):
                out.append(v)
                if len(out) >= limit:
                    break
    return out


# ------------------------------------------------------------------ scoring
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# letters whose capital and small forms look alike in Roblox's font, so the
# OCR can't be trusted on which one it saw
_CASE_LOOKALIKE = set("cosuvwxzkpil")


def _ratio(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# the OCR's blind spots in Roblox's font: O/0, l/I/1, S/5 look alike, and a
# thin underscore on the baseline is easily lost
_BLIND = str.maketrans({"0": "o", "1": "l", "i": "l", "5": "s"})


def _skeleton(s: str) -> str:
    """A name with its blind spots evened out ("Z3FR0_44" -> "z3fro44")."""
    return (s or "").lower().translate(_BLIND).replace("_", "")


def _lev(a: str, b: str) -> int:
    a, b = a.lower(), b.lower()
    d = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        p, d[0] = d[0], i
        for j, cb in enumerate(b, 1):
            p, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, p + (ca != cb))
    return d[-1]


_TWIN_SWAPS = {"l": "1i", "1": "li", "i": "l1", "o": "0", "0": "o", "s": "5", "5": "s"}


def _blind_twins(name: str, underscores: bool = True) -> list[str]:
    """Usernames one lookalike character away from `name` (l/1/I/i, O/0,
    S/5 swapped, or an underscore dropped or added): the accounts OCR could
    have mistaken this one for, or this one for them. Both directions of the
    underscore: OCR loses them, so a name read without one may really have
    one (liz48566 on screen as Liz_48566, display read "Liz 48566")."""
    n = name.lower()
    out = []
    for k, c in enumerate(n):
        for alt in _TWIN_SWAPS.get(c, ""):
            out.append(n[:k] + alt + n[k + 1:])
        if c == "_":
            out.append(n[:k] + n[k + 1:])
        elif underscores and "_" not in n and 0 < k:
            out.append(n[:k] + "_" + n[k:])
    return [v for v in dict.fromkeys(out) if v != n and valid_username(v)]


UNKNOWN_DISPLAY = 0.6   # display name OCR can't read (another script): no evidence either way


def _unreadable(display: str) -> bool:
    """A display name mostly in a script the OCR can't read (Japanese, etc.)."""
    chars = [c for c in display or "" if not c.isspace()]
    return bool(chars) and sum(c.isascii() for c in chars) < len(chars) / 2


def _garbled(p) -> bool:
    """The display line read as junk ('Ci)' for 'Cjj', 'NR* O' for Japanese):
    characters no display name is made of. A clean read, even a short one
    ('Jo' when the account says 'Ky'), is still evidence."""
    d = p.display_reading or ""
    return any(not (c.isalnum() or c in " _.'-") for c in d)


# ------------------------------------------------------------ evidence model
# One score per candidate account from all the evidence on the row, and one
# rule for accepting it. The pieces:
#   username line: how many OCR slips turn the account's name into what was
#     read, each slip priced by how often the OCR really makes it (a lookalike
#     O/0 is cheap, a lost underscore cheap, an invented one very dear),
#     averaged over the readings by their votes, so a minority reading still
#     counts (bimaaeeee1 read "bimaaeeeet" twice is not "bimaaeeee")
#   display line: the same slips against the account's display name; "can't
#     tell" when it's in a script the OCR can't read or read too badly to mean
#     anything
#   profile picture: only between accounts the names can't separate
BLIND = 0.05             # a slip the OCR can't see at all in this font: O/0,
                         # l/1/I/i, S/5, a lost underscore. All reads are of the same
                         # pixels, so even 5 votes for one can't rule the other out
SLIP_COMMON = 0.4        # a lookalike it often gets wrong but can tell (q/0, t/1...)
SLIP_RARE = 0.8          # one it sometimes gets wrong (rn/m, B/8...)
LOST_UNDERSCORE = BLIND
DISPLAY_LOST_UNDERSCORE = 0.6
LOST_NARROW = 0.5        # a thin l, i or 1 it didn't see
LOST_OTHER = 1.0         # any other character it didn't see
ADDED = 1.0              # a character it saw that isn't there
ADDED_UNDERSCORE = 1.5   # it hardly ever invents underscores
CASE_CLASH = 0.5         # a letter clearly read in the other case
OTHER = 1.0              # any other misread

DISPLAY_WEIGHT = 1.25    # the display line is bigger text: its slips count more
DISPLAY_CAP = 3.0        # a display name that's simply different costs this much
DISPLAY_UNKNOWN = 1.5    # ... and one the OCR can't read or read too badly
MARGIN = 0.4             # lead (in slips) the chosen account needs over any other
CONFIDENT = 0.80         # display-name match that vouches for an account


_BLIND_SETS = [set("o0"), set("l1i"), set("s5")]
# what the OCR turns letters into on the display line when it reads junk
_DISPLAY_JUNK = {("l", "!"): 0.4, ("i", "!"): 0.4, ("1", "!"): 0.4, ("l", "|"): 0.4,
                 ("i", "|"): 0.4, ("1", "|"): 0.4, ("8", "&"): 0.4, ("s", "$"): 0.4,
                 ("5", "$"): 0.4, ("a", "@"): 0.4,
                 ("1", "'"): 0.4, ("l", "'"): 0.4, ("i", "'"): 0.4,    # a thin glyph read as a mark
                 ("z", "2"): 0.2}      # "zzz" read "222" every time (WellExecuted, DividedPixel)           # "zzz" read "222" (WellExecuted, DividedPixel)

def _tables():
    single, multi = {}, []
    for a, b, base in PAIRS:
        c = SLIP_COMMON if base == 1.0 else SLIP_RARE
        for x, y in ((a, b), (b, a)):
            if len(x) == 1 and len(y) == 1:
                if any({x, y} <= g for g in _BLIND_SETS):
                    c = BLIND
                single[(x, y)] = min(c, single.get((x, y), 9.0))
            else:
                multi.append((x, y, c))
    return single, multi


_SINGLE, _MULTI = _tables()


def slips(read: str, name: str, display: bool = False) -> float:
    """Priced OCR slips that turn `name` (as shown on screen) into `read`."""
    return _slips(read or "", name or "", display)


@lru_cache(maxsize=200000)
def _slips(r, n, display):
    rl, nl = r.lower(), n.lower()
    R_, N_ = len(r), len(n)
    INF = 1e9
    d = [[INF] * (N_ + 1) for _ in range(R_ + 1)]
    d[0][0] = 0.0

    def lost(j):
        c = nl[j]
        if c == "_":       # the display line is big enough to show it (as _ . or space)
            return DISPLAY_LOST_UNDERSCORE if display else LOST_UNDERSCORE
        if display and c in " .'-":
            return 0.15
        return LOST_NARROW if c in "il1" else LOST_OTHER

    def added(i):
        c = rl[i]
        if display and c in " .,'`-:;|!":
            return 0.3
        return ADDED_UNDERSCORE if c == "_" else ADDED

    for i in range(R_ + 1):
        for j in range(N_ + 1):
            cur = d[i][j]
            if cur >= INF:
                continue
            if i < R_ and j < N_:
                a, b = rl[i], nl[j]
                if a == b:
                    c = CASE_CLASH if (r[i] != n[j] and a.isalpha()
                                       and a not in _CASE_LOOKALIKE) else 0.0
                elif display and {a, b} <= {".", "_", " "}:
                    c = 0.15
                elif display and (b, a) in _DISPLAY_JUNK:
                    c = _DISPLAY_JUNK[(b, a)]
                else:
                    c = _SINGLE.get((b, a), OTHER)
                if cur + c < d[i + 1][j + 1]:
                    d[i + 1][j + 1] = cur + c
            if j < N_:
                v = cur + lost(j)
                if v < d[i][j + 1]:
                    d[i][j + 1] = v
            if i < R_:
                v = cur + added(i)
                if v < d[i + 1][j]:
                    d[i + 1][j] = v
            for x, y, c in _MULTI:
                if nl.startswith(x, j) and rl.startswith(y, i):
                    v = cur + c
                    if v < d[i + len(y)][j + len(x)]:
                        d[i + len(y)][j + len(x)] = v
    return d[R_][N_]


def user_slips(p, name: str) -> float:
    """Slips per reading, averaged over the username readings by their votes."""
    reads = p.user_votes[:5]
    total = sum(max(n, 1) for _, n in reads)
    if not total:
        return 5.0
    return sum(max(n, 1) * slips(r, name) for r, n in reads) / total


def display_slips(p, acct):
    """Slips between the account's display name and the display line read
    (the best of its readings), or None when the line can't tell."""
    D = acct["displayName"] or ""
    reads = [v for v, _ in p.display_votes[:3] if v]
    if _unreadable(D) or not reads:
        return None
    best = min(slips(v, D, display=True) for v in reads)
    if TRUST_USERNAME and _garbled(p) and best > (1 - CONFIDENT) * max(len(D.replace(" ", "")), 3):
        return None          # read too badly to argue against it; a close match still counts
    return best


def evidence(p, acct) -> dict:
    """Everything the row says about one account: its cost (expected slips,
    lower is likelier), how well it fits the row on its own (0..1), and
    whether its display line vouches for it independently."""
    u = user_slips(p, acct["name"])
    ds = display_slips(p, acct)
    D = (acct["displayName"] or "").replace(" ", "")
    dm = None if ds is None else max(0.0, 1.0 - ds / max(len(D), 3))
    cost = u + DISPLAY_WEIGHT * (DISPLAY_UNKNOWN if ds is None else min(ds, DISPLAY_CAP))
    # the username readings are this account apart from blind spots (or a
    # slip or so in a long name)
    explained = u <= max(EARLY, 0.04 * len(acct["name"]))
    # fits the row on its own: its display name matches what was read, or
    # the username readings are it and the display line doesn't argue
    fits = (dm is not None and dm >= CONFIDENT) or (explained and (dm is None or dm >= 0.6))
    distinctive = (dm is not None and dm >= CONFIDENT
                   and _skeleton(acct["displayName"]) != _skeleton(acct["name"]))
    return {"cost": cost, "fits": fits, "explained": explained, "user": u, "display": dm,
            "distinctive": distinctive}


def avatar_loaded(crop) -> bool:
    """False for the flat grey circle Roblox shows before a profile picture
    loads (its middle is one solid colour); comparing that with an account's
    picture would just be a coin flip."""
    if crop is None:
        return False
    a = np.asarray(crop.convert("L"), float)
    h, w = a.shape
    return float(a[h // 4:3 * h // 4, w // 4:3 * w // 4].std()) > 5.0


def avatar_similarity(crop: Image.Image, headshot: Image.Image) -> float:
    """Compare the captured avatar circle with an account's headshot."""
    size = 24
    bg = Image.new("RGBA", headshot.size, (55, 57, 63, 255))
    hs = Image.alpha_composite(bg, headshot).convert("RGB").resize((size, size))
    cr = crop.convert("RGB").resize((size, size))
    yy, xx = np.mgrid[:size, :size]
    inside = (xx - size / 2 + 0.5) ** 2 + (yy - size / 2 + 0.5) ** 2 < (0.42 * size) ** 2
    a = np.asarray(cr, float)[inside].ravel()
    b = np.asarray(hs, float)[inside].ravel()
    a, b = a - a.mean(), b - b.mean()
    n = np.linalg.norm(a) * np.linalg.norm(b)
    return float(a @ b / n) if n else 0.0


# ------------------------------------------------------------------ groups
def check_groups(players, group_id, api=None, progress=None,
                 exclude_not_in_group=True):
    """(Re)apply group membership for a group. Each account's full group list
    is fetched once and kept on the player, so switching groups afterwards
    needs no more requests. Players you edited by hand are left alone, and
    a name you put back in or left out yourself stays that way."""
    ps = [p for p in players if not p.unread and p.status != "edited"
          and p.base_status]
    for p in ps:                                  # undo the previous group's verdict
        p.status, p.note = p.base_status, p.base_note
        # left out unless you say otherwise: no real account found (nothing
        # worth copying), or not in the group (set below)
        p.default_out = not p.user_id
        p.excluded = p.default_out if p.user_choice is None else p.user_choice
    if not group_id:
        return players
    need = [p for p in ps if p.user_id and p.group_ids is None]
    if need:
        if progress:
            progress("Checking group membership…")
        api = api or RobloxAPI()

        def fetch(p):
            try:
                return p, api.groups(p.user_id)
            except Exception as e:
                log.warning("group list for %s failed: %s", p.user_id, e)
                return p, None
        with ThreadPoolExecutor(max_workers=4) as ex:
            for p, ids in ex.map(fetch, need):
                p.group_ids = ids
    for p in ps:
        if not p.user_id:
            continue
        if p.group_ids is None:
            p.note = (p.note + "; " if p.note else "") + "group check failed"
        elif group_id not in p.group_ids:
            if p.status == "check":              # keep the warning visible
                p.note = (p.note + "; " if p.note else "") + "not in the group"
            else:
                p.status = "not_in_group"
            if exclude_not_in_group:
                p.default_out = True
                if p.user_choice is None:
                    p.excluded = True
    return players


# ------------------------------------------------------------------ main
TRUST_USERNAME = True  # a garbled display line is "can't tell", not a mismatch
RATE_LIMIT_WAIT = None # seconds a check may spend waiting out Roblox's rate limit (None: as long as it takes)
BUSY_NOTE = ("the check was stopped before every lookalike was tried "
             "(rescan to finish it)")
LOOKALIKES = True      # try lookalike spellings when nothing fits
LOOKALIKE_LIMIT = 120  # spellings tried per reading in the first round
TWO_SLIP = True        # second round: two mistakes in one name
SEARCH_DEPTH = 125     # lookalike spellings tried per name (one slip; two slips get ~half)


def _cap(two):
    """Spellings per name in a round. On every recorded scan the right account
    was within the first 103 one-slip and 36 two-slip spellings."""
    return max(10, round(SEARCH_DEPTH * (0.53 if two else 1)))
AVATAR_TIEBREAK = True # compare profile pictures between close candidates
AVATAR_MATCH = 0.80    # profile-picture similarity that counts as the same picture
EARLY = 0.3            # slips a pick may need before lookalike spellings are tried first


def judge(p, found, asked, final=False) -> str:
    """The one decision. Every account that could be this row is scored on
    all the evidence (see evidence()); the best is accepted when it scores
    well enough, leads every other account clearly, and nothing that could
    still beat it is unasked:
      ok     accepted
      none   no account fits
      weak   the best fit doesn't score well enough (display name differs...)
      tie    another account is too close (lookalike twins, ...)
      twins  its lookalike twins still need asking about
      wait   it needed real slips: try lookalike spellings first
      busy   ... but the rate limit cut that search short
    """
    reads = [r for r, _ in p.user_votes[:5]] or [p.reading]
    out = []
    for n in sorted(p._names):           # fixed order: ties sort the same every run
        a = found.get(n)
        # close to something actually read (a couple of characters off at
        # most, more for long names), never just the display name
        if a and min(_lev(n, r) for r in reads) <= max(2, len(n) // 4):
            out.append((evidence(p, a), a))
    out.sort(key=lambda x: x[0]["cost"])
    p._scored = out
    if not out:
        return "none"
    e1, a1 = out[0]
    lead = out[1][0]["cost"] - e1["cost"] if len(out) > 1 else 9.0
    if not e1["fits"]:
        return "weak"
    if lead < MARGIN:
        return "tie"
    # an account that differs only by a lookalike character must have been
    # asked about: OCR can't tell them apart. Its lookalike twins, and the top
    # reading's if that isn't it exactly (BeanO7 read: Bean07 and BeanO_7).
    # An underscore added is only a question when the display line can't
    # tell, or shows the username too and read a gap somewhere (Liz 48566):
    # a display name of its own is never shared by an underscore twin in the
    # recordings, while 7 of 753 character twins share one (impostors copy
    # it: neongoo51 / neonGoo5I, both "Neongoo")
    self_named = _skeleton(a1["displayName"]) == _skeleton(a1["name"])
    gap = any(c in v for v, _ in p.display_votes[:3] for c in " ._-")
    need = _blind_twins(a1["name"], underscores=e1["display"] is None or (self_named and gap))
    top = p.user_votes[0][0] if p.user_votes else ""
    if top and top.lower() != a1["name"].lower():
        need += _blind_twins(top)
    fresh = [n for n in need if n not in p._names]
    p._names |= set(need)
    if any(n in found for n in fresh):      # already known from another row
        return judge(p, found, asked, final)
    need = [n for n in need if n not in asked]
    if need:
        p._need = need
        return "twins"
    # it needed real slips (not just blind spots): another account might
    # need fewer, so the lookalike spellings get tried before accepting it
    if not e1["explained"] and not e1["distinctive"]:
        if getattr(p, "_incomplete", False):
            return "busy"
        if not final:
            return "wait"
    return "ok"


def _write(p, s):
    """Fill in a player's result from its judged state (see judge())."""
    if p._scored:                    # for "Show confidence scores"
        e = p._scored[0][0]
        nxt = p._scored[1] if len(p._scored) > 1 else None
        a1 = p._scored[0][1]
        p.evidence = {"user": round(e["user"], 2), "display": e["display"],
                      "cost": round(e["cost"], 2), "name": a1["name"],
                      "display_name": a1["displayName"],
                      "lead": None if nxt is None else round(nxt[0]["cost"] - e["cost"], 2),
                      "next": None if nxt is None else nxt[1]["name"],
                      "next_display_name": None if nxt is None else nxt[1]["displayName"],
                      "next_user": None if nxt is None else round(nxt[0]["user"], 2),
                      "next_display": None if nxt is None else nxt[0]["display"],
                      "lookalike": nxt is not None and _skeleton(a1["name"]) == _skeleton(nxt[1]["name"]),
                      "read": p.reading, "display_read": p.display_reading,
                      "picture": bool(getattr(p, "_by_avatar", False)),
                      "pictures": getattr(p, "_pic", None),
                      "state": s, "fits": e["fits"], "explained": e["explained"],
                      "reads": [(r, n) for r, n in p.user_votes[:3]]}
    if p._scored:
        e, a = p._scored[0]
        p.username, p.user_id = a["name"], a["id"]
        p.display_name = a["displayName"]
        p.alternates = [x[1]["name"] for x in p._scored[1:4]]
        if s == "ok":
            p.status = "verified"
            p.note = "matched by profile picture" if getattr(p, "_by_avatar", False) else ""
        else:
            p.status = "check"
            if s == "tie":
                b = p._scored[1][1]
                p.note = (f"lookalike accounts ({a['name']} / {b['name']}) that the "
                          "screen can't tell apart"
                          if _skeleton(a["name"]) == _skeleton(b["name"])
                          else "several accounts fit")
            elif s == "weak":
                p.note = ("display name differs" if e["display"] is not None
                          else "the name didn't read clearly enough")
            else:
                p.note = "its lookalike accounts couldn't all be checked"
            if getattr(p, "_incomplete", False):
                p.note += "; " + BUSY_NOTE
    else:
        p.status, p.username = "check", p.reading
        p.note = "no matching account"
        if getattr(p, "_incomplete", False):
            p.note += " found yet; " + BUSY_NOTE


def snapshot(players, st) -> list:
    """Copies of the players as they stand mid-check, for showing progress:
    settled rows as they'll end up (their group already checked), the rest
    "checking" with the best account found so far."""
    out = []
    for p in players:
        q = copy.copy(p)
        sc = getattr(p, "_scored", None)
        if p.index in st and not p.unread and st[p.index] != "ok":
            if sc:
                a = sc[0][1]
                q.username, q.display_name = a["name"], a["displayName"]
            else:
                q.username = p.reading
            q.status, q.note = "checking", ""
            q.excluded = True        # not copied until it's settled
        out.append(q)
    return out


def verify(players, group_id: int | None = None, api=None, progress=None,
           exclude_not_in_group=True, partial=None):
    """Fill in username/status/note on each Player (in place). `partial`, if
    given, is called with snapshot() copies as the check goes."""
    todo = [p for p in players if not p.unread and p.user_votes]
    if not todo:
        return players
    api = api or RobloxAPI()
    tm = {"start": time.time()}
    if isinstance(api, RobloxAPI):
        api.wait_limit = float("inf") if not RATE_LIMIT_WAIT else float(RATE_LIMIT_WAIT)
        api.waited, api.rate_limited, api.stop = 0.0, False, False
    found, asked = {}, set()

    def say(msg):
        if progress:
            progress(msg)

    def ask(names, label=None) -> bool:
        """Look names up (once each). False if the rate limit cut it short."""
        new = [n for n in dict.fromkeys(x.lower() for x in names) if n not in asked]
        asked.update(n for n in new if not valid_username(n))   # nothing to ask
        new = [n for n in new if valid_username(n)]
        chunks = [new[i:i + 100] for i in range(0, len(new), 100)]
        for b, chunk in enumerate(chunks, 1):
            if getattr(api, "rate_limited", False) or getattr(api, "stop", False):
                log.info("lookups stopped by Roblox's rate limit (%d of %d batches done)",
                         b - 1, len(chunks))
                return False
            if label:
                say(f"{label}… {b}/{len(chunks)}")
            found.update(api.lookup(chunk))
            if getattr(api, "failed", 0):
                return False
            asked.update(chunk)
        return True

    # 1. every reading, plus the display line when it could itself be the
    #    username (people without a display name show their username)
    say("Looking up usernames…")
    first = []
    for p in todo:
        p._names = {r.lower() for r, _ in p.user_votes[:6]}
        d = p.display_reading.replace(" ", "")
        if d:
            p._names.add(d.lower())
        first += [r for r, _ in p.user_votes[:6]]
        if valid_username(d) and _ratio(d, p.reading) > 0.6:
            first.append(d)
    ask(first)
    tm["lookup"] = time.time()

    # 2. one queue for everything still to ask, most likely first across all
    #    players: a pick's lookalike twins, and lookalike spellings (one slip,
    #    then two) for rows with no good account yet. When Roblox's rate limit
    #    cuts it short, what got asked is what was likeliest to matter.
    st = {p.index: judge(p, found, asked) for p in todo}

    def waiting(secs):
        done = sum(v == "ok" for v in st.values())
        say(f"Roblox's rate limit: waiting for it to lift ({secs:.0f} s so far)… "
            f"{done} of {len(todo)} settled, {len(asked)} names asked")
    if isinstance(api, RobloxAPI):
        api.on_wait = waiting
    spell = {}            # p.index -> spellings still to ask, likeliest first
    rounds = {}           # p.index -> spelling rounds started (1: one slip, 2: two)

    def wants_spellings(p, s):
        if not LOOKALIKES or s in ("ok", "twins"):
            return False
        # a tie between accounts the readings already explain (lookalike
        # twins) can't be helped by more spellings
        if s == "tie" and len(p._scored) > 1 and p._scored[1][0]["explained"]:
            return False
        return True

    def next_spellings(p):
        """This row's spellings still worth asking, starting the next round
        when one runs out. Done when nothing left could come within the
        margin of the best account found (it would need more slips)."""
        while True:
            left = [v for v in spell.get(p.index, []) if v not in asked]
            if left and p._scored and user_slips(p, left[0]) >= p._scored[0][0]["cost"] + MARGIN:
                left = []
            spell[p.index] = left
            if left:
                return left
            r = rounds.get(p.index, 0)
            if r >= (2 if TWO_SLIP else 1):
                return []
            two = r == 1
            if two and p._scored and p._scored[0][0]["explained"] and p._scored[0][0]["fits"]:
                return []        # the expensive round: not for a name the readings explain
            rounds[p.index] = r + 1
            vs = []
            for rd, _ in p.user_votes[:3]:
                if not two:
                    vs += blind_swaps(rd)
                vs += variants(rd, limit=int(max(LOOKALIKE_LIMIT, SEARCH_DEPTH) * (1.25 if two else 1)),
                               two=two)
            vs = [v for v in dict.fromkeys(vs) if v not in asked]
            # only the likeliest few (see _cap)
            spell[p.index] = sorted(vs, key=lambda v: user_slips(p, v))[:_cap(two)]
            p._names |= set(spell[p.index])

    shown = set()

    def show():
        """Settled rows get their result and group check now (same requests
        as at the end, just sooner), then the list is updated."""
        new = [p for p in todo if st[p.index] == "ok" and p.index not in shown]
        for p in new:
            shown.add(p.index)
            _write(p, "ok")
            p.base_status, p.base_note = p.status, p.note
        if new and group_id:
            check_groups(new, group_id, api=api, exclude_not_in_group=exclude_not_in_group)
        if partial:
            partial(snapshot(players, st))

    show()
    final, cut = False, False
    while True:
        pending = [p for p in todo if st[p.index] != "ok"]
        req = []                          # (priority, likelihood, turn, name)
        for p in pending:
            s = st[p.index]
            if s == "twins":
                p._names |= set(p._need)
                # a row's twins together: it's settled only once all are asked
                req += [(0, p.index, k, n) for k, n in enumerate(p._need)]
            elif wants_spellings(p, s):
                # twin checks first: a dozen lookups settle a row that's
                # already found, while a row still unfound may need many. If
                # the lookups get cut short, that settles the most rows (every
                # recording, cut at 300 lookups a scan: 1841 right, against
                # 1832 asking in the old order and 1701 spellings first)
                req += [(1, user_slips(p, n), k, n) for k, n in enumerate(next_spellings(p)[:25])]
        batch = []
        for *_, n in sorted(req):
            if n not in asked and n not in batch:
                batch.append(n)
                if len(batch) == 100:
                    break
        if batch and not cut:
            done = sum(st[p.index] == "ok" for p in todo)
            say(f"Checking names with Roblox… {done} of {len(todo)} settled, "
                f"{len(asked)} names asked")
            if not ask(batch):
                cut = True               # rate limit: an account we didn't hear
                for p in pending:        # about could be the right one
                    if st[p.index] == "twins" or spell.get(p.index):
                        p._incomplete = True
            for p in pending:
                st[p.index] = judge(p, found, asked, final)
            show()
            if not cut:
                continue
        if final:
            break
        final = True                      # everything asked: the last word
        for p in pending:
            st[p.index] = judge(p, found, asked, final)
        show()
    pending = [p for p in todo if st[p.index] != "ok"]
    tm["lookalikes"] = time.time()

    # 3. profile pictures, only between accounts the names can't separate
    for p in pending:                    # for the evidence tooltip
        if st[p.index] == "tie":
            p._pic = ("off" if not AVATAR_TIEBREAK else
                      "not loaded" if not avatar_loaded(p.avatar) else "failed")
    ties = [p for p in pending if AVATAR_TIEBREAK and st[p.index] == "tie"
            and avatar_loaded(p.avatar)]
    if ties:
        say("Comparing profile pictures…")
        try:
            def rivals(p):
                top = p._scored[0][0]["cost"]
                return [a for k, (e, a) in enumerate(p._scored[:4])
                        if k < 2 or e["cost"] - top < MARGIN]
            uids = sorted({a["id"] for p in ties for a in rivals(p)})
            heads = {}
            for i in range(0, len(uids), 50):
                heads.update(api.headshots(uids[i:i + 50]))
            for p in ties:
                sims = sorted(((avatar_similarity(p.avatar, heads[a["id"]]), a)
                               for a in rivals(p) if a["id"] in heads),
                              key=lambda x: x[0], reverse=True)
                p._pic = {a["name"]: round(sm, 2) for sm, a in sims}
                for a in rivals(p):
                    p._pic.setdefault(a["name"], None)      # no picture from Roblox
                # the winner must really look like the screen's picture, not
                # just less unlike it than the others (lostsoull999 "won" at
                # 0.23); every correct picture decision so far scored 0.91+
                if (len(sims) >= 2 and sims[0][0] >= AVATAR_MATCH
                        and sims[0][0] - sims[1][0] > 0.15):
                    best = sims[0][1]
                    p._scored = [x for x in p._scored if x[1] is best] + \
                                [x for x in p._scored if x[1] is not best]
                    p._by_avatar = True
                    st[p.index] = "ok"
                    pending.remove(p)
        except Exception as e:
            log.warning("profile-picture tiebreak skipped: %s", e)
    tm["pictures"] = time.time()

    # 4. write results
    for p in todo:
        _write(p, st[p.index])

    # 5. group membership
    for p in todo:
        p.base_status, p.base_note = p.status, p.note
    check_groups(players, group_id, api=api, progress=progress,
                 exclude_not_in_group=exclude_not_in_group)
    tm["groups"] = time.time()
    if isinstance(api, RobloxAPI):
        api.wait_limit, api.on_wait = None, None
    log.info("verify: %d players in %.1fs (usernames %.1fs, lookalikes %.1fs, "
             "pictures %.1fs, groups %.1fs; %d names looked up)",
             len(todo), tm["groups"] - tm["start"], tm["lookup"] - tm["start"],
             tm["lookalikes"] - tm["lookup"], tm["pictures"] - tm["lookalikes"],
             tm["groups"] - tm["pictures"], len(asked))
    for p in todo:                       # a record for tuning, in nameocr.log
        log.info("verify #%d read=%s disp=%r -> %s [%s] %s | candidates: %s",
                 p.index, [r for r, _ in p.user_votes[:3]], p.display_reading,
                 p.username, p.status, p.note,
                 [(a["name"], a["displayName"], round(e["cost"], 2),
                   None if e["display"] is None else round(e["display"], 2), round(e["user"], 2))
                  for e, a in getattr(p, "_scored", [])[:3]])
    for p in todo:
        for attr in ("_scored", "_names", "_by_avatar", "_incomplete", "_need", "_pic"):
            if hasattr(p, attr):
                delattr(p, attr)
    return players
