"""Applies the Settings > Tuning values to the scanner, reader and verifier."""
import rows as R
import scanner as S
import verify as V


def apply(cfg: dict):
    # finding rows
    R.CARD_TOL = int(cfg["row_colour_tolerance"])
    # junk filtering
    S.FILTER_POSITION = bool(cfg["filter_position"])
    S.MIN_SEEN = max(1, int(cfg["min_frames"]))
    # verifying
    V.CONFIDENT = float(cfg["display_match"]) / 100.0
    V.AVATAR_TIEBREAK = bool(cfg["avatar_tiebreak"])
    V.SEARCH_DEPTH = int(cfg["search_depth"])
    # no longer settings, fixed at their best values: every OCR setting, how
    # alike a row must look to be the same player (90%), how far the list may
    # seem to move (5 px), lookalike spellings on, garbled display reads
    # don't count against an account, and Roblox's rate limit is waited out
    # for as long as it takes (with progress shown, and Stop checking)
    R.USER_READS = R.ALL_USER_READS
    R.TEXT_SAME = 0.90
    S.GEOM_TOL = (5, 5, 3)
    V.LOOKALIKES, V.LOOKALIKE_LIMIT, V.TWO_SLIP = True, 120, True
    V.TRUST_USERNAME = True
    V.RATE_LIMIT_WAIT = None
