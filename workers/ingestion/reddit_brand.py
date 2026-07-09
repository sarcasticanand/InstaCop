"""Reddit brand-review cache (Workstream A).

On a check: read brand_reviews first; if fresh (< BRAND_REVIEW_STALENESS_DAYS)
serve stored rows; else fetch, upsert, bump freshness, serve. Fully automated —
no manual step anywhere.

Access paths, in priority order (both compliant):
  1. SerpAPI Google queries scoped to reddit.com (works today)
  2. PRAW via REDDIT_* creds — behind REDDIT_API_ENABLED, activates without a
     rewrite if/when Reddit approves the operator's Data API application.
Direct reddit.com HTML scraping is deliberately NOT implemented (ToS + brittle).
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from shared.config import settings
from shared.models import BrandReview, BrandReviewFreshness, Seller

from engine.identity import brand_stem

logger = logging.getLogger(__name__)

SUBREDDITS_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "subreddits.txt"
SERPAPI_COST_INR = 0.015 * 83.0
RESULTS_PER_QUERY = 8


def load_subreddits() -> list[str]:
    if not SUBREDDITS_FILE.exists():
        return []
    return [
        line.strip().removeprefix("r/")
        for line in SUBREDDITS_FILE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def is_fresh(db: Session, seller_id: int, source: str = "reddit") -> bool:
    row = db.get(BrandReviewFreshness, (seller_id, source))
    if row is None or row.last_scraped_at is None:
        return False
    last = row.last_scraped_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last < timedelta(days=settings.BRAND_REVIEW_STALENESS_DAYS)


def _bump_freshness(db: Session, seller_id: int, source: str = "reddit") -> None:
    row = db.get(BrandReviewFreshness, (seller_id, source))
    if row is None:
        row = BrandReviewFreshness(seller_id=seller_id, source=source)
        db.add(row)
    row.last_scraped_at = datetime.now(timezone.utc)


def _queries_for(handle: str) -> list[str]:
    stem = brand_stem(handle)
    queries = [f'site:reddit.com "{handle}"']
    if stem and stem != handle.replace(".", "").replace("_", ""):
        queries.append(f'site:reddit.com "{stem}" review OR scam OR fraud')
    else:
        queries.append(f'site:reddit.com "{stem or handle}" review OR scam')
    return queries


def _upsert_review(db: Session, seller_id: int, source_url: str | None, text: str, subreddit: str | None) -> bool:
    if source_url:
        exists = db.query(BrandReview.id).filter_by(seller_id=seller_id, source_url=source_url).first()
        if exists:
            return False
    db.add(
        BrandReview(
            seller_id=seller_id,
            source="reddit",
            source_url=source_url,
            source_subreddit=subreddit,
            raw_text=text[:12000],
        )
    )
    db.flush()  # same-run duplicates (handle + stem queries hit the same thread) must be visible
    return True


def _subreddit_from_url(url: str) -> str | None:
    import re

    m = re.search(r"reddit\.com/r/([A-Za-z0-9_]+)/", url or "")
    return m.group(1) if m else None


def _fetch_via_serpapi(db: Session, seller: Seller) -> tuple[int, float]:
    inserted, cost = 0, 0.0
    client = httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (InstaCopBot)"})
    for query in _queries_for(seller.ig_handle):
        try:
            resp = client.get(
                "https://serpapi.com/search",
                params={"engine": "google", "q": query, "num": RESULTS_PER_QUERY, "api_key": settings.SERPAPI_KEY},
            )
            resp.raise_for_status()
            cost += SERPAPI_COST_INR
            results = resp.json().get("organic_results", []) or []
        except Exception as exc:
            logger.warning("reddit brand query failed (%s): %s", query, exc)
            continue

        for r in results:
            link = r.get("link") or ""
            if "reddit.com" not in link:
                continue
            text = f"{r.get('title', '')}\n{r.get('snippet', '')}"
            # pull thread text where possible (google cache of thread body via page fetch is
            # blocked by reddit; snippet + title is what we compliantly get on this path)
            if _upsert_review(db, seller.id, link, text, _subreddit_from_url(link)):
                inserted += 1
    return inserted, cost


def _fetch_via_praw(db: Session, seller: Seller) -> tuple[int, float]:
    import praw

    reddit = praw.Reddit(
        client_id=settings.REDDIT_CLIENT_ID,
        client_secret=settings.REDDIT_CLIENT_SECRET,
        user_agent=settings.REDDIT_USER_AGENT,
    )
    inserted = 0
    stem = brand_stem(seller.ig_handle)
    terms = {seller.ig_handle, stem} - {""}
    for sub in load_subreddits():
        for term in terms:
            try:
                for post in reddit.subreddit(sub).search(f'"{term}"', time_filter="year", limit=15):
                    post.comments.replace_more(limit=0)
                    body = f"[{post.title}]\n{post.selftext or ''}\n--- comments ---\n" + "\n".join(
                        c.body for c in post.comments[:10]
                    )
                    url = f"https://www.reddit.com{post.permalink}"
                    if _upsert_review(db, seller.id, url, body, sub):
                        author_age = None
                        try:
                            if post.author:
                                author_age = int((datetime.now(timezone.utc).timestamp() - post.author.created_utc) / 86400)
                        except Exception:
                            pass
                        review = db.query(BrandReview).filter_by(seller_id=seller.id, source_url=url).one()
                        review.source_author = str(post.author) if post.author else None
                        review.source_author_age_days = author_age
                        review.posted_at = datetime.fromtimestamp(post.created_utc, tz=timezone.utc)
                        inserted += 1
            except Exception as exc:
                logger.warning("praw search failed (r/%s, %s): %s", sub, term, exc)
    return inserted, 0.0


def ensure_brand_reviews(db: Session, seller: Seller, budget_ok: bool = True) -> tuple[list[BrandReview], float]:
    """The read-first front door. Returns (reviews, fetch_cost_inr)."""
    cost = 0.0
    if not is_fresh(db, seller.id) and budget_ok:
        try:
            if settings.REDDIT_API_ENABLED and settings.REDDIT_CLIENT_ID:
                inserted, cost = _fetch_via_praw(db, seller)
            else:
                inserted, cost = _fetch_via_serpapi(db, seller)
            _bump_freshness(db, seller.id)
            db.commit()
            logger.info("reddit brand fetch for %s: %d new reviews", seller.ig_handle, inserted)
        except Exception as exc:
            db.rollback()  # never poison the caller's session mid-check
            logger.warning("reddit brand fetch failed for %s: %s", seller.ig_handle, exc)
    elif not budget_ok:
        logger.info("reddit: skipped (cap) for %s", seller.ig_handle)

    reviews = db.query(BrandReview).filter_by(seller_id=seller.id).order_by(BrandReview.fetched_at.desc()).all()
    return reviews, cost
