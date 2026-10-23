"""Applies the Settings > Tuning values to the scanner, reader and verifier."""
import rows as R
import scanner as S
import verify as V


def apply(cfg: dict):
    # finding rows
    R.CARD_TOL = int(cfg["row_colour_tolerance"])
    # following rows between frames
    R.TEXT_SAME = float(cfg["tracking_strictness"]) / 100.0
    # junk filtering
    S.FILTER_POSITION = bool(cfg["filter_position"])
    t = int(cfg["position_tolerance"])
    S.GEOM_TOL = (t, t, max(3, round(t * 0.6)))
    S.MIN_SEEN = max(1, int(cfg["min_frames"]))
    # reading: always every OCR setting (no longer a setting)
    R.USER_READS = R.ALL_USER_READS
    # verifying
    V.CONFIDENT = float(cfg["display_match"]) / 100.0
    V.LOOKALIKES = bool(cfg["lookalikes"])
    # spellings per reading and the two-slip round are fixed (no longer settings)
    V.LOOKALIKE_LIMIT, V.TWO_SLIP = 120, True
    V.AVATAR_TIEBREAK = bool(cfg["avatar_tiebreak"])
    V.TRUST_USERNAME = bool(cfg["trust_username"])
    V.RATE_LIMIT_WAIT = int(cfg["rate_limit_wait"])
