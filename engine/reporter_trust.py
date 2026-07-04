"""Workstream D1/D2: reporter trust from Telegram-native identity. No OTP —
the Telegram account plus its history with our bot IS the identity.

Trust weight 0..1 =
  base from tenure (first-seen-by-our-bot; we can't read TG creation date)
  + corroboration history bonus
  + evidence bonus (payment screenshot)
  x burst penalty (many reports in a short window)
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from shared.models import Check, Report

logger = logging.getLogger(__name__)

REPORTS_PER_DAY = 3
REPORTS_PER_WEEK = 10
NEW_ACCOUNT_HOURS = 48          # "new to us" threshold
SMEAR_BURST_REPORTERS = 3       # distinct new reporters on one seller in 24h -> quarantine


def _first_seen(db: Session, chat_id: str) -> datetime | None:
    candidates = [
        db.query(func.min(Check.requested_at)).filter(Check.requested_by_chat_id == chat_id).scalar(),
        db.query(func.min(Report.created_at)).filter(Report.reporter_chat_id == chat_id).scalar(),
    ]
    known = [c for c in candidates if c is not None]
    return min(known) if known else None


def _aware(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def reporter_trust(db: Session, chat_id: str, has_evidence: bool) -> float:
    now = datetime.now(timezone.utc)
    first = _first_seen(db, chat_id)
    days_known = (now - _aware(first)).days if first else 0

    if days_known < 1:
        base = 0.1
    elif days_known < 7:
        base = 0.3
    elif days_known < 30:
        base = 0.55
    else:
        base = 0.75

    corroborated = (
        db.query(func.count(Report.id))
        .filter(Report.reporter_chat_id == chat_id, Report.status == "accepted")
        .scalar()
    )
    base += 0.1 * min(corroborated, 2)

    if has_evidence:
        base += 0.3

    # behavioral: this reporter hammering many sellers today?
    recent = (
        db.query(func.count(Report.id))
        .filter(Report.reporter_chat_id == chat_id, Report.created_at >= now - timedelta(days=1))
        .scalar()
    )
    if recent >= REPORTS_PER_DAY:
        base *= 0.2

    # ever quarantined -> lasting discount
    quarantined = (
        db.query(func.count(Report.id))
        .filter(Report.reporter_chat_id == chat_id, Report.status == "quarantined")
        .scalar()
    )
    if quarantined:
        base *= 0.5

    return round(max(0.0, min(1.0, base)), 3)


def report_rate_ok(db: Session, chat_id: str) -> bool:
    now = datetime.now(timezone.utc)
    day = (
        db.query(func.count(Report.id))
        .filter(Report.reporter_chat_id == chat_id, Report.created_at >= now - timedelta(days=1))
        .scalar()
    )
    if day >= REPORTS_PER_DAY:
        return False
    week = (
        db.query(func.count(Report.id))
        .filter(Report.reporter_chat_id == chat_id, Report.created_at >= now - timedelta(days=7))
        .scalar()
    )
    return week < REPORTS_PER_WEEK
