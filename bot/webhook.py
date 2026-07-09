"""Prod bot: webhook mode. Mounted onto the FastAPI process so we don't run a
separate always-on bot server; Telegram POSTs updates to a URL we hand it, we
feed them to aiogram's dispatcher. Long polling is the dev path (bot/main.py).

Wired from api/app/main.py: `from bot.webhook import mount_webhook`."""

import logging

from aiogram import Bot, Dispatcher
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request

from shared.config import settings

from bot.main import router as bot_router

logger = logging.getLogger(__name__)


def mount_webhook(app: FastAPI) -> None:
    if not settings.TELEGRAM_BOT_TOKEN or not settings.WEBHOOK_URL:
        logger.info("webhook mode disabled (TELEGRAM_BOT_TOKEN/WEBHOOK_URL unset)")
        return

    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(bot_router)

    router = APIRouter()

    @app.on_event("startup")
    async def _register_webhook():
        try:
            webhook_url = settings.WEBHOOK_URL.rstrip("/") + "/telegram/webhook"
            await bot.set_webhook(
                url=webhook_url,
                secret_token=settings.WEBHOOK_SECRET or None,
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=False,
            )
            logger.info("webhook registered at %s", webhook_url)
        except Exception as exc:
            logger.error("webhook registration failed: %s", exc)

    @app.on_event("shutdown")
    async def _close_session():
        # NOTE: deliberately do NOT delete_webhook here. Free-tier hosts spin
        # the process down when idle; the webhook must stay registered so
        # Telegram's next POST wakes the service back up.
        await bot.session.close()

    @router.post("/telegram/webhook")
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str = Header(default=""),
    ):
        import hmac

        if settings.WEBHOOK_SECRET and not hmac.compare_digest(
            x_telegram_bot_api_secret_token, settings.WEBHOOK_SECRET
        ):
            raise HTTPException(401, "bad secret")
        update = await request.json()
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}

    app.include_router(router)
