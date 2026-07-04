"""Meta Ad Library pre-scan (spec 5.5): find Instagram sellers running ads in
India by niche keyword, insert unknown ones, and queue low-priority checks so
fresh scam pages are scanned BEFORE their first victim asks.

Requires META_AD_LIBRARY_TOKEN (developers.facebook.com identity verification
has multi-day lead time — start early). Usage: python -m workers.adlibrary
"""

import logging
import re

import httpx

from shared.config import settings
from shared.db import SessionLocal
from shared.models import Seller

logger = logging.getLogger(__name__)

AD_LIBRARY_URL = "https://graph.facebook.com/v21.0/ads_archive"

NICHE_KEYWORDS = [
    "saree", "kurti", "lehenga", "sneakers", "watches", "perfume",
    "thrift", "jewellery", "handbags", "salwar",
]

IG_HANDLE_RE = re.compile(r"instagram\.com/([a-zA-Z0-9._]{2,30})")


def _handles_from_ad(ad: dict) -> set[str]:
    handles = set()
    blob = " ".join(
        str(ad.get(k, ""))
        for k in ("ad_creative_link_captions", "ad_creative_link_titles", "ad_creative_bodies", "page_name")
    )
    for m in IG_HANDLE_RE.finditer(blob):
        handles.add(m.group(1).lower())
    return handles


def run(limit_per_keyword: int = 50) -> tuple[int, int]:
    """Returns (handles_seen, new_sellers_queued)."""
    if not settings.META_AD_LIBRARY_TOKEN:
        raise SystemExit("META_AD_LIBRARY_TOKEN not set (developers.facebook.com -> Ad Library API; identity verification takes days)")

    from bot.service import enqueue_cold_check  # lazy: needs redis

    client = httpx.Client(timeout=30)
    db = SessionLocal()
    seen: set[str] = set()
    queued = 0
    try:
        for keyword in NICHE_KEYWORDS:
            try:
                resp = client.get(
                    AD_LIBRARY_URL,
                    params={
                        "access_token": settings.META_AD_LIBRARY_TOKEN,
                        "search_terms": keyword,
                        "ad_reached_countries": '["IN"]',
                        "ad_active_status": "ACTIVE",
                        "fields": "page_name,ad_creative_link_captions,ad_creative_link_titles,ad_creative_bodies",
                        "limit": limit_per_keyword,
                    },
                )
                resp.raise_for_status()
                ads = resp.json().get("data", [])
            except Exception as exc:
                logger.warning("ad library query failed (%s): %s", keyword, exc)
                continue

            for ad in ads:
                for handle in _handles_from_ad(ad):
                    if handle in seen:
                        continue
                    seen.add(handle)
                    if db.query(Seller.id).filter(Seller.ig_handle == handle).first():
                        continue
                    db.add(Seller(ig_handle=handle, category=keyword))
                    db.commit()
                    # low priority: no chat to notify, no followup; respects the
                    # global daily cap (pre-scan should never exhaust user capacity)
                    if enqueue_cold_check(handle, None, False):
                        queued += 1
                    else:
                        logger.info("global cold-check cap hit; stopping ad-library sweep for today")
                        return len(seen), queued
        logger.info("ad library sweep: %d handles seen, %d new sellers queued", len(seen), queued)
        return len(seen), queued
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
