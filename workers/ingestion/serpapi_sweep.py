"""SerpAPI discovery sweeps (spec 5.4): cheap weekly Google queries that find
scam mentions on sites we don't crawl directly. Result URLs are fetched and
dumped into raw_mentions. Usage: python -m workers.ingestion.serpapi_sweep"""

import logging
import time

import httpx
from bs4 import BeautifulSoup

from shared.config import settings

from .common import save_mentions

logger = logging.getLogger(__name__)

# Reddit self-serve API access is gone (Responsible Builder Policy, 2025) --
# until/unless the manual application is approved, Google's index via SerpAPI
# is our compliant window into Reddit. These mirror the PRAW query set.
QUERIES = [
    "site:consumercomplaints.in instagram seller",
    '"instagram" "scam" "upi" site:reddit.com',
    '"instagram scam" site:reddit.com/r/india',
    '"instagram seller" fraud site:reddit.com/r/IndiaSocial',
    '"never delivered" instagram site:reddit.com',
    '"instagram boutique" scam site:reddit.com',
    '"instagram shop" fake site:reddit.com/r/onlineshopping',
    '"saree" instagram scam',
    '"sneakers" instagram fake',
    '"kurti" instagram seller fraud',
    '"lehenga" instagram scam',
    '"perfume" instagram fake india',
]

RESULTS_PER_QUERY = 10


def run() -> int:
    if not settings.SERPAPI_KEY:
        raise SystemExit("SERPAPI_KEY not set")
    total = 0
    client = httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (TrustKaroBot)"})
    for query in QUERIES:
        try:
            resp = client.get(
                "https://serpapi.com/search",
                params={"engine": "google", "q": query, "num": RESULTS_PER_QUERY, "api_key": settings.SERPAPI_KEY},
            )
            resp.raise_for_status()
            results = resp.json().get("organic_results", []) or []
        except Exception as exc:
            logger.warning("serpapi query failed (%s): %s", query, exc)
            continue

        rows = []
        for r in results:
            link = r.get("link")
            snippet = f"{r.get('title', '')}\n{r.get('snippet', '')}"
            page_text = ""
            if link:
                try:
                    page = client.get(link)
                    if page.status_code == 200:
                        page_text = BeautifulSoup(page.text, "html.parser").get_text(" ", strip=True)[:8000]
                    time.sleep(1)
                except Exception:
                    pass
            rows.append({"source_url": link, "content": f"[query: {query}]\n{snippet}\n---\n{page_text}"})
        total += save_mentions("serpapi", rows)
    logger.info("serpapi sweep complete: %d new mentions", total)
    return total


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
