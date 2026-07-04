"""Workstream E2: public-page publication gate (legal-exposure control).

A seller page is publicly indexable ONLY when the band is corroborated:
  - an evidenced first-party report, OR
  - >=3 computed signals matched including at least one hard (weight-3) signal.
Thin/algorithm-only "High" verdicts stay bot-DM-only (private), never indexed
under a real business's name.
"""

from shared.models import RiskSnapshot

from engine.scoring import HARD_SIGNALS, signal_weight
from shared.schemas import SignalResult

# reconstruct SignalResult weights from stored snapshot dicts
def _matched_signals(snapshot: RiskSnapshot) -> list[dict]:
    return [s for s in (snapshot.signals or {}).get("signals", []) if s.get("status") == "matched"]


def _has_hard_signal(matched: list[dict]) -> bool:
    for s in matched:
        num = s.get("number")
        if num in HARD_SIGNALS:
            return True
        if num == 6 and (s.get("data") or {}).get("first_party"):
            return True
        if num == 2:
            return True
    return False


def is_publishable(snapshot: RiskSnapshot, has_evidenced_report: bool) -> bool:
    if snapshot is None:
        return False
    # low/insufficient bands are never an accusation -> always safe to publish
    if snapshot.risk_band in ("low", "insufficient"):
        return True
    if has_evidenced_report:
        return True
    matched = _matched_signals(snapshot)
    return len(matched) >= 3 and _has_hard_signal(matched)


def publication_reason(snapshot: RiskSnapshot, has_evidenced_report: bool) -> str:
    if is_publishable(snapshot, has_evidenced_report):
        return "corroborated"
    return "thin_algorithmic"  # bot-DM only, not indexed
