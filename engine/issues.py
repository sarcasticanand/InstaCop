"""Workstream B: fixed issue taxonomy + per-text graded classifier.

Five categories, each scored 0..1 per text. Adding a category later = edit
CATEGORIES here; the JSONB storage needs no migration.
"""

import json
import logging
import re

from engine.llm import get_extract_llm

logger = logging.getLogger(__name__)

CATEGORIES = ["fraud", "quality", "delivery", "as_described", "responsiveness"]

CLASSIFY_SYSTEM = (
    "You classify a buyer's text about an Instagram seller into issue categories. "
    "Score each 0.0-1.0 for how strongly the text evidences that issue:\n"
    "- fraud: money taken and nothing delivered, seller disappeared, identity/photos stolen\n"
    "- quality: item cheaper/worse than shown, defective, poor material\n"
    "- delivery: late or unpredictable delivery timelines\n"
    "- as_described: wrong item, colour/size mismatch, misleading photos\n"
    "- responsiveness: ignores messages, no support after payment\n"
    "These are issue-type scores, NOT verdicts about the seller as a person; never use "
    "words like scammer/fraudster/criminal in the reason. "
    'Return ONLY JSON: {"fraud":f,"quality":f,"delivery":f,"as_described":f,'
    '"responsiveness":f,"sentiment":"positive"|"negative"|"mixed"|"neutral","reason":str}. '
    "Unrelated/empty text: all zeros, sentiment neutral."
)


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def classify_text(text: str, brand: str | None = None) -> tuple[dict, str, float]:
    """Returns (issue_categories dict 0..1 per category, sentiment, cost_inr).
    brand: when given, only issues attributed to THAT seller are scored."""
    if not (text or "").strip():
        return {c: 0.0 for c in CATEGORIES}, "neutral", 0.0
    system = CLASSIFY_SYSTEM
    if brand:
        system += (
            f"\nIMPORTANT: the seller being scored is '{brand}'. Score ONLY issues the writer "
            f"attributes to this specific seller based on described buyer experience. Complaints "
            f"about OTHER brands/sellers appearing in the same text score zero. A bare question "
            f"('is {brand} legit?') or hearsay with no experience described scores zero. "
            f"'Overpriced' alone is NOT a quality issue."
        )
    llm = get_extract_llm()
    raw, cost = llm.complete(system, text[:8000], max_tokens=300, json_mode=True)
    data = _parse(raw)
    categories = {}
    for c in CATEGORIES:
        try:
            categories[c] = max(0.0, min(1.0, float(data.get(c, 0.0))))
        except (TypeError, ValueError):
            categories[c] = 0.0
    sentiment = data.get("sentiment") if data.get("sentiment") in ("positive", "negative", "mixed", "neutral") else "neutral"
    return categories, sentiment, cost


def classify_brand_reviews(db, seller_id: int, limit: int = 25) -> float:
    """Fill issue_categories/sentiment on unclassified brand_reviews rows.
    Returns total LLM cost."""
    from datetime import datetime, timezone

    from shared.models import BrandReview, Seller

    seller = db.get(Seller, seller_id)
    brand = seller.ig_handle if seller else None

    rows = (
        db.query(BrandReview)
        .filter(BrandReview.seller_id == seller_id, BrandReview.extracted_at.is_(None))
        .limit(limit)
        .all()
    )
    total = 0.0
    for row in rows:
        try:
            categories, sentiment, cost = classify_text(row.raw_text, brand=brand)
        except Exception as exc:
            logger.warning("classification failed for brand_review %s: %s", row.id, exc)
            continue
        row.issue_categories = categories
        row.sentiment = sentiment
        row.extracted_at = datetime.now(timezone.utc)
        total += cost
    db.commit()
    return total
