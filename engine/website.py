"""User pasted a store website instead of an Instagram handle.

Step 1: find the shop's Instagram link on the page — nearly every seller site
links its IG in the header/footer. Found -> hand back to the normal seller
check (full report, everything logged).

Step 2 (no IG link on the page): website-only mini-check from what we can
verify without the account: domain age (the classic scam tell), HTTPS, and a
brand-name match against the community-review database.
"""

import logging
import re
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

IG_LINK_RE = re.compile(r"instagram\.com/([a-zA-Z0-9._]{2,30})")
# path segments that appear after instagram.com/ but aren't profiles
NOT_HANDLES = {
    "p", "reel", "reels", "tv", "stories", "explore", "accounts", "share",
    "sharer", "about", "legal", "directory", "developer", "web", "invites",
    "s", "oauth", "static", "embed",
}
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


def fetch_page(url: str) -> str:
    try:
        resp = httpx.get(url, timeout=12, follow_redirects=True, headers=UA)
        return resp.text[:800_000]
    except Exception as exc:
        logger.info("website fetch failed for %s: %s", url, exc)
        return ""


def find_instagram_handle(html: str) -> str | None:
    """Most-linked plausible IG handle on the page, or None."""
    counts: dict[str, int] = {}
    for h in IG_LINK_RE.findall(html or ""):
        h = h.lower().rstrip(".")
        if h in NOT_HANDLES or len(h) < 2:
            continue
        counts[h] = counts.get(h, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _matching_seller(domain: str) -> tuple[str | None, int]:
    """Community-review match by brand stem: ('bluorng', 7) if the domain's
    name lines up with a seller we already have reviews for."""
    from sqlalchemy import func

    from shared.db import SessionLocal
    from shared.models import BrandReview, Seller

    from engine.identity import brand_stem

    label = (domain or "").removeprefix("www.").split(".")[0]
    stem = brand_stem(label)
    if len(stem) < 4:
        return None, 0
    db = SessionLocal()
    try:
        row = (
            db.query(Seller.ig_handle, func.count(BrandReview.id).label("n"))
            .outerjoin(BrandReview, BrandReview.seller_id == Seller.id)
            .filter(Seller.ig_handle.ilike(f"%{stem}%"))
            .group_by(Seller.ig_handle)
            .order_by(func.count(BrandReview.id).desc())
            .first()
        )
        return (row[0], row[1]) if row else (None, 0)
    finally:
        db.close()


def check_website(url: str) -> tuple[str | None, str | None, float]:
    """Returns (ig_handle, reply_text, cost_inr).

    ig_handle set  -> the site links an Instagram; caller runs the normal
                      check (reply_text announces that).
    ig_handle None -> reply_text is the standalone mini-report.
    """
    from engine.whois_lookup import domain_age_days

    host = (urlparse(url).hostname or "").lower()
    html = fetch_page(url)

    handle = find_instagram_handle(html)
    if handle:
        return handle, f"that site links to @{handle} on Instagram — running the full check on them now", 0.0

    age_days, cost = domain_age_days(host)

    lines = [f"couldn't find an Instagram link on {host}, so here's what I can verify about the site itself:"]

    if age_days is not None and age_days < 60:
        lines.append(
            f"• the domain was registered {age_days} days ago. brand-new store domains "
            "are the single biggest scam tell — most fake stores are under 2 months old"
        )
    elif age_days is not None and age_days < 365:
        lines.append(f"• the domain is about {age_days // 30} months old — not damning, but young")
    elif age_days is not None:
        years = age_days // 365
        lines.append(f"• the domain has been around ~{years} year{'s' if years > 1 else ''}, which is a decent sign")
    else:
        lines.append("• couldn't verify the domain's age")

    if not url.startswith("https"):
        lines.append("• no https — do not enter card details there, full stop")

    if not html:
        lines.append("• the site didn't load for me just now, which is worth noting on its own")

    match_handle, review_count = _matching_seller(host)
    if match_handle and review_count > 0:
        lines.append(
            f"• the name matches @{match_handle} in our review database "
            f"({review_count} community review{'s' if review_count > 1 else ''}) — "
            f"send me @{match_handle} and I'll pull the full report"
        )
    else:
        lines.append("• no community reviews on file for this name yet")

    lines.append("if the shop has an Instagram, send me the @handle — the account tells me a lot more than the site does")
    return None, "\n".join(lines), cost
