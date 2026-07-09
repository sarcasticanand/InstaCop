"""Bot-side domain logic kept separate from aiogram handlers so it can be
unit-tested without Telegram."""

import re
from dataclasses import dataclass

import redis
from rq import Queue

from shared.cache import lookup
from shared.config import settings
from shared.db import SessionLocal

HANDLE_RE = re.compile(r"^[a-zA-Z0-9._]{1,30}$")
URL_RE = re.compile(r"instagram\.com/([a-zA-Z0-9._]{1,30})")

COLD_CHECKS_PER_USER_PER_DAY = 5
CACHED_CHECKS_PER_USER_PER_DAY = 30
GROUP_CHECKS_PER_DAY = 20

_redis = None


def get_redis():
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.REDIS_URL)
    return _redis


def get_queue() -> Queue:
    return Queue("checks", connection=get_redis())


def parse_handle(text: str) -> str | None:
    """Accepts profile URLs, @handle, or bare handle. Returns normalized handle."""
    text = (text or "").strip()
    m = URL_RE.search(text)
    if m:
        return m.group(1).lower()
    candidate = text.lstrip("@").strip().rstrip("/")
    if HANDLE_RE.match(candidate) and not candidate.startswith("http"):
        return candidate.lower()
    return None


def _bump_daily(key: str, limit: int) -> bool:
    """Returns True if within limit after incrementing."""
    r = get_redis()
    count = r.incr(key)
    if count == 1:
        r.expire(key, 60 * 60 * 24)
    return count <= limit


def _is_admin(user_id: int) -> bool:
    admins = {x.strip() for x in settings.ADMIN_USER_IDS.split(",") if x.strip()}
    return str(user_id) in admins


def allow_cold_check(user_id: int) -> bool:
    if _is_admin(user_id):
        return True
    return _bump_daily(f"rl:cold:{user_id}", COLD_CHECKS_PER_USER_PER_DAY)


def allow_cached_check(user_id: int) -> bool:
    if _is_admin(user_id):
        return True
    return _bump_daily(f"rl:cached:{user_id}", CACHED_CHECKS_PER_USER_PER_DAY)


def allow_group_check(chat_id: int) -> bool:
    return _bump_daily(f"rl:group:{chat_id}", GROUP_CHECKS_PER_DAY)


def allow_global_cold_check() -> bool:
    """System-wide daily spend brake: N groups x 20 checks must not become
    unbounded Apify spend. Applies to EVERY cold-check enqueue (bot, web,
    ad-library pre-scan, report-triggered re-checks)."""
    from datetime import date

    return _bump_daily(f"rl:global:cold:{date.today().isoformat()}", settings.MAX_COLD_CHECKS_PER_DAY)


def enqueue_cold_check(handle: str, chat_id: str | None, schedule_followup: bool) -> bool:
    """The one gate every cold-check enqueue must pass. Returns False if the
    global daily cap is hit."""
    if not allow_global_cold_check():
        return False
    get_queue().enqueue("workers.jobs.cold_check_job", handle, chat_id, schedule_followup, job_timeout=600)
    return True


@dataclass
class CheckOutcome:
    kind: str  # 'cached' | 'stale' | 'queued' | 'rate_limited_cold' | 'rate_limited_cached'
    card_text: str | None = None
    banner: str | None = None


def start_check(handle: str, chat_id: str, user_id: int) -> CheckOutcome:
    """Cache-policy front door for a check request. Rate limits are enforced
    HERE, before anything is enqueued, so a limited user never triggers spend."""
    db = SessionLocal()
    try:
        seller, snapshot, state = lookup(db, handle)

        if state in ("fresh", "stale") and snapshot:
            if not allow_cached_check(user_id):
                return CheckOutcome(kind="rate_limited_cached")
            _record_cached_check(db, seller.id, chat_id)
            from engine.render import render_from_snapshot

            if state == "fresh":
                return CheckOutcome(kind="cached", card_text=render_from_snapshot(handle, snapshot))

            enqueue_cold_check(handle, None, False)  # background refresh; if capped, stale card still serves
            return CheckOutcome(
                kind="stale",
                card_text=render_from_snapshot(handle, snapshot),
                banner="⚠️ This report is over a week old — a fresh check is running; check again in a few minutes.",
            )

        if not allow_cold_check(user_id):
            return CheckOutcome(kind="rate_limited_cold")
        if not enqueue_cold_check(handle, chat_id, True):
            return CheckOutcome(kind="global_capped")
        return CheckOutcome(kind="queued")
    finally:
        db.close()


def _record_cached_check(db, seller_id: int, chat_id: str) -> None:
    from shared.models import Check

    db.add(Check(seller_id=seller_id, requested_by_chat_id=chat_id, served_from_cache=True, result=None))
    db.commit()


