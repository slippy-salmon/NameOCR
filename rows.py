"""Resolution-independent row detection for the Roblox Esc menu > People list.

Each player row is an opaque card of a flat colour (~(32,33,40)) whatever
the menu transparency is set to; the gaps between rows can be anything.
Nothing is measured in pixels; everything is relative to the detected
row height, so it works at any screenshot size.
"""
from dataclasses import dataclass
import numpy as np
from PIL import Image, ImageOps
import pytesseract

CARD_RGB = np.array([32, 34, 39])   # Roblox's row colour (exact in real captures)
CARD_SEARCH = 12       # how far the real colour may sit from CARD_RGB
CARD_TOL = 3           # tolerance around the colour measured in each frame;
                       # tight, because the gaps between rows can be a grey
                       # only ~5 away when the game behind is grey


def card_colour(a):
    """The exact row colour in this frame: the most common colour near
    CARD_RGB. Returns None if the menu isn't on screen."""
    s = a[::3, ::3].reshape(-1, 3)
    near = s[np.abs(s - CARD_RGB).max(axis=1) <= CARD_SEARCH].astype(np.int32)
    if len(near) < 0.02 * len(s):
        return None
    keys = (near[:, 0] << 16) | (near[:, 1] << 8) | near[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    k = int(vals[np.argmax(counts)])
    return np.array([k >> 16, (k >> 8) & 255, k & 255], dtype=np.int16)
USER_WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"


@dataclass
class Row:
    top: int
    bottom: int
    left: int
    right: int
    complete: bool          # False if cut off by the list viewport
    display_box: tuple = None
    user_box: tuple = None
    full_h: float = 0.0     # typical row height in this frame
    hovered: bool = False   # unused: Roblox doesn't highlight the row under the mouse
    text_x: int = 0         # where the text column starts

    @property
    def height(self):
        return self.bottom - self.top


def _runs(mask_1d, min_len=1):
    """(start, end) index pairs of consecutive True runs."""
    padded = np.concatenate([[False], mask_1d, [False]])
    d = np.diff(padded.astype(int))
    starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
    return [(s, e) for s, e in zip(starts, ends) if e - s >= min_len]


def detect_rows(img: Image.Image):
    """Rows in the frame, top to bottom. Real captures render the row colour
    exactly, so it's matched tightly (the gaps between rows can be a grey
    only ~5 away). Blurry/compressed images get a looser match."""
    a = np.asarray(img.convert("RGB"), dtype=np.int16)
    cc = card_colour(a)
    if cc is None:
        return []
    s = a[::3, ::3].reshape(-1, 3)
    d = np.abs(s - cc).max(axis=1)
    near = (d <= 7).sum()
    exact = (d <= 1).sum()
    tol = CARD_TOL if near and exact / near > 0.7 else 7
    return _detect(a, tol, cc)


def _detect(a, tol, cc):
    H, W, _ = a.shape
    card = np.abs(a - cc).max(axis=2) <= tol

    # Columns that are mostly card colour = the empty middle of the rows.
    col_frac = card.mean(axis=0)
    list_cols = col_frac > 0.25 * col_frac.max()
    col_runs = _runs(list_cols)
    if not col_runs:
        return []
    x0, x1 = max(col_runs, key=lambda r: r[1] - r[0])   # widest band

    # Rows: horizontal bands where those columns are card colour.
    # Gaps between rows have ~0% card colour; a text line inside a row still
    # has plenty, so a low threshold gives the full row extent. Each band must
    # also be solidly card-coloured somewhere.
    row_frac = card[:, x0:x1].mean(axis=1)
    seeds = _runs(row_frac > 0.6, min_len=max(3, H // 200))
    # Grow each seed while still clearly above the darkest point of the gaps
    # between rows (that level depends on the menu transparency).
    gap_mins = [row_frac[e:s2].min() for (s, e), (s2, _) in zip(seeds, seeds[1:])
                if 0 < s2 - e < 0.5 * max(1, np.median([b - a for a, b in seeds]))]
    grow_t = min(0.55, (np.median(gap_mins) if gap_mins else 0.0) + 0.15)
    bands = []
    for s, e in seeds:
        while s > 0 and row_frac[s - 1] > grow_t:
            s -= 1
        while e < H and row_frac[e] > grow_t:
            e += 1
        if bands and s <= bands[-1][1]:            # grew into the previous one
            continue
        bands.append((s, e))
    if len(bands) < 2:
        return []

    # Player rows share one height; the Invite/Mute bar and tab strip don't.
    heights = np.array([e - s for s, e in bands])
    full_h = np.median(heights[heights >= np.percentile(heights, 50)])

    bands = _split_merged(bands, full_h)
    rows = []
    for s, e in bands:
        h = e - s
        if h < 0.35 * full_h or h > 1.5 * full_h:
            continue                      # too small/large to be a player row
        rows.append(Row(s, e, 0, 0, abs(h - full_h) <= 0.12 * full_h))
    if not rows:
        return []
    # Card edges, shared by every row: walk out from the empty middle while
    # columns are still partly card colour (the avatar and buttons leave
    # ~20%+), then take the outermost column that is mostly card colour.
    sel = np.zeros(H, bool)
    for r in rows:
        sel[r.top:r.bottom] = True
    colf = card[sel].mean(axis=0)
    lo = x0
    while lo > 0 and colf[lo - 1] > 0.1:
        lo -= 1
    hi = x1
    while hi < W and colf[hi] > 0.1:
        hi += 1
    # The real edge is a sharp step up to ~95% card colour; background that
    # happens to match the card only reaches ~40-75%, so don't stop there.
    solid = np.where(colf[lo:hi] > 0.85)[0]
    if not len(solid):
        solid = np.where(colf[lo:hi] > 0.6)[0]
    L, R = (lo + solid[0], lo + solid[-1] + 1) if len(solid) else (x0, x1)
    for r in rows:
        r.left, r.right = L, R

    # Drop leading bands that aren't player rows (e.g. Invite/Mute bar):
    # a player row has an avatar (non-card pixels) near its left edge.
    out = []
    for r in rows:
        av = card[r.top:r.bottom, r.left:r.left + int(1.1 * full_h)]
        if av.mean() < 0.75:              # avatar present
            out.append(r)
        elif out:                          # blank-left band below rows: stop
            break
    if not out:
        return []
    # The text column starts at the same x in every row; use the median.
    starts = [x for x in (_text_start(card, r, full_h) for r in out) if x]
    tx_off = int(np.median(starts)) if starts else int(1.2 * full_h)
    for r in out:
        _locate_text(a, card, r, full_h, tx_off)
    allrows = sorted(out, key=lambda r: r.top)
    # A row with rows right above and below can't be clipped by the list's
    # edges. Otherwise it must be the full height (to ~1px): a few clipped
    # pixels shift the text and spoil both OCR and tracking.
    tol = max(1.0, 0.025 * full_h)
    for i, r in enumerate(allrows):
        above = i > 0 and r.top - allrows[i - 1].bottom < 0.5 * full_h
        below = i + 1 < len(allrows) and allrows[i + 1].top - r.bottom < 0.5 * full_h
        r.complete = bool((above and below and r.height > 0.8 * full_h)
                          or abs(r.height - full_h) <= tol)
    return allrows


def _split_merged(bands, full_h):
    """When the game behind the menu is the same grey as the rows, the gap
    between two rows vanishes and they merge into one tall band. Rows are
    evenly spaced, so cut tall bands using the spacing of the normal ones."""
    normal = [(s, e) for s, e in bands if abs((e - s) - full_h) <= 0.12 * full_h]
    odd = [(s, e) for s, e in bands if 1.03 * full_h < (e - s)]
    if not odd or len(normal) < 2:
        return bands
    pitch = row_pitch([s for s, _ in normal], full_h)
    if not (1.02 * full_h < pitch < 1.6 * full_h):
        return bands
    anchor = normal[0][0]
    out = []
    for s, e in bands:
        if (e - s) <= 1.5 * full_h:
            # a row that swallowed a bit of a background-coloured gap: snap
            # it back onto the even spacing at its true height
            if (e - s) > 1.03 * full_h:
                k = round((s - anchor) / pitch)
                t = anchor + k * pitch
                if s - 2 <= t and t + full_h <= e + 2:
                    s, e = int(round(t)), int(round(t + full_h))
            out.append((s, e))
            continue
        k = int(np.ceil((s - 2 - anchor) / pitch))
        t = anchor + k * pitch
        while t + full_h <= e + 2:
            out.append((int(round(t)), int(round(t + full_h))))
            t += pitch
    return sorted(out)


def row_pitch(tops, full_h):
    """Spacing between consecutive rows: median of the one-row steps
    (steps over a missing row are ~2x and get ignored)."""
    d = [b - t for t, b in zip(tops, tops[1:])]
    one = [x for x in d if 0.9 * full_h < x < 1.6 * full_h]
    if one:
        return float(np.median(one))
    two = [x / 2 for x in d if 1.8 * full_h < x < 3.2 * full_h]
    return float(np.median(two)) if two else 1.15 * full_h


def _text_start(card, r: Row, full_h):
    """First all-card column after the avatar (offset from the card's left)."""
    frac = card[r.top + 2:r.bottom - 2, r.left:r.right].mean(axis=0)
    for x in range(int(0.75 * full_h), int(1.4 * full_h)):
        if x < len(frac) and frac[x] > 0.97:
            return x
    return None


def _locate_text(a, card, r: Row, full_h, tx_off=None):
    """Find the display-name and @username lines inside a row."""
    if tx_off is None:
        tx_off = _text_start(card, r, full_h) or int(1.2 * full_h)
    tx0 = r.left + int(tx_off)
    tx1 = r.right
    r.full_h, r.text_x = full_h, tx0
    region = a[r.top:r.bottom, tx0:tx1]
    bright = region.max(axis=2) > 110                 # text pixels
    # End the text region at the first wide empty gap (before the buttons).
    ink_cols = bright.any(axis=0)
    gap_min = int(0.6 * full_h)
    last_ink, run = 0, 0
    for x, ink in enumerate(ink_cols):
        if ink:
            last_ink, run = x, 0
        elif last_ink:
            run += 1
            if run >= gap_min:
                break
    bright = bright[:, :last_ink + 1]
    lines = _runs(bright.sum(axis=1) > 0, min_len=2)
    if len(lines) < 2:
        return
    # Merge fragments, then take the two tallest/most-inked lines in order.
    lines = sorted(lines, key=lambda l: bright[l[0]:l[1]].sum(), reverse=True)[:2]
    lines.sort()
    boxes = []
    for i, (ys, ye) in enumerate(lines):
        cols = np.where(bright[ys:ye].any(axis=0))[0]
        if len(cols) == 0:
            return                          # clipped row: no usable text
        px, py = max(2, int(0.08 * full_h)), max(1, int(0.04 * full_h))
        boxes.append((tx0 + cols[0] - px, r.top + ys - py,
                      tx0 + cols[-1] + px + 1, r.top + ye + py))
    r.display_box, r.user_box = boxes


def prep(crop: Image.Image, target_h=96, threshold=150):
    """Upscale so the crop is ~target_h px tall, invert, optionally binarize.

    Defaults match the settings that read the full-res 2560x1440 captures.
    threshold=None leaves the greyscale for Tesseract to binarize itself,
    which did better on tiny (downscaled) text. Re-check with sweep.py.
    """
    g = ImageOps.grayscale(crop)
    scale = max(1, target_h / g.height)
    g = g.resize((int(g.width * scale), int(g.height * scale)), Image.LANCZOS)
    g = ImageOps.invert(g)
    if threshold is not None:
        g = g.point(lambda v: 255 if v > threshold else 0)
    return ImageOps.expand(g, border=20, fill=255)


# Sizes/thresholds to read each username at; each one is a vote. Picked on
# 20 real 1440p names: the readings disagree on hard names (e.g. a long one
# ending "o85"), and with five the right reading is usually among them.
ALL_USER_READS = ((80, 150), (128, 160), (64, None), (96, None), (80, 160))  # best first
USER_READS = ALL_USER_READS


def ocr_username_crop(crop: Image.Image, target_h=96, threshold=None):
    """OCR a username line including its leading '@'."""
    txt = pytesseract.image_to_string(
        prep(crop, target_h, threshold),
        config=f"--psm 7 -c tessedit_char_whitelist=@{USER_WHITELIST}").strip()
    txt = txt[1:] if txt.startswith("@") else txt
    return "".join(ch for ch in txt if ch in USER_WHITELIST) or None


def ocr_display_crop(crop: Image.Image):
    """OCR a display-name line; drops a trailing verified-badge glyph."""
    txt = pytesseract.image_to_string(prep(crop), config="--psm 7").strip()
    parts = txt.split()
    while parts and len(parts) > 1 and not any(c.isalnum() for c in parts[-1]):
        parts.pop()                       # badge read as '©', '@', etc.
    if len(parts) > 1 and len(parts[-1]) == 1 and not parts[-1].isalnum():
        parts.pop()
    return " ".join(parts) or None


def ocr_username(img, r: Row):
    return ocr_username_crop(img.crop(r.user_box)) if r.user_box else None


def ocr_display(img, r: Row):
    return ocr_display_crop(img.crop(r.display_box)) if r.display_box else None


def avatar_box(r: Row):
    """Square around the avatar circle."""
    pad = int(round(0.1 * r.full_h))
    size = int(round(0.8 * r.full_h))    # fixed square: same shape every frame
    return (r.left + pad, r.top + pad, r.left + pad + size, r.top + pad + size)


def signature(img: Image.Image, r: Row):
    """Fingerprint of a row used to recognise the same player across frames:
    (avatar in colour, both text lines, avatar-has-loaded). Roblox shows a
    plain grey circle until a profile picture loads, so an unloaded avatar
    is ignored when comparing. Compare with similarity()."""
    h = r.full_h
    avimg = img.crop(avatar_box(r)).resize((16, 16), Image.BILINEAR)
    av = np.asarray(avimg, float).ravel()
    loaded = float(np.asarray(avimg.convert("L"), float).std()) > AVATAR_LOADED_STD
    # start at the text's own left edge (content-anchored), not the card
    # edge, so a pixel of edge jitter between frames doesn't matter
    x = min(b[0] for b in (r.display_box, r.user_box) if b) \
        if (r.display_box or r.user_box) else r.text_x
    tx = np.asarray(img.crop((x, r.top + 2, int(x + 6 * h),
                              r.bottom - 2)).convert("L")
                    .resize((240, 24), Image.BILINEAR), float).ravel()
    return (_unit(av - av.mean()), _unit(tx - tx.mean()), loaded)


AVATAR_LOADED_STD = 12.0


TEXT_SAME = 0.90     # real captures: same player >= 0.98, different <= 0.81


def similarity(a, b) -> float:
    """0..1 score; >= 0.75 means same player. The text must match closely,
    and the avatar too when both have loaded."""
    t = float(a[1] @ b[1])
    score = 0.75 + (t - TEXT_SAME) * 2.5          # TEXT_SAME -> 0.75, 1.0 -> 1.0
    if a[2] and b[2]:
        score = min(score, float(a[0] @ b[0]))
    return score


def blend(a, b, w=0.3):
    """Running average of two signatures (a loaded avatar replaces a blank)."""
    if b[2] and not a[2]:
        av, loaded = b[0], True
    elif a[2] and not b[2]:
        av, loaded = a[0], True
    else:
        av, loaded = _unit((1 - w) * a[0] + w * b[0]), a[2]
    return (av, _unit((1 - w) * a[1] + w * b[1]), loaded)


def _unit(v):
    n = np.linalg.norm(v)
    return v / n if n else v


def sharpness(crop: Image.Image):
    """Variance of a Laplacian: higher = crisper text."""
    g = np.asarray(crop.convert("L"), float)
    if g.shape[0] < 3 or g.shape[1] < 3:
        return 0.0
    lap = (g[1:-1, :-2] + g[1:-1, 2:] + g[:-2, 1:-1] + g[2:, 1:-1]
           - 4 * g[1:-1, 1:-1])
    return float(lap.var())


if __name__ == "__main__":
    import sys
    for path in sys.argv[1:]:
        img = Image.open(path).convert("RGB")
        rows = detect_rows(img)
        print(f"{path}  {img.size}  rows={len(rows)}")
        for r in rows:
            tag = "ok " if r.complete else "CUT"
            u = ocr_username(img, r) if r.complete else ""
            d = ocr_display(img, r) if r.complete else ""
            print(f"  {tag} y={r.top}-{r.bottom} h={r.height}  {d!r:24} @{u}")
