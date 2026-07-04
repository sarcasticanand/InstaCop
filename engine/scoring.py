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
