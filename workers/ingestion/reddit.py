"""Reddit ingestion (spec 5.1). Usage:
    python -m workers.ingestion.reddit backfill   # one-time, time_filter=year
    python -m workers.ingestion.reddit daily      # cron, time_filter=day
Requires REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET (script app, free)."""

import logging
import sys

import praw

from shared.config import settings

from .common import save_mentions

logger = logging.getLogger(__name__)

SUBREDDITS = [
    "india", "IndiaSocial", "onlineshopping", "IndianFashionAddicts",
    "delhi", "mumbai", "bangalore", "Chandigarh", "kolkata", "IndianSkincareAddicts",
]
QUERIES = [
    '"instagram scam"',
    '"instagram seller"',
    '"instagram shop fraud"',
    '"never delivered" instagram',
    '"instagram boutique"',
]


def run(mode: str = "daily") -> int:
    if not settings.REDDIT_CLIENT_ID or not settings.REDDIT_CLIENT_SECRET:
        raise SystemExit("REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set (create a script app at reddit.com/prefs/apps)")

    reddit = praw.Reddit(
        client_id=settings.REDDIT_CLIENT_ID,
        client_secret=settings.REDDIT_CLIENT_SECRET,
        user_agent=settings.REDDIT_USER_AGENT,
    )
    time_filter = "year" if mode == "backfill" else "day"
    limit = 100 if mode == "backfill" else 25

    total = 0
    for sub in SUBREDDITS:
        for query in QUERIES:
            try:
                rows = []
                for post in reddit.subreddit(sub).search(query, time_filter=time_filter, limit=limit):
                    body = f"[{post.title}]\n{post.selftext or ''}"
                    post.comments.replace_more(limit=0)
                    top_comments = "\n".join(c.body for c in post.comments[:10])
                    rows.append({
                        "source_url": f"https://www.reddit.com{post.permalink}",
                        "content": f"{body}\n--- top comments ---\n{top_comments}",
                    })
                total += save_mentions("reddit", rows)
            except Exception as exc:
                logger.warning("reddit search failed (r/%s, %s): %s", sub, query, exc)
    logger.info("reddit %s complete: %d new mentions", mode, total)
    return total


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run(sys.argv[1] if len(sys.argv) > 1 else "daily")
