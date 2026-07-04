"""Public Telegram scam-alert channel reader (spec 5.3). Uses the operator's
own account via Telethon (TELETHON_API_ID/HASH/SESSION). Channels come from
config/telegram_channels.txt — one public username per line, curated manually.
Usage: python -m workers.ingestion.telegram_channels [messages_per_channel]"""

import asyncio
import logging
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

from shared.config import settings

from .common import save_mentions

logger = logging.getLogger(__name__)

CHANNELS_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "telegram_channels.txt"


def load_channels() -> list[str]:
    if not CHANNELS_FILE.exists():
        return []
    return [
        line.strip().lstrip("@")
        for line in CHANNELS_FILE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


async def run(limit_per_channel: int = 50) -> int:
    if not (settings.TELETHON_API_ID and settings.TELETHON_API_HASH and settings.TELETHON_SESSION):
        raise SystemExit("TELETHON_API_ID / TELETHON_API_HASH / TELETHON_SESSION not set (see my.telegram.org)")
    channels = load_channels()
    if not channels:
        raise SystemExit(f"No channels in {CHANNELS_FILE} — add public channel usernames, one per line")

    total = 0
    client = TelegramClient(StringSession(settings.TELETHON_SESSION), int(settings.TELETHON_API_ID), settings.TELETHON_API_HASH)
    async with client:
        for channel in channels:
            try:
                rows = []
                async for msg in client.iter_messages(channel, limit=limit_per_channel):
                    text = msg.message or ""
                    if len(text) < 30:
                        continue
                    rows.append({
                        "source_url": f"https://t.me/{channel}/{msg.id}",
                        "content": text,
                    })
                total += save_mentions("telegram_channel", rows)
            except Exception as exc:
                logger.warning("channel %s failed: %s", channel, exc)
    logger.info("telegram channels complete: %d new mentions", total)
    return total


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(int(sys.argv[1]) if len(sys.argv) > 1 else 50))
