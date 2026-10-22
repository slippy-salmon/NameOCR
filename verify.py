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

    def __init__(self, timeout=10):
        import requests
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "NameOCR/1.0"
        self.timeout = timeout

    def _req(self, method, url, **kw):
        delay = 1.0
        for attempt in range(6):
            r = self.s.request(method, url, timeout=self.timeout, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("Retry-After", delay))
                time.sleep(min(wait, 10))
                delay *= 2
                continue
            return r
        r.raise_for_status()
        return r

    def lookup(self, names: list[str], progress=None) -> dict[str, dict]:
        """lowercased username -> {id, name, displayName}. A failed batch is
        logged and skipped rather than stopping everything."""
        out, names = {}, sorted({n.lower() for n in names if valid_username(n)})
        batches = [names[i:i + 100] for i in range(0, len(names), 100)]
        for b, chunk in enumerate(batches, 1):
            if progress and len(batches) > 1:
                progress(b, len(batches))
            try:
                r = self._req("POST", USERS_URL,
                              json={"usernames": chunk, "excludeBannedUsers": True})
                r.raise_for_status()
                for d in r.json().get("data", []):
                    out[d["name"].lower()] = {"id": d["id"], "name": d["name"],
                                              "displayName": d.get("displayName", "")}
            except Exception as e:
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
                except Exception as e:
                    log.warning("lookup by id for %s failed: %s", n, e)
        return out

    def groups(self, uid: int) -> set[int]:
        for url in (GROUPS_V2, GROUPS_V1):
            r = self._req("GET", url.format(uid=uid))
            if r.status_code == 404:
                continue
            r.raise_for_status()
            return {d["group"]["id"] for d in r.json().get("data", [])}
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


def _ratio(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _lev(a: str, b: str) -> int:
    a, b = a.lower(), b.lower()
    d = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        p, d[0] = d[0], i
        for j, cb in enumerate(b, 1):
            p, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, p + (ca != cb))
    return d[-1]


def score(p, acct) -> tuple[float, float]:
    """(overall score, display-name similarity) of an account for a player."""
    disp = [v for v, _ in p.display_votes[:3]]
    dsim = max((_ratio(acct["displayName"], d) for d in disp), default=0.5)
    reads = p.user_votes[:5]
    total = sum(max(n, 1) for _, n in reads) or 1
    dist = min((_lev(acct["name"], r) for r, _ in reads), default=5)
    support = sum(max(n, 1) for r, n in reads
                  if r.lower() == acct["name"].lower()) / total
    return 2.0 * dsim - 0.25 * dist + 0.5 * support, dsim


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
        """Settle a player. A strong display-name match settles it at once.
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
        (s1, d1), a1 = scored[0]
        lead = s1 - scored[1][0][0] if len(scored) > 1 else 9
        exact = a1["name"].lower() == p.reading.lower()
        if d1 >= CONFIDENT and lead >= CLEAR_LEAD:
            return True
        return final and exact and d1 >= 0.6 and lead >= CLEAR_LEAD

    pending = [p for p in todo if not decide(p, found)]

    # 2. lookalike spellings for anything unresolved: one slip first (a few
    #    dozen names each), two slips only if still needed (capped).
    rounds = [(False, LOOKALIKE_LIMIT)] + ([(True, int(LOOKALIKE_LIMIT * 1.25))]
                                           if TWO_SLIP else [])
    for rnd, (two, cap) in enumerate(rounds if LOOKALIKES else [], 1):
        if not pending:
            break
        extra = set()
        for p in pending:
            vs = set()
            for r, _ in p.user_votes[:3]:
                vs.update(variants(r, limit=cap, two=two))
            p._variants = getattr(p, "_variants", set()) | vs
            extra |= vs
        new = [n for n in extra if n not in found]
        label = "lookalike spellings" if rnd == 1 else "more lookalike spellings"
        say(f"Trying {label} for {len(pending)} name(s)…")
        found.update(api.lookup(new, progress=lambda b, n: say(
            f"Trying {label} for {len(pending)} name(s)… {b}/{n}")))
        pending = [p for p in pending if not decide(p, found)]

    pending = [p for p in pending if not decide(p, found, final=True)]

    # 3. profile-picture tiebreak between close candidates (best effort)
    ties = [p for p in pending if AVATAR_TIEBREAK and len(p._scored) >= 2 and p.avatar is not None
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

    # 4. write results
    for p in todo:
        if p._scored:
            (s, d), a = p._scored[0]
            p.username, p.user_id = a["name"], a["id"]
            p.display_name = a["displayName"]
            p.alternates = [x[1]["name"] for x in p._scored[1:4]]
            if p in pending:
                p.status = "check"
                p.note = ("display name differs"
                          if d < CONFIDENT else "several accounts fit")
            else:
                p.status = "verified"
                p.note = "matched by profile picture" if getattr(p, "_by_avatar", False) else ""
        else:
            p.status, p.username = "check", p.reading
            p.note = "no matching account"

    # 5. group membership
    for p in todo:
        p.base_status, p.base_note = p.status, p.note
    check_groups(players, group_id, api=api, progress=progress,
                 exclude_not_in_group=exclude_not_in_group)
    for p in todo:                       # a record for tuning, in nameocr.log
        log.info("verify #%d read=%s disp=%r -> %s [%s] %s | candidates: %s",
                 p.index, [r for r, _ in p.user_votes[:3]], p.display_reading,
                 p.username, p.status, p.note,
                 [(a["name"], a["displayName"], round(sc[0], 2), round(sc[1], 2))
                  for sc, a in getattr(p, "_scored", [])[:3]])
    for p in todo:
        for attr in ("_scored", "_variants", "_by_avatar"):
            if hasattr(p, attr):
                delattr(p, attr)
    return players
