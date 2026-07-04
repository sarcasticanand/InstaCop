import logging

import httpx

from shared.config import settings

logger = logging.getLogger(__name__)

SERPAPI_URL = "https://serpapi.com/search"
COST_PER_CALL_USD = 0.015  # SerpAPI ~$50/5000 searches
USD_TO_INR = 83.0


def reverse_image_matches(image_url: str) -> tuple[list[dict], float]:
    """Returns (matches, cost_inr). Each match: {source, title, link}."""
    try:
        resp = httpx.get(
            SERPAPI_URL,
            params={"engine": "google_lens", "url": image_url, "api_key": settings.SERPAPI_KEY},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("SerpAPI Lens call failed for %s: %s", image_url, exc)
        return [], 0.0

    matches = [
        {"source": r.get("source"), "title": r.get("title"), "link": r.get("link")}
        for r in data.get("visual_matches", []) or []
    ]
    return matches, COST_PER_CALL_USD * USD_TO_INR
