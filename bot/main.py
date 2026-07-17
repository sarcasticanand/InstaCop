"""InstaCop Telegram bot (aiogram 3).

Dev: long polling (`python -m bot.main`). Prod: switch to webhook by setting
WEBHOOK_URL — see run() below.
"""

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from shared.config import settings
from shared.db import SessionLocal
from shared.models import Check, Followup, Seller
from shared.observability import init_sentry

init_sentry("bot")

from bot import service

logging.basicConfig(level=logging.DEBUG if __import__("os").environ.get("BOT_DEBUG") else logging.INFO)
logger = logging.getLogger(__name__)

router = Router()


@router.message.outer_middleware()
async def allowlist_middleware(handler, event, data):
    """Private-beta gate: while the bot runs on the operator's laptop, only
    allowlisted Telegram user ids get service (BOT_ALLOWLIST env, comma-sep).
    Empty allowlist = open to everyone (production)."""
    allowlist = {x.strip() for x in settings.BOT_ALLOWLIST.split(",") if x.strip()}
    if allowlist and event.from_user and str(event.from_user.id) not in allowlist:
        await event.answer("This bot is in private beta — public launch soon. Follow for updates!")
        return None
    return await handler(event, data)


@router.callback_query.outer_middleware()
async def allowlist_callback_middleware(handler, event, data):
    allowlist = {x.strip() for x in settings.BOT_ALLOWLIST.split(",") if x.strip()}
    if allowlist and event.from_user and str(event.from_user.id) not in allowlist:
        await event.answer("Private beta.", show_alert=False)
        return None
    return await handler(event, data)


START_TEXT = (
    "I check Instagram shops for known fraud patterns before you pay.\n"
    "Send me a shop's link or @handle — or forward their profile: Share → Telegram → me."
)

REPORT_KINDS = [
    ("never_delivered", "Never delivered"),
    ("fake_product", "Fake or wrong item"),
    ("bad_quality", "Very poor quality"),
    ("late", "Months late"),
    ("other", "Other"),
]


class ReportFlow(StatesGroup):
    choosing_kind = State()
    awaiting_screenshot = State()
    awaiting_narrative = State()


def _is_group(message: Message) -> bool:
    return message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


# --- /start ------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message):
    args = (message.text or "").split(maxsplit=1)
    # deep link: t.me/<bot>?start=check_<handle>
    if len(args) > 1 and args[1].startswith("check_"):
        await _handle_check(message, args[1].removeprefix("check_"))
        return
    await message.answer(START_TEXT)


# --- check flow ---------------------------------------------------------

@router.message(Command("check"))
async def cmd_check(message: Message):
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage: /check <handle or instagram link>")
        return
    await _handle_check(message, args[1])


async def _handle_from_screenshot(message: Message) -> str | None:
    try:
        photo = message.photo[-1]
        file = await message.bot.get_file(photo.file_id)
        buf = await message.bot.download_file(file.file_path)
        from engine.ig_provider import ScreenshotVisionIGProvider

        profile, _ = ScreenshotVisionIGProvider().extract_from_screenshot(buf.read())
        return profile.handle if profile.handle != "unknown" else None
    except Exception as exc:
        logger.warning("screenshot handle extraction failed: %s", exc)
        return None


async def _handle_check(message: Message, raw: str):
    handle = service.parse_handle(raw)
    if handle is None:
        await message.answer("That doesn't look like an Instagram handle or link.")
        return

    user_id = message.from_user.id
    if _is_group(message):
        if not service.allow_group_check(message.chat.id):
            await message.answer("This group hit today's check limit (20). Try again tomorrow.")
            return

    chat_id = str(message.chat.id)
    outcome = await asyncio.to_thread(service.start_check, handle, chat_id, user_id)

    if outcome.kind == "rate_limited_cold":
        await message.answer("You've used today's 5 fresh checks. Cached sellers still work — try tomorrow for new ones.")
        return
    if outcome.kind == "rate_limited_cached":
        await message.answer("You've hit today's check limit. Try again tomorrow.")
        return
    if outcome.kind == "global_capped":
        await message.answer("We're at today's system-wide check capacity. Cached sellers still work — try this one tomorrow.")
        return

    if outcome.kind == "queued":
        await message.answer(f"🔍 Checking @{handle} — full report in ~3 min.")
        return

    text = outcome.card_text or f"@{handle}: report unavailable, try again."
    if outcome.banner:
        text = f"{text}\n\n{outcome.banner}"
    text += "\n─ Got scammed by them? Tap: /report"
    await message.answer(text)


# --- /fresh: admin-only cache bypass for testing --------------------------

@router.message(Command("fresh"))
async def cmd_fresh(message: Message):
    if not service._is_admin(message.from_user.id):
        return  # silently ignore for non-admins
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage: /fresh <handle> — forces a fresh check, skipping the cache.")
        return
    handle = service.parse_handle(args[1])
    if handle is None:
        await message.answer("That doesn't look like an Instagram handle or link.")
        return

    # /fresh means bypass ALL caches — including the brand-review freshness
    # stamp, or a previously failed reddit fetch stays silenced for 30 days
    from shared.models import BrandReviewFreshness

    db = SessionLocal()
    try:
        seller = db.query(Seller).filter(Seller.ig_handle == handle).one_or_none()
        if seller:
            db.query(BrandReviewFreshness).filter_by(seller_id=seller.id).delete()
            db.commit()
    finally:
        db.close()

    if service.enqueue_cold_check(handle, str(message.chat.id), False, user_id=message.from_user.id):
        await message.answer(f"🔄 Fresh check queued for @{handle} (all caches bypassed).")
    else:
        await message.answer("Global daily check cap reached.")


# --- /sweep: admin-only bulk reddit harvest -------------------------------

@router.message(Command("sweep"))
async def cmd_sweep(message: Message):
    if not service._is_admin(message.from_user.id):
        return
    args = (message.text or "").split()
    since_days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 365
    from workers.ingestion.reddit_brand import load_subreddits

    subs = load_subreddits()
    queue = service.get_queue()
    for sub in subs:
        queue.enqueue("workers.jobs.reddit_sweep_job", sub, since_days, job_timeout=1800)
    await message.answer(
        f"🧹 Queued sweeps for {len(subs)} subreddits (last {since_days} days). "
        "They run one by one — watch the logs; brands land in the DB as they're found."
    )


# --- /report flow -------------------------------------------------------

@router.message(Command("report"))
async def cmd_report(message: Message, state: FSMContext):
    chat_id = str(message.chat.id)
    db = SessionLocal()
    try:
        from engine.reporter_trust import report_rate_ok

        if not report_rate_ok(db, chat_id):
            await message.answer("We've logged enough from you today — thank you. Try again tomorrow.")
            return
        last = (
            db.query(Seller)
            .join(Check, Check.seller_id == Seller.id)
            .filter(Check.requested_by_chat_id == chat_id)
            .order_by(Check.requested_at.desc())
            .first()
        )
    finally:
        db.close()

    if last is None:
        await message.answer("Which seller? Check them first (send their @handle), then tap /report.")
        return

    await state.update_data(seller_id=last.id, handle=last.ig_handle)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=f"rk:{kind}")] for kind, label in REPORT_KINDS]
    )
    await state.set_state(ReportFlow.choosing_kind)
    await message.answer(f"Reporting @{last.ig_handle}. What happened?", reply_markup=kb)


@router.callback_query(ReportFlow.choosing_kind, F.data.startswith("rk:"))
async def report_kind_chosen(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.removeprefix("rk:")
    await state.update_data(kind=kind)
    await state.set_state(ReportFlow.awaiting_screenshot)
    await cb.message.answer("Send a payment screenshot if you have one (or /skip).")
    await cb.answer()


@router.message(ReportFlow.awaiting_screenshot, Command("skip"))
async def report_skip_screenshot(message: Message, state: FSMContext):
    await state.update_data(evidence_file_id=None, payment=None)
    await state.set_state(ReportFlow.awaiting_narrative)
    await message.answer("One line about what happened (or /skip):")


@router.message(ReportFlow.awaiting_screenshot, F.photo)
async def report_screenshot(message: Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    payment = None
    try:
        file = await message.bot.get_file(file_id)
        buf = await message.bot.download_file(file.file_path)
        payment = await asyncio.to_thread(_extract_payment_identity, buf.read())
    except Exception as exc:
        logger.warning("payment screenshot extraction failed: %s", exc)
    await state.update_data(evidence_file_id=file_id, payment=payment)
    await state.set_state(ReportFlow.awaiting_narrative)
    note = f" (found {payment['kind']}: {payment['value']})" if payment else ""
    await message.answer(f"Got the screenshot{note}. One line about what happened (or /skip):")


@router.message(ReportFlow.awaiting_narrative)
async def report_narrative(message: Message, state: FSMContext):
    narrative = None if (message.text or "").strip() == "/skip" else message.text
    data = await state.get_data()
    await state.clear()
    await asyncio.to_thread(_save_report, data, str(message.chat.id), narrative)
    await message.answer("Logged. This protects the next buyer. 🙏")


# report intake logic lives in bot/service.py (shared with the Instagram bot)
_extract_payment_identity = service.extract_payment_identity
_save_report = service.save_report


# --- follow-up buttons ----------------------------------------------------

@router.callback_query(F.data.startswith("fu:"))
async def followup_answer(cb: CallbackQuery):
    _, followup_id, response = cb.data.split(":", 2)
    db = SessionLocal()
    try:
        fu = db.get(Followup, int(followup_id))
        if fu:
            fu.response = response
            fu.responded_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()

    if response in ("never_arrived", "bought_bad"):
        await cb.message.answer("Sorry to hear that. Tap /report to log it — it protects the next buyer.")
    else:
        await cb.message.answer("Thanks — this helps other buyers!")
    await cb.answer()


# --- group mode -----------------------------------------------------------

@router.message(F.text, F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def group_text(message: Message):
    me = await message.bot.get_me()
    text = message.text or ""
    if f"@{me.username}" not in text and not text.startswith("/check"):
        return
    cleaned = text.replace(f"@{me.username}", " ").removeprefix("/check").strip()
    if cleaned:
        await _handle_check(message, cleaned)


# --- catch-all handlers (MUST be registered last) --------------------------
# aiogram gives the message to the FIRST matching handler. These match any
# private text/photo, so registering them before the command and FSM handlers
# silently swallowed /sweep, /fresh, /report and the report-flow replies.

@router.message(F.text, F.chat.type == ChatType.PRIVATE)
async def any_text(message: Message, state: FSMContext):
    if await state.get_state() is not None:
        return  # mid-flow message for a state handler; never treat as a handle
    if (message.text or "").startswith("/"):
        await message.answer("Unknown command. Send an Instagram @handle or link to check a seller.")
        return
    handle = service.parse_handle(message.text)
    if handle is None:
        await message.answer("Send an Instagram profile link or @handle to check it.")
        return
    await _handle_check(message, handle)


@router.message(F.photo, F.chat.type == ChatType.PRIVATE)
async def photo_check(message: Message, state: FSMContext):
    if await state.get_state() is not None:
        return
    await message.answer("Reading the screenshot…")
    handle = await _handle_from_screenshot(message)
    if handle is None:
        await message.answer("Couldn't read a handle from that screenshot — type the @handle instead.")
        return
    await _handle_check(message, handle)


# --- runner ----------------------------------------------------------------

async def run():
    if not settings.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set — create a bot with @BotFather and add it to .env")
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    logger.info("Bot starting (long polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(run())
