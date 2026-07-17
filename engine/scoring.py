from shared.schemas import RiskBand, SignalResult

# Not all patterns are equal evidence. Weights are three-tiered, with three
# signals graded by their own data:
#
#   weight 3 (hard)   — near-sufficient alone: stolen photos (2), corroborated
#                       prior reports (6), payment identity with history (9).
#                       Signal 4 escalates to 3 on severe complaint volume.
#   weight 2 (strong) — very young selling account (1, <=30d), complaint-driven
#                       comment flags (4), young website domain (5, <60d).
#   weight 1 (soft)   — weak alone: tags (3), engagement (7), funnel (8);
#                       plus the weaker variants of 1/4/5.
#
# Thresholds below are the starting calibration -- tune against the operator's
# hand-labelled handles (spec 9.5), not by feel.
HARD_SIGNALS = {2, 9}  # 6 is graded below: full weight only for first-party evidence
SOFT_SIGNALS = {3, 7, 8}

HIGH_THRESHOLD = 5
CAUTION_THRESHOLD = 2
MIN_COMPUTABLE = 5


def signal_weight(s: SignalResult) -> int:
    if s.number in HARD_SIGNALS:
        return 3
    if s.number in SOFT_SIGNALS:
        return 1
    if s.number == 6:
        # Plan D4: ingested/public mentions alone cap at weight 2; evidenced
        # first-party buyer reports carry the full hard weight 3.
        return 3 if s.data.get("first_party") else 2
    if s.number == 1:
        age = s.data.get("age_days")
        return 2 if age is not None and age <= 30 else 1
    if s.number == 4:
        ratio = s.data.get("complaint_ratio") or 0.0
        tagged = s.data.get("tagged_complaints") or 0
        if ratio >= 0.25 or tagged >= 3:
            return 3
        if s.data.get("comments_disabled"):
            return 1
        return 2
    if s.number == 5:
        dom = s.data.get("domain_age_days")
        return 2 if dom is not None and dom < 60 else 1
    return 1


# --- 0-100 risk score -------------------------------------------------------
# The card headline. Anchored to the band (so score and band never contradict)
# and adjusted by community experience in both directions: complaints push it
# up, positive buyer reports pull it down. One bad review can never move the
# score more than a few points — levels already require multiple independent
# contributors to escalate.

_BAND_RANGE = {"low": (5, 25), "caution": (30, 60), "high": (65, 95)}
_LEVEL_PTS = {"isolated": 2, "recurring": 8, "severe": 15}
_CAT_MULT = {"fraud": 1.5, "as_described": 1.0, "quality": 1.0, "delivery": 0.6, "responsiveness": 0.4}


def _experience_adjustment(experience: dict | None) -> float:
    exp = experience or {}
    adj = 0.0
    for cat, level in (exp.get("categories") or {}).items():
        adj += _LEVEL_PTS.get(level, 0) * _CAT_MULT.get(cat, 1.0)
    positives = exp.get("positive_signals") or 0
    adj -= min(positives * 3, 15)
    return adj


def _experience_floor(experience: dict | None) -> int:
    """The score label must never contradict the complaint lines on the card:
    'looks trustworthy' next to 'repeated reports' is indefensible."""
    levels = ((experience or {}).get("categories") or {}).values()
    if "severe" in levels:
        return 45
    if "recurring" in levels:
        return 20
    return 0


def risk_score(weighted_score: int, risk_band: str, experience: dict | None = None) -> int | None:
    """0-100 scam-likelihood estimate; None = not enough data to say anything."""
    adj = _experience_adjustment(experience)
    floor = _experience_floor(experience)
    if risk_band == "insufficient":
        if not (experience or {}).get("has_data"):
            return None
        # community reviews only — never claim near-certainty either way
        return int(max(10, floor, min(70, 25 + adj)))
    lo, hi = _BAND_RANGE[risk_band]
    base = lo + (hi - lo) * min(weighted_score / 10.0, 1.0)
    return int(max(lo, floor, min(hi, base + adj)))


def score_label(score: int | None) -> tuple[str, str]:
    """(emoji, plain-language read) for the headline."""
    if score is None:
        return "⚪", "not enough data yet"
    if score < 20:
        return "🟢", "looks trustworthy so far"
    if score < 40:
        return "🟢", "minor concerns"
    if score < 65:
        return "🟡", "be careful"
    return "🔴", "serious red flags"


def compute_band(signals: list[SignalResult]) -> tuple[int, int, int, RiskBand]:
    """Returns (patterns_matched, patterns_total_computable, weighted_score, risk_band)."""
    computable = [s for s in signals if s.computable]
    matched = [s for s in computable if s.matched]
    patterns_matched = len(matched)
    patterns_total = len(computable)
    weighted_score = sum(signal_weight(s) for s in matched)

    if patterns_total < MIN_COMPUTABLE:
        return patterns_matched, patterns_total, weighted_score, "insufficient"
    if weighted_score >= HIGH_THRESHOLD:
        return patterns_matched, patterns_total, weighted_score, "high"
    if weighted_score >= CAUTION_THRESHOLD:
        return patterns_matched, patterns_total, weighted_score, "caution"
    return patterns_matched, patterns_total, weighted_score, "low"
