"""Cache policy for risk snapshots (spec Phase 4):
- < 7 days old   -> fresh, serve as-is
- 7-30 days old  -> serve with a staleness banner AND trigger a background re-check
- > 30 days old  -> treat as cold (full re-check before serving)
- a new accepted report at any time -> force re-check
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from shared.models import RiskSnapshot, Seller

FRESH_DAYS = 7
STALE_DAYS = 30


def latest_snapshot(db: Session, seller_id: int) -> RiskSnapshot | None:
    return (
        db.query(RiskSnapshot)
        .filter(RiskSnapshot.seller_id == seller_id)
        .order_by(RiskSnapshot.computed_at.desc())
        .first()
    )


def cache_state(snapshot: RiskSnapshot | None) -> str:
    """Returns 'fresh' | 'stale' | 'cold'."""
    if snapshot is None:
        return "cold"
    computed = snapshot.computed_at
    if computed.tzinfo is None:
        computed = computed.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - computed
    if age <= timedelta(days=FRESH_DAYS):
        return "fresh"
    if age <= timedelta(days=STALE_DAYS):
        return "stale"
    return "cold"


def lookup(db: Session, handle: str) -> tuple[Seller | None, RiskSnapshot | None, str]:
    """Returns (seller, latest_snapshot, cache_state)."""
    seller = db.query(Seller).filter(Seller.ig_handle == handle.lower().lstrip("@")).one_or_none()
    if seller is None:
        return None, None, "cold"
    snapshot = latest_snapshot(db, seller.id)
    return seller, snapshot, cache_state(snapshot)
