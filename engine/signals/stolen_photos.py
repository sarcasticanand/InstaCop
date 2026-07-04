"""Signal 2 — photo theft, rebuilt around same-operator logic.

A photo appearing elsewhere is NOT theft by itself. Every Google Lens match is
bucketed:

  own        - the seller's own ecosystem (their site / their store pages /
               their other socials, identified by brand stem)   -> IGNORE
  cluster    - same photo under a DIFFERENT-stem handle that is also a seller
               in our DB -> operator-network evidence, recorded in
               operator_links; counts as theft only with fraud corroboration
  foreign    - a *named business* with a different stem owns the photo
               (branded domain / different brand's catalog)     -> THE SIGNAL
  generic    - generic marketplace or social platform, no clear owner
               (common for honest resellers)                    -> NEUTRAL

Signal matches ONLY on: >=2 images with foreign-brand matches, OR >=2 images
in a corroborated same-operator cluster. Match URLs are persisted in data for
the audit/dispute trail.
"""

import re
from urllib.parse import urlparse

from shared.schemas import IGProfile, SignalResult

from engine.identity import brand_stem, same_operator, stem_in_url
from engine.reverse_image_search import reverse_image_matches

from .base import matched, not_matched, unavailable

MAX_IMAGES = 5
MATCH_THRESHOLD = 2
# A photo often matches multiple foreign domains because Lens returns similar-
# style items (e.g. rust anarkali sets across many boutique stores). If ONE
# photo hits many different foreign brands, that pattern is style-similarity,
# not theft — bucket it as generic. Real theft = same photo on ONE foreign
# domain (the actual owner) plus resellers/socials.
FOREIGN_DOMAIN_DIVERSITY_CEILING = 3

# Platforms where a photo's presence proves nothing about ownership.
GENERIC_MARKETPLACES = {
    "amazon", "flipkart", "meesho", "ebay", "aliexpress", "dhgate", "alibaba",
    "temu", "snapdeal", "indiamart", "shopclues", "wish", "etsy", "shopsy",
    # peer-to-peer resale (photos routinely reused; no authoritative owner):
    "poshmark", "mercari", "depop", "vinted", "grailed", "carousell",
    "olx", "quikr", "vestiairecollective", "threadup", "thredup",
    # multi-brand Indian fashion retailers: established brands legitimately sell
    # here, so a match proves nothing about ownership (plan assumption 5 —
    # ambiguous leans neutral, never theft)
    "myntra", "ajio", "tatacliq", "nykaa", "limeroad", "shoppersstop",
    "lifestylestores", "pantaloons", "trends", "firstcry",
}
SOCIAL_MEDIA = {
    "youtube", "youtu", "tiktok", "facebook", "fb", "pinterest", "x", "twitter",
    "reddit", "threads", "imdb", "tumblr", "linkedin", "whatsapp", "telegram",
    "t", "snapchat", "quora", "medium",
}

_IG_HANDLE_IN_URL = re.compile(r"instagram\.com/([a-zA-Z0-9._]{1,30})")


def _domain_core(url: str) -> str:
    host = urlparse(url).hostname or ""
    parts = host.lower().removeprefix("www.").split(".")
    return parts[0] if parts else ""


def classify_match(
    seller_handle: str, link: str, known_seller_stems: dict[str, str], title: str | None = None
) -> tuple[str, str | None]:
    """Returns (bucket, linked_seller_handle_or_None).
    bucket: 'own' | 'cluster' | 'foreign' | 'generic'"""
    ig = _IG_HANDLE_IN_URL.search(link or "")
    if ig:
        other = ig.group(1).lower()
        if other in ("p", "reel", "reels", "stories", "explore"):
            return "generic", None  # post URL, owner unknown
        if same_operator(seller_handle, other):
            return "own", None
        other_stem = brand_stem(other)
        for known_handle, known_stem in known_seller_stems.items():
            if known_handle != seller_handle and known_stem == other_stem:
                return "cluster", known_handle
        return "generic", None  # unknown IG account reposting — owner unclear

    # A page whose URL *or title* carries the seller's own brand name is a page
    # about/by that brand (their site, their store page, a review/press piece,
    # an authorized-retail listing) — never theft evidence.
    if stem_in_url(seller_handle, link) or stem_in_url(seller_handle, title or ""):
        return "own", None

    core = _domain_core(link)
    if core in SOCIAL_MEDIA:
        return "generic", None
    if core in GENERIC_MARKETPLACES:
        return "generic", None

    # Branded domain with a different stem -> named business owns this photo.
    return "foreign", None


def compute(profile: IGProfile) -> tuple[SignalResult, float]:
    image_urls = [p.image_url for p in profile.recent_posts if p.image_url][:MAX_IMAGES]
    if not image_urls:
        return unavailable(2, "No product images available to reverse-search."), 0.0

    known_seller_stems = _load_known_seller_stems()

    total_cost = 0.0
    foreign_hits: list[dict] = []   # one entry per image with >=1 foreign match
    cluster_hits: list[dict] = []
    generic_count = 0
    audit: list[dict] = []          # full per-match trail for disputes
    checked = 0

    for url in image_urls:
        checked += 1
        matches, cost = reverse_image_matches(url)
        total_cost += cost

        image_foreign, image_cluster = [], []
        for m in matches:
            link = m.get("link") or ""
            if not link:
                continue
            bucket, linked_handle = classify_match(profile.handle, link, known_seller_stems, title=m.get("title"))
            audit.append({"image": url, "link": link, "source": m.get("source"), "title": m.get("title"), "bucket": bucket})
            if bucket == "foreign":
                image_foreign.append(link)
            elif bucket == "cluster":
                image_cluster.append({"link": link, "linked_seller": linked_handle})
            elif bucket == "generic":
                generic_count += 1

        # Style-similarity guard: many different foreign domains for one photo
        # means Lens returned visually-similar (not identical) results.
        # Real theft has ONE authoritative source, not a dozen.
        distinct_foreign_domains = {_domain_core(l) for l in image_foreign}
        if len(distinct_foreign_domains) >= FOREIGN_DOMAIN_DIVERSITY_CEILING:
            # reclassify all foreign matches for this image as generic
            for a in audit:
                if a["image"] == url and a["bucket"] == "foreign":
                    a["bucket"] = "generic"
                    a["reclassified"] = "style_similarity"
            generic_count += len(image_foreign)
            image_foreign = []

        if image_foreign:
            foreign_hits.append({"image": url, "links": image_foreign[:3]})
        if image_cluster:
            cluster_hits.append({"image": url, "matches": image_cluster[:3]})

        # verdict determined -> stop spending
        if len(foreign_hits) >= MATCH_THRESHOLD:
            break

    _record_cluster_links(profile.handle, cluster_hits)

    data = {
        "checked": checked,
        "total_available": len(image_urls),
        "foreign_hit_images": len(foreign_hits),
        "cluster_hit_images": len(cluster_hits),
        "generic_matches": generic_count,
        "audit": audit[:30],
    }

    if len(foreign_hits) >= MATCH_THRESHOLD:
        domains = {_domain_core(l) for h in foreign_hits for l in h["links"]}
        return matched(
            2,
            f"{len(foreign_hits)} of {checked} product photos appear to originate from other named businesses "
            f"({', '.join(sorted(domains)[:3])}).",
            data=data,
        ), total_cost

    if len(cluster_hits) >= MATCH_THRESHOLD and _cluster_has_fraud_corroboration(profile.handle, cluster_hits):
        partners = {m["linked_seller"] for h in cluster_hits for m in h["matches"] if m.get("linked_seller")}
        return matched(
            2,
            f"Product photos shared with other seller account(s) ({', '.join(sorted(partners)[:3])}) "
            "that show prior fraud-pattern activity.",
            data=data,
        ), total_cost

    note = ""
    if generic_count:
        note = f" {generic_count} matches on generic marketplaces/social platforms — common for resellers, not counted as theft."
    return not_matched(
        2,
        f"No photos traced to another named business ({checked} checked).{note}",
        data=data,
    ), total_cost


def _load_known_seller_stems() -> dict[str, str]:
    from shared.db import SessionLocal
    from shared.models import Seller

    db = SessionLocal()
    try:
        return {s.ig_handle: brand_stem(s.ig_handle) for s in db.query(Seller.ig_handle).all()}
    finally:
        db.close()


def _record_cluster_links(handle: str, cluster_hits: list[dict]) -> None:
    if not cluster_hits:
        return
    from engine.operator_graph import record_link
    from shared.db import SessionLocal

    db = SessionLocal()
    try:
        for hit in cluster_hits:
            for m in hit["matches"]:
                if m.get("linked_seller"):
                    record_link(db, handle, m["linked_seller"], basis="shared_photo", confidence=0.7)
        db.commit()
    finally:
        db.close()


def _cluster_has_fraud_corroboration(handle: str, cluster_hits: list[dict]) -> bool:
    """Cluster = theft signal only when the linked operator also shows fraud
    behavior: a shared payment identity link, or accepted reports on a partner."""
    from engine.operator_graph import cluster_fraud_corroborated
    from shared.db import SessionLocal

    partners = {m["linked_seller"] for h in cluster_hits for m in h["matches"] if m.get("linked_seller")}
    if not partners:
        return False
    db = SessionLocal()
    try:
        return cluster_fraud_corroborated(db, handle, partners)
    finally:
        db.close()
