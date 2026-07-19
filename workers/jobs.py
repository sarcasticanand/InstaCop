"""RQ jobs. The bot enqueues; `rq worker` (see workers/README.md) executes.
Jobs run in a separate process from the bot, so they send Telegram messages
via raw HTTP rather than sharing the aiogram dispatcher."""

import logging
from datetime import datetime, timedelta, timezone

import httpx

from shared.config import settings
from shared.db import SessionLocal
from shared.models import Check, Followup
from shared.observability import init_sentry

init_sentry("worker")

from engine.check import run_check

logger = logging.getLogger(__name__)

FOLLOWUP_DELAY_MINUTES = settings.FOLLOWUP_DELAY_MINUTES


def send_telegram(chat_id: str, text: str) -> None:
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN unset; would have sent to %s: %s", chat_id, text[:80])
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        ).raise_for_status()
    except Exception as exc:
        logger.error("Telegram send to %s failed: %s", chat_id, exc)


def send_user(chat_id: str, text: str) -> None:
    """Channel-aware send: "ig:<igsid>" chat ids answer over Instagram DM,
    everything else is a Telegram chat id."""
    if str(chat_id).startswith("ig:"):
        from bot.instagram import send_instagram

        send_instagram(str(chat_id).removeprefix("ig:"), text)
    else:
        send_telegram(chat_id, text)


def _early_signal_text(profile) -> str | None:
    reference = profile.oldest_post_at or profile.account_created_at
    if reference is None:
        return None
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - reference).days
    if age_days <= 90:
        return (
            f"early heads-up: this account looks only ~{age_days} days old and it's already selling. "
            "hold off on paying until the full report's in"
        )
    return None


def cold_check_job(
    handle: str, chat_id: str | None = None, schedule_followup: bool = True, user_id: int | None = None
) -> int | None:
    """Full risk check; records the check row, schedules the follow-up, and
    (if chat_id given) sends the finished card back to the requester.
    Returns the check id."""
    db = SessionLocal()

    def refund_quota():
        # failed checks must not count against the requester's daily limit
        if user_id is None:
            return
        try:
            from bot.service import refund_cold_check

            refund_cold_check(user_id)
        except Exception as exc:
            logger.warning("rate-limit refund failed for user %s: %s", user_id, exc)

    def on_profile(profile):
        if chat_id:
            text = _early_signal_text(profile)
            if text:
                send_user(chat_id, text)

    sent_experience = {"done": False}

    def on_experience(experience):
        # community reviews land in seconds — send them without waiting for
        # the slow Instagram scan
        if chat_id:
            from engine.render import render_experience_early

            text = render_experience_early(handle, experience)
            if text:
                send_user(chat_id, text)
                sent_experience["done"] = True

    try:
        card = run_check(handle, on_profile=on_profile, on_experience=on_experience, requested_by=chat_id or "system")

        if schedule_followup and chat_id and card.check_id:
            db.add(
                Followup(
                    check_id=card.check_id,
                    scheduled_for=datetime.now(timezone.utc) + timedelta(minutes=FOLLOWUP_DELAY_MINUTES),
                )
            )
            db.commit()

        if chat_id:
            if str(chat_id).startswith("ig:"):
                footer = "\n— got scammed by them? reply 'report'"
            else:
                footer = "\n— got scammed by them? tap /report"
            send_user(chat_id, card.card_text + footer)
        return card.check_id
    except SystemExit as exc:
        # engine raises SystemExit on unfetchable profiles; report kindly
        refund_quota()
        if chat_id:
            if sent_experience["done"]:
                send_user(
                    chat_id,
                    f"couldn't finish the account scan for @{handle} — {exc}. "
                    "the buyer reviews above still stand",
                )
            else:
                send_user(chat_id, f"couldn't check @{handle} — {exc}. sure the handle's right and the account's public?")
        return None
    except Exception:
        refund_quota()
        raise
    finally:
        db.close()


def reddit_sweep_job(subreddit: str, since_days: int = 365) -> dict:
    """Bulk-harvest one subreddit from Arctic Shift into brand_reviews."""
    from workers.ingestion.reddit_backfill import sweep_subreddit

    return sweep_subreddit(subreddit, since_days=since_days)


def recheck_stale_public_pages(max_rechecks: int = 20) -> int:
    """E2 staleness rule: a public High/Caution page older than 30 days gets a
    fresh check (band updates if the seller cleaned up). Respects the global
    daily cap. Returns number queued."""
    from sqlalchemy import func as sqlfunc

    from bot.service import enqueue_cold_check
    from shared.models import RiskSnapshot, Seller

    db = SessionLocal()
    queued = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        latest = (
            db.query(RiskSnapshot.seller_id, sqlfunc.max(RiskSnapshot.computed_at).label("last"))
            .group_by(RiskSnapshot.seller_id)
            .subquery()
        )
        rows = (
            db.query(Seller.ig_handle)
            .join(latest, latest.c.seller_id == Seller.id)
            .join(
                RiskSnapshot,
                (RiskSnapshot.seller_id == Seller.id) & (RiskSnapshot.computed_at == latest.c.last),
            )
            .filter(latest.c.last < cutoff, RiskSnapshot.risk_band.in_(["high", "caution"]))
            .limit(max_rechecks)
            .all()
        )
        for (handle,) in rows:
            if enqueue_cold_check(handle, None, False):
                queued += 1
            else:
                break  # global cap; try again tomorrow
        if queued:
            logger.info("staleness pass: queued %d re-checks", queued)
        return queued
    finally:
        db.close()


def send_due_followups() -> int:
    """Cron-style job: sends every due follow-up question. Returns count sent."""
    db = SessionLocal()
    sent = 0
    try:
        due = (
            db.query(Followup)
            .filter(Followup.scheduled_for <= datetime.now(timezone.utc), Followup.sent_at.is_(None))
            .limit(200)
            .all()
        )
        for fu in due:
            check = db.get(Check, fu.check_id) if fu.check_id else None
            if not check or not check.requested_by_chat_id or check.requested_by_chat_id == "system":
                fu.sent_at = datetime.now(timezone.utc)  # nothing to send to; close it out
                continue
            from shared.models import Seller

            seller = db.get(Seller, check.seller_id) if check.seller_id else None
            handle = seller.ig_handle if seller else "the seller"
            _send_followup_keyboard(check.requested_by_chat_id, handle, fu.id)
            fu.sent_at = datetime.now(timezone.utc)
            sent += 1
        db.commit()
        return sent
    finally:
        db.close()


FOLLOWUP_CHOICES = [
    ("Bought, all good ✅", "bought_good"),
    ("Bought, came late 🐢", "bought_late"),
    ("Bad quality 👎", "bought_bad"),
    ("Never arrived ❌", "never_arrived"),
    ("Didn't buy", "didnt_buy"),
]


def _send_followup_keyboard(chat_id: str, handle: str, followup_id: int) -> None:
    if str(chat_id).startswith("ig:"):
        from bot.instagram import send_instagram

        send_instagram(
            str(chat_id).removeprefix("ig:"),
            f"hey — you checked @{handle} a while back. did you end up buying?",
            quick_replies=[(title, f"fu:{followup_id}:{resp}") for title, resp in FOLLOWUP_CHOICES],
        )
        return
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN unset; would ask followup %s to %s", followup_id, chat_id)
        return
    buttons = [
        [{"text": "Bought, all good ✅", "callback_data": f"fu:{followup_id}:bought_good"}],
        [{"text": "Bought, came late 🐢", "callback_data": f"fu:{followup_id}:bought_late"}],
        [{"text": "Bought, bad quality 👎", "callback_data": f"fu:{followup_id}:bought_bad"}],
        [{"text": "Never arrived ❌", "callback_data": f"fu:{followup_id}:never_arrived"}],
        [{"text": "Didn't buy", "callback_data": f"fu:{followup_id}:didnt_buy"}],
    ]
    try:
        httpx.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": f"hey — you checked @{handle} a while back. did you end up buying?",
                "reply_markup": {"inline_keyboard": buttons},
            },
            timeout=15,
        ).raise_for_status()
    except Exception as exc:
        logger.error("Followup send to %s failed: %s", chat_id, exc)
