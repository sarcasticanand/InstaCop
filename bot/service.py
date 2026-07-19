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


# TLDs we treat as "this is a website, not a dotted IG handle". A handle like
# shop.bluorng stays a handle; bluorng.com becomes a site check.
_COMMON_TLDS = {
    "com", "in", "shop", "store", "co", "net", "org", "io", "me", "us", "uk",
    "au", "ca", "de", "fr", "nl", "es", "it", "xyz", "site", "online", "biz",
    "info", "club", "fashion", "boutique", "app", "ai", "tech", "life", "world",
}


def parse_website(text: str) -> str | None:
    """Returns a normalized https URL when the message is a (non-Instagram)
    website; None otherwise. Checked BEFORE parse_handle by callers, because
    'bluorng.com' would otherwise pass as a dotted handle."""
    from urllib.parse import urlparse

    t = (text or "").strip()
    m = re.search(r"https?://\S+", t, re.I)
    if m:
        cand = m.group(0).rstrip(").,>\"'")
    else:
        if not re.fullmatch(r"(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+/?", t, re.I):
            return None
        tld = t.rstrip("/").rsplit(".", 1)[-1].lower()
        if tld not in _COMMON_TLDS:
            return None
        cand = "https://" + t.rstrip("/")
    host = (urlparse(cand).hostname or "").lower()
    if not host or "instagram.com" in host or host == "instagr.am":
        return None  # instagram links belong to parse_handle
    return cand


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


def refund_cold_check(user_id: int | str) -> None:
    """Give back one cold-check credit when a check fails through no fault of
    the user (bad handle, IG block, crash) — failed checks must not count."""
    r = get_redis()
    key = f"rl:cold:{user_id}"
    if int(r.get(key) or 0) > 0:
        r.decr(key)


def enqueue_cold_check(handle: str, chat_id: str | None, schedule_followup: bool, user_id: int | None = None) -> bool:
    """The one gate every cold-check enqueue must pass. Returns False if the
    global daily cap is hit."""
    if not allow_global_cold_check():
        return False
    get_queue().enqueue("workers.jobs.cold_check_job", handle, chat_id, schedule_followup, user_id, job_timeout=600)
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
        if not enqueue_cold_check(handle, chat_id, True, user_id=user_id):
            return CheckOutcome(kind="global_capped")
        return CheckOutcome(kind="queued")
    finally:
        db.close()


def _record_cached_check(db, seller_id: int, chat_id: str) -> None:
    from shared.models import Check

    db.add(Check(seller_id=seller_id, requested_by_chat_id=chat_id, served_from_cache=True, result=None))
    db.commit()


# --- report intake (shared by the Telegram and Instagram bots) --------------

def extract_payment_identity(image_bytes: bytes) -> dict | None:
    """Vision-extract UPI id / phone from a payment screenshot. PII: routed
    through get_pii_llm(), never free-tier Gemini in prod."""
    import json as _json
    import re as _re

    from engine.llm import get_pii_llm

    text, _cost = get_pii_llm().complete_vision(
        "Extract payment identifiers from this payment screenshot. Return ONLY JSON: "
        '{"upi_id": str|null, "phone": str|null}. Do not guess.',
        image_bytes,
        "image/jpeg",
        max_tokens=200,
    )
    m = _re.search(r"\{.*\}", text or "", _re.DOTALL)
    if not m:
        return None
    data = _json.loads(m.group(0))
    if data.get("upi_id"):
        return {"kind": "upi", "value": data["upi_id"].lower()}
    if data.get("phone"):
        return {"kind": "phone", "value": _re.sub(r"\D", "", data["phone"])[-10:]}
    return None


def save_report(data: dict, chat_id: str, narrative: str | None) -> None:
    """Persist a buyer report (+ payment identity link, trust and spam scores).
    `data` needs seller_id; optional: kind, evidence_file_id, payment."""
    import logging

    from shared.models import PaymentIdentity, Report, SellerPaymentLink

    logger = logging.getLogger(__name__)
    db = SessionLocal()
    try:
        payment_identity_id = None
        payment = data.get("payment")
        if payment:
            pi = (
                db.query(PaymentIdentity)
                .filter(PaymentIdentity.kind == payment["kind"], PaymentIdentity.value == payment["value"])
                .one_or_none()
            )
            if pi is None:
                pi = PaymentIdentity(kind=payment["kind"], value=payment["value"])
                db.add(pi)
                db.flush()
            payment_identity_id = pi.id
            existing_link = (
                db.query(SellerPaymentLink)
                .filter_by(seller_id=data["seller_id"], payment_identity_id=pi.id)
                .one_or_none()
            )
            if existing_link is None:
                db.add(SellerPaymentLink(seller_id=data["seller_id"], payment_identity_id=pi.id, source="user_report"))

        issue_categories = None
        if narrative:
            try:
                from engine.issues import classify_text

                issue_categories, _sentiment, _cost = classify_text(narrative)
            except Exception as exc:
                logger.warning("report narrative classification failed: %s", exc)

        from engine.reporter_trust import reporter_trust
        from engine.spam_detect import check_smear_burst, spam_score_for_report

        has_evidence = bool(data.get("evidence_file_id")) and payment_identity_id is not None
        trust = reporter_trust(db, chat_id, has_evidence=has_evidence)

        report = Report(
            seller_id=data["seller_id"],
            reporter_chat_id=chat_id,
            kind=data.get("kind", "other"),
            narrative=narrative,
            evidence_file_id=data.get("evidence_file_id"),
            payment_identity_id=payment_identity_id,
            issue_categories=issue_categories,
            reporter_trust=trust,
        )
        db.add(report)
        db.flush()
        report.spam_score = spam_score_for_report(db, report, trust)
        db.commit()

        # burst check runs AFTER commit so this report counts toward the window
        try:
            check_smear_burst(db, data["seller_id"])
        except Exception as exc:
            logger.warning("smear burst check failed: %s", exc)
    finally:
        db.close()


