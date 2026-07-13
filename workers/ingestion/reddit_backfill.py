"""Bulk Reddit harvester on Arctic Shift (free, no auth, 2005→today).

The proactive layer: instead of fetching reviews per-check, sweep the
configured subreddits for buyer-experience threads about Instagram sellers,
extract WHICH brands they discuss, and store everything as brand_reviews.
Result: checks answer instantly from the database, and brands nobody has
checked yet are already on file. Run the full backfill once (/sweep), then
monthly deltas via the scheduler.
"""

import logging
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from shared.db import SessionLocal
from shared.models import BrandReview, Seller

logger = logging.getLogger(__name__)

ARCTIC = "https://arctic-shift.photon-reddit.com/api"
PER_PAGE = 100
COMMENTS_PER_POST = 20
HANDLE_RE = re.compile(r"^[a-z0-9._]{2,30}$")

# A post is worth an LLM look when it signals a bad buying experience OR
# explicitly involves an Instagram shop. Broad terms alone ("review",
# "order") drown us in restaurant threads from the city subreddits.
STRONG = re.compile(
    r"scam|fraud|legit\b|fake|refund|cheated|never (arrived|received|delivered)|didn'?t (arrive|receive)",
    re.I,
)
IG_HINT = re.compile(r"instagram|insta\b|ig (page|store|shop|brand)|@[a-z0-9._]{3,}", re.I)

EXTRACT_SYSTEM = (
    "You read a Reddit thread from an Indian shopping community. Identify small brands / "
    "Instagram-based sellers that buyers describe EXPERIENCES with (ordered, bought, scammed, "
    "reviewed, warned about). "
    'Return ONLY JSON: {"brands": [{"name": str, "ig_handle": str|null}]}. '
    "ig_handle only when explicitly present as an @handle or instagram.com link — do not guess. "
    "Exclude big marketplaces (Amazon, Flipkart, Myntra, Ajio, Nykaa, Meesho, Snapdeal) and "
    "non-shopping topics. If no buyer experience with a nameable brand is described, return an "
    "empty list."
)


def _relevant(text: str) -> bool:
    return bool(STRONG.search(text) or IG_HINT.search(text))


def _fetch_posts(client: httpx.Client, sub: str, since: datetime, max_pages: int) -> list[dict]:
    """Newest-first pages until `since` or the page cap."""
    posts: list[dict] = []
    before: int | None = None
    for _ in range(max_pages):
        params = {"subreddit": sub, "limit": PER_PAGE, "sort": "desc"}
        if before:
            params["before"] = before
        resp = client.get(f"{ARCTIC}/posts/search", params=params)
        resp.raise_for_status()
        batch = resp.json().get("data", [])
        if not batch:
            break
        posts += batch
        oldest = batch[-1].get("created_utc") or 0
        before = int(oldest)
        if datetime.fromtimestamp(oldest, tz=timezone.utc) < since or len(batch) < PER_PAGE:
            break
        time.sleep(0.4)
    cutoff = since.timestamp()
    return [p for p in posts if (p.get("created_utc") or 0) >= cutoff]


def _fetch_comments(client: httpx.Client, post_id: str) -> list[str]:
    try:
        resp = client.get(f"{ARCTIC}/comments/search", params={"link_id": post_id, "limit": 50})
        resp.raise_for_status()
        items = resp.json().get("data", [])
    except Exception as exc:
        logger.info("arctic comments failed for %s: %s", post_id, exc)
        return []
    out = []
    for c in items:
        body = c.get("body") or ""
        author = (c.get("author") or "").lower()
        if not body or body in ("[removed]", "[deleted]") or author == "automoderator":
            continue
        out.append(body)
        if len(out) >= COMMENTS_PER_POST:
            break
    return out


def _extract_brands(text: str) -> tuple[list[dict], float]:
    from engine.issues import _parse
    from engine.llm import get_extract_llm

    raw, cost = get_extract_llm().complete(EXTRACT_SYSTEM, text[:6000], max_tokens=300, json_mode=True)
    data = _parse(raw)
    return [b for b in (data.get("brands") or []) if b.get("name")], cost


def _handle_for(brand: dict) -> str | None:
    """Explicit handle when given; else a conservative guess from the name.
    Guessed handles link up the moment a user checks that handle."""
    raw = (brand.get("ig_handle") or "").lstrip("@").split("instagram.com/")[-1].strip("/ ").lower()
    if raw and HANDLE_RE.match(raw):
        return raw
    guess = re.sub(r"[^a-z0-9._]", "", (brand.get("name") or "").lower().replace(" ", ""))
    return guess if HANDLE_RE.match(guess) else None


def sweep_subreddit(sub: str, since_days: int = 365, max_pages: int = 20) -> dict:
    """Harvest one subreddit into brand_reviews. Idempotent — reruns skip
    already-stored (seller, thread) pairs. Returns stats."""
    from workers.ingestion.reddit_brand import _upsert_review

    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    stats = {"sub": sub, "scanned": 0, "relevant": 0, "brands": 0, "stored": 0, "cost_inr": 0.0}
    client = httpx.Client(timeout=30, headers={"User-Agent": "instacop-research/0.1"})
    db = SessionLocal()
    try:
        posts = _fetch_posts(client, sub, since, max_pages)
        stats["scanned"] = len(posts)
        for post in posts:
            title = post.get("title") or ""
            body = post.get("selftext") or ""
            if body in ("[removed]", "[deleted]"):
                body = ""
            if not _relevant(f"{title}\n{body}"):
                continue
            stats["relevant"] += 1

            comments = _fetch_comments(client, str(post.get("id") or ""))
            text = "\n".join(filter(None, [f"[{title}]", body, "--- comments ---" if comments else "", *comments]))

            try:
                brands, cost = _extract_brands(text)
            except Exception as exc:
                logger.warning("brand extraction failed (%s): %s", post.get("id"), exc)
                continue
            stats["cost_inr"] += cost

            url = f"https://www.reddit.com{post.get('permalink', '')}" if post.get("permalink") else None
            posted_at = (
                datetime.fromtimestamp(post["created_utc"], tz=timezone.utc) if post.get("created_utc") else None
            )
            for brand in brands:
                handle = _handle_for(brand)
                if not handle:
                    continue
                stats["brands"] += 1
                seller = db.query(Seller).filter(Seller.ig_handle == handle).one_or_none()
                if seller is None:
                    seller = Seller(ig_handle=handle, display_name=brand.get("name"))
                    db.add(seller)
                    db.flush()
                if _upsert_review(db, seller.id, url, text, sub):
                    if url:
                        row = db.query(BrandReview).filter_by(seller_id=seller.id, source_url=url).one()
                        row.posted_at = posted_at
                        row.source_author = post.get("author")
                    stats["stored"] += 1
            db.commit()
            time.sleep(0.4)  # be polite to the free community API
        logger.info("sweep r/%s: %s", sub, stats)
        return stats
    finally:
        db.close()
