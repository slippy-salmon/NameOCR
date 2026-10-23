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

import difflib
import logging
import re
import time
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

    def _mem(self, which):
        return self.__dict__.setdefault(which, {})

    def _req(self, method, url, **kw):
        """A request that rides out Roblox's rate limit. During a verify
        (wait_limit set) it keeps retrying until that many seconds have been
        spent waiting in total, then gives up and sets rate_limited."""
        delay, attempts = 1.0, 0
        while True:
            r = self.s.request(method, url, timeout=self.timeout, **kw)
            if r.status_code != 429 and r.status_code < 500:
                return r
            attempts += 1
            wait = min(float(r.headers.get("Retry-After", delay)), 10)
            limit = getattr(self, "wait_limit", None)
            waited = getattr(self, "waited", 0.0)
            if limit is not None and waited + wait > limit:
                self.rate_limited = True
                log.info("Roblox said %s (rate limit); already waited %.0fs of %.0fs allowed, giving up",
                         r.status_code, waited, limit)
                break
            if limit is None and attempts > 5:
                break
            log.info("Roblox said %s (busy/rate limit); waiting %.0fs", r.status_code, wait)
            time.sleep(wait)
            self.waited = waited + wait
            delay = min(delay * 2, 10)
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
        batches = [names[i:i + 100] for i in range(0, len(names), 100)]
        for b, chunk in enumerate(batches, 1):
            if progress and len(batches) > 1:
                progress(b, len(batches))
            try:
                r = self._req("POST", USERS_URL,
                              json={"usernames": chunk, "excludeBannedUsers": True})
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
        for x, y in ((a, b), (b, a)):
            k = s.find(x)
            while k != -1:
                cost = base
                if len(x) == 1 and len(y) == 1:
                    near = s[max(0, k - 1):k] + s[k + 1:k + 2]
                    if y.isdigit() != x.isdigit():
                        # a letter among digits was probably a digit, and
                        # a digit among letters probably a letter
                        fits = any(c.isdigit() == y.isdigit() and c.isalnum() for c in near)
                        cost *= 0.6 if fits else 1.3
                out.append((cost, k, k + len(x), y))
                k = s.find(x, k + 1)
    for k in range(n):                                   # stray extra character
        too_long = n > 20 and not s.startswith("roblox_user")
        out.append((0.5 if too_long else (1.5 if 0 < k < n - 1 else 1.8), k, k + 1, ""))
    for k in range(1, n):                                # underscore not seen
        if s[k - 1] != "_" and s[k] != "_":              # (also a 2nd one, which
            out.append((1.5, k, k, "_"))                 # only special names have)
    return out


def _apply(s, edits):
    for _, a, b, t in sorted(edits, key=lambda e: -e[1]):   # right to left
        s = s[:a] + t + s[b:]
    return s


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
_DIGIT_AS = str.maketrans({"0": "o", "1": "l", "5": "s"})


def _case_clash(screen: str, account: str) -> int:
    """Letters read clearly in one case on screen but the other in the
    account's name ('ry_ujin' read, account 'Ry_ujin': 1). Roblox shows names
    with their real capitals and usernames can't differ only by capitals, so
    a clash means the OCR slipped or, more often, it's a different account
    (Ry_ujin vs ry_uj1n, cownola vs a "CowNola" on screen)."""
    a = (screen or "").translate(_DIGIT_AS).replace("_", "").replace(" ", "")
    b = (account or "").translate(_DIGIT_AS).replace("_", "").replace(" ", "")
    if len(a) != len(b):
        return 0
    return sum(1 for x, y in zip(a, b)
               if x != y and x.lower() == y.lower() and x.lower() not in _CASE_LOOKALIKE)


def _same_name(reading: str, account: str) -> bool:
    """The reading is this account's name exactly (capitals that clash don't count)."""
    return reading.lower() == account.lower() and not _case_clash(reading, account)


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


def _slip_dist(name: str, reading: str) -> float:
    """Edit distance where a blind-spot slip counts a quarter: dropping a
    clearly read character is far less likely than missing an underscore."""
    full = _lev(name, reading)
    core = _lev(_skeleton(name), _skeleton(reading))
    return core + 0.25 * max(0, full - core)


def _lev(a: str, b: str) -> int:
    a, b = a.lower(), b.lower()
    d = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        p, d[0] = d[0], i
        for j, cb in enumerate(b, 1):
            p, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, p + (ca != cb))
    return d[-1]


def _twins(p, pool) -> list[dict]:
    """Accounts in pool that are a reading of p apart from blind-spot slips."""
    skel = {_skeleton(r) for r, _ in p.user_votes[:3]}
    return [a for a in pool.values() if _skeleton(a["name"]) in skel]


_TWIN_SWAPS = {"l": "1i", "1": "li", "i": "l1", "o": "0", "0": "o", "s": "5", "5": "s"}


def _blind_twins(name: str) -> list[str]:
    """Usernames one lookalike character away from `name` (l/1/I/i, O/0,
    S/5 swapped, or an underscore dropped): the accounts OCR could have
    mistaken this one for, or this one for them."""
    n = name.lower()
    out = []
    for k, c in enumerate(n):
        for alt in _TWIN_SWAPS.get(c, ""):
            out.append(n[:k] + alt + n[k + 1:])
        if c == "_":
            out.append(n[:k] + n[k + 1:])
    return [v for v in dict.fromkeys(out) if v != n and valid_username(v)]


def _self_named(acct) -> bool:
    """The account's display name is just its username (apart from lookalike
    characters), so reading the display line only reads the same text again:
    no independent check on a lookalike misread."""
    return _skeleton(acct["displayName"]) == _skeleton(acct["name"])


def _indistinct(p, a, b) -> bool:
    """Two accounts OCR can't tell apart on this row: usernames differ only by
    lookalike characters, and so do their display names, with neither one's
    capitals clashing with the screen more than the other's
    (nhilyyy / nhllyyy, Minxm9 / Mlnxm9, Columbina3 / co1umbina3)."""
    if a is b or _skeleton(a["name"]) != _skeleton(b["name"]):
        return False
    # OCR loses underscores but never invents them: an underscore that was
    # read rules out the account without it (abesh_43, not abesh43)
    ru = max((r.count("_") for r, _ in p.user_votes[:3]), default=0)
    if (a["name"].count("_") >= ru) != (b["name"].count("_") >= ru):
        return False
    da, db = a["displayName"], b["displayName"]
    rd = max((v.count("_") for v, _ in p.display_votes[:3] if v), default=0)
    if (da.count("_") >= rd) != (db.count("_") >= rd):
        return False
    if _unreadable(da) and _unreadable(db):
        return True
    if _skeleton(da) != _skeleton(db):
        return False
    seen = [v for v, _ in p.display_votes[:3] if v] or [p.display_reading or ""]
    return all(_case_clash(v, da) == _case_clash(v, db) for v in seen)


def _settled_tie(p, pool) -> bool:
    tw = _twins(p, pool)
    return len(tw) >= 2 and any(score(p, a)[1] >= CONFIDENT for a in tw)


UNKNOWN_DISPLAY = 0.6   # display name OCR can't read (another script): no evidence either way


def _unreadable(display: str) -> bool:
    """A display name mostly in a script the OCR can't read (Japanese, etc.)."""
    chars = [c for c in display or "" if not c.isspace()]
    return bool(chars) and sum(c.isascii() for c in chars) < len(chars) / 2


def _disp_sim(display: str, readings) -> float:
    """How well an account's display name matches the display line read off
    the screen. Blind-spot slips (N01R read as NOR) cost only a little."""
    if _unreadable(display):
        return UNKNOWN_DISPLAY
    return max((max(0.0, max(_ratio(display, d), 0.97 * _ratio(_skeleton(display), _skeleton(d)))
                     - 0.25 * _case_clash(d, display))
                for d in readings), default=0.5)


def _weak_display(p) -> bool:
    """The display line didn't read cleanly (very short, or junk characters
    like 'Ci)' for 'Cjj'), so a mismatch there means little."""
    d = (p.display_reading or "").replace(" ", "")
    alnum = sum(c.isalnum() for c in d)
    return alnum < 4 or (len(d) - alnum - d.count("_")) > 0


def _garbled(p, acct) -> bool:
    """The display line looks like a bad read OF THIS ACCOUNT's display name,
    not a different name: it read poorly (short, or junk characters) and still
    shares something with it ("Ci)" for "Cjj"). A clean short read that simply
    differs ("Jo" on screen, account says "Ky") is real evidence and doesn't count."""
    if not TRUST_USERNAME or not _weak_display(p):
        return False
    d = acct["displayName"]
    return _unreadable(d) or max((_ratio(_skeleton(d), _skeleton(v))
                                  for v, _ in p.display_votes[:3]), default=0) >= 0.4


def score(p, acct) -> tuple[float, float]:
    """(overall score, display-name similarity) of an account for a player."""
    disp = [v for v, _ in p.display_votes[:3]]
    dsim = _disp_sim(acct["displayName"], disp)
    reads = p.user_votes[:5]
    total = sum(max(n, 1) for _, n in reads) or 1
    dist = min((_slip_dist(acct["name"], r) + 0.5 * _case_clash(r, acct["name"])
                for r, _ in reads), default=5)
    support = sum(max(n, 1) for r, n in reads if _same_name(r, acct["name"])) / total
    return 2.0 * dsim - 0.25 * dist + 0.5 * support, dsim


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
        p.excluded = bool(p.user_choice)          # your own in/out choice wins
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
            if exclude_not_in_group and p.user_choice is None:
                p.excluded = True
    return players


# ------------------------------------------------------------------ main
TRUST_USERNAME = True  # trust an exact username read when the display line read badly
RATE_LIMIT_WAIT = 60   # seconds a check may spend waiting out Roblox's rate limit
BUSY_NOTE = ("Roblox's rate limit stopped the spelling check, so not every lookalike "
             "was tried (rescan in a minute, or allow a longer wait in Tuning)")
CONFIDENT = 0.80       # display-name similarity that settles it
LOOKALIKES = True      # try lookalike spellings when nothing fits
LOOKALIKE_LIMIT = 120  # spellings tried per reading in the first round
TWO_SLIP = True        # second round: two mistakes in one name
AVATAR_TIEBREAK = True # compare profile pictures between close candidates
CLEAR_LEAD = 0.30      # score lead over the runner-up


def verify(players, group_id: int | None = None, api=None, progress=None,
           exclude_not_in_group=True):
    """Fill in username/status/note on each Player (in place)."""
    todo = [p for p in players if not p.unread and p.user_votes]
    if not todo:
        return players
    api = api or RobloxAPI()
    tm = {"start": time.time()}
    looked = [0]
    if isinstance(api, RobloxAPI):
        api.wait_limit, api.waited, api.rate_limited = float(RATE_LIMIT_WAIT), 0.0, False

    def say(msg):
        if progress:
            progress(msg)

    # 1. look up every reading, plus the display name when it could itself be
    #    the username (people without a display name show their username).
    say("Looking up usernames…")
    first = set()
    for p in todo:
        first.update(r for r, _ in p.user_votes[:6])
        d = p.display_reading.replace(" ", "")
        if valid_username(d) and _ratio(d, p.reading) > 0.6:
            first.add(d)
    found = api.lookup(list(first))
    looked[0] += len(first)
    tm["lookup"] = time.time()

    def candidates(p, pool):
        names = {r.lower() for r, _ in p.user_votes[:6]}
        d = p.display_reading.replace(" ", "").lower()
        if d:
            names.add(d)
        names |= getattr(p, "_variants", set())
        reads = [r for r, _ in p.user_votes[:5]] or [p.reading]
        out = []
        for n in names:
            if n not in pool:
                continue
            # must be close to something actually read: a couple of characters
            # off at most (more for long names), never just the display name
            near = min(_lev(n, r) for r in reads)
            if near <= max(2, len(n) // 4):
                out.append(pool[n])
        return out

    def decide(p, pool, final=False):
        """Settle a player, unless the account chosen has a lookalike twin that
        OCR can't tell it apart from (see _indistinct): then only a profile
        picture can decide, so it stays open for the tiebreak and otherwise
        goes to Needs a look. Never guess between lookalikes."""
        ok = _choose(p, pool, final)
        if not ok or not p._scored:
            return ok
        if getattr(p, "_twins_unchecked", False):
            return False      # its lookalike twins never got checked (rate limit)
        top = p._scored[0][1]
        twins = [a for _, a in p._scored[1:] if _indistinct(p, top, a)]
        if twins:
            p._twin_tie = True
            tied = sorted([p._scored[0]] + [x for x in p._scored[1:] if x[1] in twins],
                          key=lambda x: x[0][0], reverse=True)       # best guess first
            p._scored = tied + [x for x in p._scored[1:] if x[1] not in twins]
            return False
        p._twin_tie = False
        return True

    def _choose(p, pool, final=False):
        """Pick the best account by the rules below. A strong display-name match settles it at once.
        An exact username reading with only a so-so display match is only
        accepted on the final pass, after lookalike spellings have had a
        chance to turn up a better-matching account (a misread like O for 0
        can be someone else's real username)."""
        scored = sorted(((score(p, a), a) for a in candidates(p, pool)),
                        key=lambda x: x[0][0], reverse=True)
        p._scored = scored
        if not scored:
            return False
        # Exactly one account whose display name clearly matches the row: that
        # one wins, even over an exact-but-wrong username reading.
        strong = [x for x in scored if x[0][1] >= CONFIDENT]
        if len(strong) == 1 and strong[0] is not scored[0]:
            scored.remove(strong[0])
            scored.insert(0, strong[0])
            p._scored = scored
        if len(strong) == 1:
            return True
        # Accounts that are the reading apart from blind-spot slips. When
        # there are several (JrSpiderMan1237 and JrSpiderMan_1237 both real),
        # the display line settles it if exactly one has a display name
        # matching the screen character for character, capitals and
        # underscores included (the display line often keeps the underscore
        # the username line lost).
        skel = {_skeleton(r) for r, _ in p.user_votes[:3]}
        twins = [x for x in scored if _skeleton(x[1]["name"]) in skel]
        seen = {v.replace(" ", "") for v, _ in p.display_votes[:3] if v}
        exact = [x for x in twins if x[1]["displayName"].replace(" ", "") in seen]
        if len(twins) >= 2 and len(exact) == 1:
            scored.remove(exact[0])
            scored.insert(0, exact[0])
            p._scored = scored
            return True
        # Final pass, once every lookalike has been looked up: exactly one
        # account that IS the reading apart from blind-spot slips (Z3FRO44
        # read for Z3FR0_44), with a display name that doesn't argue, beats
        # accounts that need a clearly read character thrown away. Two such
        # accounts (S07 and 507 both real) settle nothing here.
        def sdisp(a):        # display match with blind spots evened out too
            if _unreadable(a["displayName"]):
                return UNKNOWN_DISPLAY
            return max((_ratio(_skeleton(a["displayName"]), _skeleton(v))
                        for v, _ in p.display_votes[:3]), default=0.0)
        # With several twins (Bearg0brrr and Bearg0_brrr both real), the one
        # whose display name clearly matches best wins: a strong match, well
        # ahead of the other twins. Otherwise it stays a question.
        complete = not getattr(p, "_incomplete", False)
        if final and twins and complete:
            tw = sorted(twins, key=lambda x: sdisp(x[1]), reverse=True)
            best, runner = tw[0], (sdisp(tw[1][1]) if len(tw) > 1 else None)
            alone = runner is None
            if (best[0][1] >= (0.6 if alone else CONFIDENT)
                    and sdisp(best[1]) >= max(sdisp(a) for _, a in scored)
                    and (alone or sdisp(best[1]) - runner >= 0.15)):
                scored.remove(best)
                scored.insert(0, best)
                p._scored = scored
                return True
        # Final pass: the username line read an account exactly, nothing that
        # differs from it only by blind spots turned up, and the display line
        # just didn't read cleanly ("Ci)" for "Cjj"): trust the username line.
        ex = [x for x in scored if _same_name(p.reading, x[1]["name"])]
        if (final and complete and ex and len(twins) == 1 and twins[0] is ex[0]
                and _garbled(p, ex[0][1])):
            scored.remove(ex[0])
            scored.insert(0, ex[0])
            p._scored = scored
            return True
        (s1, d1), a1 = scored[0]
        lead = s1 - scored[1][0][0] if len(scored) > 1 else 9
        exact = _same_name(p.reading, a1["name"])
        if d1 >= CONFIDENT and lead >= CLEAR_LEAD:
            return True
        return final and exact and d1 >= 0.6 and lead >= CLEAR_LEAD

    pending = [p for p in todo if not decide(p, found)]

    # 1b. A pick whose display name is just its username got no independent
    #     check from the display line (same text, same blind spot), so ask
    #     Roblox about its lookalike twins (Columbina3 -> co1umbina3) before
    #     trusting it. A twin turning up makes it a question, not a guess.
    settled = [p for p in todo if p not in pending and p._scored]
    twin_q = {}
    for p in settled:
        a = p._scored[0][1]
        if _self_named(a) and not AUTO_NAME.match(a["name"].lower()):
            tw = [n for n in _blind_twins(a["name"]) if n not in found]
            p._variants = getattr(p, "_variants", set()) | set(tw)
            twin_q[p.index] = tw
    asked = sorted({n for tw in twin_q.values() for n in tw})
    if asked:
        say("Checking lookalike accounts…")
        looked[0] += len(asked)
        cut = False
        for i in range(0, len(asked), 100):
            if getattr(api, "rate_limited", False):
                cut = True
                break
            found.update(api.lookup(asked[i:i + 100]))
            cut |= bool(getattr(api, "failed", 0))
        for p in settled:
            if p.index in twin_q:
                if cut:
                    p._incomplete = p._twins_unchecked = True
                if not decide(p, found):
                    pending.append(p)

    # 2. lookalike spellings for anything unresolved: one slip first (a few
    #    dozen names each), two slips only if still needed (capped).
    rounds = [(False, LOOKALIKE_LIMIT)] + ([(True, int(LOOKALIKE_LIMIT * 1.25))]
                                           if TWO_SLIP else [])
    for rnd, (two, cap) in enumerate(rounds if LOOKALIKES else [], 1):
        if not pending:
            break
        # a tie between accounts already found (two real names differing
        # only by blind spots, one of them with the right display name) can't
        # be helped by more spellings: those go straight to the final pass and
        # the profile-picture tiebreak. If none of them has the display name
        # on screen, the real one is still out there (LOCATION_002 and
        # location002 found, L0CATI0N_002 is "lalaland"), so keep searching.
        search = [p for p in pending if not _settled_tie(p, found)]
        if two:      # the expensive round: not for a username read exactly
            search = [p for p in search          # whose display just read badly
                      if not (p.reading.lower() in found
                              and _same_name(p.reading, found[p.reading.lower()]["name"])
                              and _garbled(p, found[p.reading.lower()]))]
        ranked = []                   # each player's spellings, most likely first
        for p in search:
            vs = []
            for r, _ in p.user_votes[:3]:
                vs += variants(r, limit=cap, two=two)
            p._variants = getattr(p, "_variants", set()) | set(vs)
            ranked.append(vs)
        new, seen = [], set(found)    # interleave players so none is starved
        for k in range(max((len(v) for v in ranked), default=0)):
            for vs in ranked:
                if k < len(vs) and vs[k] not in seen:
                    seen.add(vs[k])
                    new.append(vs[k])
        looked[0] += len(new)
        label = "lookalike spellings" if rnd == 1 else "more lookalike spellings"
        chunks = [new[i:i + 100] for i in range(0, len(new), 100)]
        cut = False
        for b, chunk in enumerate(chunks, 1):
            if getattr(api, "rate_limited", False):
                log.info("lookalike search stopped by Roblox's rate limit (%d of %d batches done)",
                         b - 1, len(chunks))
                cut = True
                break
            say(f"Trying {label} for {len(search)} name(s)… {b}/{len(chunks)}")
            found.update(api.lookup(chunk))
            cut |= bool(getattr(api, "failed", 0))
        if cut:              # some spellings never got an answer: an account
            for p in search:  # we didn't hear about could be the right one
                p._incomplete = True
        pending = [p for p in pending if not decide(p, found)]

    pending = [p for p in pending if not decide(p, found, final=True)]
    tm["lookalikes"] = time.time()

    # 3. profile-picture tiebreak between close candidates (best effort)
    ties = [p for p in pending if AVATAR_TIEBREAK and len(p._scored) >= 2 and avatar_loaded(p.avatar)
            and p._scored[0][0][1] >= 0.6 and p._scored[1][0][1] >= 0.6]
    if ties:
        say("Comparing profile pictures…")
        try:
            uids = sorted({a["id"] for p in ties for _, a in p._scored[:4]})
            heads = {}
            for i in range(0, len(uids), 50):
                heads.update(api.headshots(uids[i:i + 50]))
            for p in ties:
                sims = sorted(((avatar_similarity(p.avatar, heads[a["id"]]), a)
                               for _, a in p._scored[:4] if a["id"] in heads),
                              key=lambda x: x[0], reverse=True)
                if len(sims) >= 2 and sims[0][0] - sims[1][0] > 0.15:
                    best = sims[0][1]
                    p._scored = [x for x in p._scored if x[1] is best] + \
                                [x for x in p._scored if x[1] is not best]
                    p._by_avatar = True
                    pending.remove(p)
        except Exception as e:
            log.warning("profile-picture tiebreak skipped: %s", e)

    tm["pictures"] = time.time()

    # 4. write results
    for p in todo:
        if p._scored:
            (s, d), a = p._scored[0]
            p.username, p.user_id = a["name"], a["id"]
            p.display_name = a["displayName"]
            p.alternates = [x[1]["name"] for x in p._scored[1:4]]
            if p in pending:
                p.status = "check"
                if getattr(p, "_twin_tie", False):
                    p.note = ("lookalike accounts (" + " / ".join(x[1]["name"] for x in p._scored[:2])
                              + ") that the screen can't tell apart")
                else:
                    p.note = ("display name differs"
                              if d < CONFIDENT else "several accounts fit")
                if getattr(p, "_incomplete", False):
                    p.note += "; " + BUSY_NOTE
            else:
                p.status = "verified"
                p.note = "matched by profile picture" if getattr(p, "_by_avatar", False) else ""
        else:
            p.status, p.username = "check", p.reading
            p.note = "no matching account"
            if getattr(p, "_incomplete", False):
                p.note += " found yet; " + BUSY_NOTE

    # 5. group membership
    for p in todo:
        p.base_status, p.base_note = p.status, p.note
    check_groups(players, group_id, api=api, progress=progress,
                 exclude_not_in_group=exclude_not_in_group)
    tm["groups"] = time.time()
    if isinstance(api, RobloxAPI):
        api.wait_limit = None
    log.info("verify: %d players in %.1fs (usernames %.1fs, lookalikes %.1fs, "
             "pictures %.1fs, groups %.1fs; %d names looked up)",
             len(todo), tm["groups"] - tm["start"], tm["lookup"] - tm["start"],
             tm["lookalikes"] - tm["lookup"], tm["pictures"] - tm["lookalikes"],
             tm["groups"] - tm["pictures"], looked[0])
    for p in todo:                       # a record for tuning, in nameocr.log
        log.info("verify #%d read=%s disp=%r -> %s [%s] %s | candidates: %s",
                 p.index, [r for r, _ in p.user_votes[:3]], p.display_reading,
                 p.username, p.status, p.note,
                 [(a["name"], a["displayName"], round(sc[0], 2), round(sc[1], 2))
                  for sc, a in getattr(p, "_scored", [])[:3]])
    for p in todo:
        for attr in ("_scored", "_variants", "_by_avatar", "_incomplete", "_twin_tie",
                     "_twins_unchecked"):
            if hasattr(p, attr):
                delattr(p, attr)
    return players
