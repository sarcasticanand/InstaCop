"""Consumer complaint site ingestion (spec 5.2). Plain httpx + BeautifulSoup,
1 request / 3s, respects robots.txt disallows for our paths; if a site blocks
us we skip it — no evasion. Usage: python -m workers.ingestion.complaint_sites [pages]"""

import logging
import sys
import time
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from .common import save_mentions

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; TrustKaroBot/0.1; +https://trustkaro.example/bot)"
DELAY_SECONDS = 3

SITES = [
    {
        "name": "consumercomplaints.in",
        "search_url": "https://www.consumercomplaints.in/search?search={query}&page={page}",
        "query": "instagram",
        "item_selector": "div.complaint-item, td.complaint",
        "link_selector": "a",
    },
]


def _allowed(base_url: str, path_url: str) -> bool:
    try:
        rp = RobotFileParser()
        rp.set_url(urljoin(base_url, "/robots.txt"))
        rp.read()
        return rp.can_fetch(USER_AGENT, path_url)
    except Exception:
        return True  # robots unreachable -> proceed politely


def run(max_pages: int = 3) -> int:
    total = 0
    client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True)
    for site in SITES:
        for page in range(1, max_pages + 1):
            url = site["search_url"].format(query=site["query"], page=page)
            if not _allowed(url, url):
                logger.info("robots.txt disallows %s; skipping site", url)
                break
            try:
                resp = client.get(url)
                if resp.status_code in (403, 429):
                    logger.info("%s returned %s; backing off, skipping site", site["name"], resp.status_code)
                    break
                resp.raise_for_status()
            except Exception as exc:
                logger.warning("fetch failed %s: %s", url, exc)
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            rows = []
            for item in soup.select(site["item_selector"]) or soup.select("div, article"):
                text = item.get_text(" ", strip=True)
                if "instagram" not in text.lower() or len(text) < 80:
                    continue
                link = item.select_one(site["link_selector"])
                href = urljoin(url, link["href"]) if link and link.has_attr("href") else None
                rows.append({"source_url": href, "content": text})
            total += save_mentions("complaint_site", rows[:50])
            time.sleep(DELAY_SECONDS)
    return total


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 3)
