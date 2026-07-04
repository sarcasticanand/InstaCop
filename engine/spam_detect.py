"""Workstream D3: coordinated-attack detection, both directions.

Competitor smear: burst of fraud reports on one seller from low-tenure
accounts, little/no evidence -> quarantine (weight 0), operator review; the
public page does NOT flip.

Self-praise ring: burst of positive signals from low-tenure accounts ->
their weight ~0, seller flagged internally ("possible review manipulation").
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from shared.models import Check, Followup, Report

from engine.reporter_trust import NEW_ACCOUNT_HOURS, SMEAR_BURST_REPORTERS, _aware, _first_seen

logger = logging.getLogger(__name__)


def _is_new_reporter(db: Session, chat_id: str) -> bool:
    first = _first_seen(db, chat_id)
    if first is None:
        return True
    return datetime.now(timezone.utc) - _aware(first) < timedelta(hours=NEW_ACCOUNT_HOURS)


def check_smear_burst(db: Session, seller_id: int) -> list[Report]:
    """If >= SMEAR_BURST_REPORTERS distinct new-account reporters hit one seller
    within 24h, quarantine that whole burst. Returns quarantined reports."""
    since = datetime.now(timezone.utc) - timedelta(days=1)
    recent = (
        db.query(Report)
        .filter(
            Report.seller_id == seller_id,
            Report.created_at >= since,
            Report.reporter_chat_id.isnot(None),
            Report.status.in_(["unreviewed", "accepted"]),
        )
        .all()
    )
    new_account_reports = [r for r in recent if _is_new_reporter(db, r.reporter_chat_id)]
    distinct_new = {r.reporter_chat_id for r in new_account_reports}
    if len(distinct_new) < SMEAR_BURST_REPORTERS:
        return []

    for r in new_account_reports:
        r.status = "quarantined"
        r.spam_score = 1.0
    db.commit()
    logger.warning(
        "smear-burst quarantine: seller %s, %d reports from %d new accounts",
        seller_id, len(new_account_reports), len(distinct_new),
    )
    return new_account_reports


def check_praise_ring(db: Session, seller_id: int) -> int:
    """Positive follow-ups from many new accounts in 48h -> log internal flag,
    return count of suspect answers (their weight is handled by the caller
    via spam-scored aggregation; follow-ups from flagged rings are excluded)."""
    since = datetime.now(timezone.utc) - timedelta(hours=48)
    rows = (
        db.query(Followup, Check.requested_by_chat_id)
        .join(Check, Check.id == Followup.check_id)
        .filter(
            Check.seller_id == seller_id,
            Followup.response == "bought_good",
            Followup.responded_at >= since,
        )
        .all()
    )
    suspect = [fu for fu, chat in rows if chat and _is_new_reporter(db, chat)]
    if len({chat for fu, chat in rows if chat and _is_new_reporter(db, chat)}) >= SMEAR_BURST_REPORTERS:
        logger.warning("possible review manipulation: seller %s (%d new-account praise answers)", seller_id, len(suspect))
        return len(suspect)
    return 0


def spam_score_for_report(db: Session, report: Report, trust: float) -> float:
    """0 = trusted, 1 = likely spam. Inverse-ish of trust, sharpened by burst."""
    score = max(0.0, 1.0 - trust)
    if report.status == "quarantined":
        return 1.0
    return round(min(1.0, score), 3)
