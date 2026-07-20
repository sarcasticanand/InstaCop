"""Workstream B3: aggregate classified evidence into a per-seller Experience
profile — per-category levels + a plain-language findings summary.

Source base weights (operator decision #3): Reddit 1.0, evidenced first-party
report 1.0, follow-up answer 0.7, bare ingested mention 0.5 — each multiplied
by (1 - spam_score). Last-60-day issues count 2x.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from shared.models import BrandReview, Check, Followup, Report

from engine.issues import CATEGORIES
from engine.llm import _violates_banned_words, get_synth_llm

logger = logging.getLogger(__name__)

LEVELS = ["none", "isolated", "recurring", "severe"]
RECENT_DAYS = 60
RECENT_MULTIPLIER = 2.0

SOURCE_BASE_WEIGHT = {
    "reddit": 1.0,
    "report_evidenced": 1.0,
    "followup": 0.7,
    "report_bare": 0.5,
    "complaint_site": 0.5,
    "serpapi": 0.5,
}

# follow-up button -> implied category scores
FOLLOWUP_CATEGORY_MAP = {
    "bought_late": {"delivery": 0.8},
    "bought_bad": {"quality": 0.8},
    "never_arrived": {"fraud": 0.6, "delivery": 0.5},
}
REPORT_KIND_MAP = {
    "never_delivered": {"fraud": 0.8, "delivery": 0.5},
    "fake_product": {"as_described": 0.9, "quality": 0.5},
    "bad_quality": {"quality": 0.9},
    "late": {"delivery": 0.9},
}


def _recency_boost(ts) -> float:
    if ts is None:
        return 1.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return RECENT_MULTIPLIER if datetime.now(timezone.utc) - ts <= timedelta(days=RECENT_DAYS) else 1.0


def _collect_evidence(db: Session, seller_id: int) -> list[dict]:
    """Each item: {categories: {c: score}, weight, when, source, note}."""
    items: list[dict] = []

    for r in db.query(BrandReview).filter_by(seller_id=seller_id).all():
        if not r.issue_categories:
            continue
        weight = SOURCE_BASE_WEIGHT.get(r.source, 0.5) * (1 - (r.spam_score or 0.0))
        items.append({
            "categories": r.issue_categories,
            "weight": weight,
            "when": r.posted_at or r.fetched_at,
            "source": r.source,
            "note": (r.raw_text or "")[:200],
        })

    for rep in db.query(Report).filter(Report.seller_id == seller_id, Report.status.in_(["unreviewed", "accepted"])).all():
        base = "report_evidenced" if rep.evidence_file_id else "report_bare"
        weight = SOURCE_BASE_WEIGHT[base] * (1 - (rep.spam_score or 0.0)) * (rep.reporter_trust if rep.reporter_trust is not None else 0.6)
        categories = rep.issue_categories or REPORT_KIND_MAP.get(rep.kind, {})
        if not categories:
            continue
        items.append({
            "categories": categories,
            "weight": weight,
            "when": rep.created_at,
            "source": base,
            "note": (rep.narrative or rep.kind or "")[:200],
        })

    followups = (
        db.query(Followup)
        .join(Check, Check.id == Followup.check_id)
        .filter(Check.seller_id == seller_id, Followup.response.isnot(None))
        .all()
    )
    for fu in followups:
        categories = FOLLOWUP_CATEGORY_MAP.get(fu.response)
        if not categories:
            continue
        items.append({
            "categories": categories,
            "weight": SOURCE_BASE_WEIGHT["followup"],
            "when": fu.responded_at,
            "source": "followup",
            "note": fu.response,
        })
    return items


def _level_for(weighted_sum: float, contributor_count: int) -> str:
    if contributor_count == 0 or weighted_sum < 0.3:
        return "none"
    if weighted_sum < 1.2 or contributor_count == 1:
        return "isolated"
    # "severe" is a public accusation — it needs 3+ independent reports,
    # not just a high weighted score from one or two loud threads
    if weighted_sum < 3.0 or contributor_count < 3:
        return "recurring"
    return "severe"


def build_experience_profile(db: Session, seller_id: int) -> dict:
    """Returns {"categories": {cat: {"level","weighted_score","contributors"}},
    "has_data": bool, "positive_signals": int}."""
    items = _collect_evidence(db, seller_id)
    profile = {}
    for cat in CATEGORIES:
        weighted = 0.0
        contributors = 0
        for item in items:
            score = float(item["categories"].get(cat, 0.0) or 0.0)
            if score < 0.3:
                continue
            contributors += 1
            weighted += score * item["weight"] * _recency_boost(item["when"])
        profile[cat] = {
            "level": _level_for(weighted, contributors),
            "weighted_score": round(weighted, 2),
            "contributors": contributors,
        }
    positive = sum(
        1 for r in db.query(BrandReview).filter_by(seller_id=seller_id).all() if r.sentiment == "positive"
    )
    positive += (
        db.query(Followup)
        .join(Check, Check.id == Followup.check_id)
        .filter(Check.seller_id == seller_id, Followup.response == "bought_good")
        .count()
    )
    return {"categories": profile, "has_data": bool(items or positive), "positive_signals": positive}


SUMMARY_SYSTEM = (
    "You're telling a friend what actual buyers said about an Instagram clothing seller, so they can "
    "decide whether to buy. Use ONLY the reviewer notes given. Quote or paraphrase SPECIFIC things "
    "buyers experienced: sizing ran small, took 3 weeks to ship, fabric felt thin, print cracked after "
    "a wash, great quality for the price, ghosted on DMs, refund took ages, etc. "
    "Write plain and direct, 1-3 short sentences, like a real person, not a brand or an AI. "
    "Hard bans: verdict nouns (scammer/fraudster/con artist/criminal/thief/cheat), buy/don't-buy advice, "
    "and vague filler with no concrete detail ('frequently mentioned', 'discussed alongside other "
    "retailers', 'public inquiries focus on', 'social media discussions'). "
    "If the notes have NO concrete first-hand buyer experience — just brand name-drops or generic chatter — "
    'return an empty string. Better to say nothing than pad. Return ONLY JSON: {"summary": str}.'
)

# phrases that mark LLM filler with no real substance — reject and show nothing
_SLOP_MARKERS = (
    "frequently mentioned", "discussed alongside", "public inquir", "social media discussion",
    "focus on the quality", "no fraud reports were identified", "in the provided data",
    "is a brand that", "is a popular", "gaining popularity", "various", "overall",
)


def _looks_like_slop(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _SLOP_MARKERS)


def synthesize_findings_summary(profile: dict, evidence_notes: list[str]) -> tuple[str, float]:
    active = {c: v for c, v in profile["categories"].items() if v["level"] != "none"}
    if not profile["has_data"]:
        return "", 0.0
    if not active and profile["positive_signals"]:
        return "buyers who've ordered seem happy, no recurring issues so far.", 0.0

    payload = json.dumps({"levels": active, "positive_signals": profile["positive_signals"], "example_notes": evidence_notes[:6]})
    try:
        raw, cost = get_synth_llm().complete(SUMMARY_SYSTEM, payload, max_tokens=300, json_mode=True)
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        summary = (json.loads(m.group(0)).get("summary") or "").strip() if m else ""
    except Exception as exc:
        logger.warning("findings summary synthesis failed: %s", exc)
        summary, cost = "", 0.0

    # drop generic/AI-slop summaries and banned-word summaries entirely
    if summary and (_violates_banned_words([summary]) or _looks_like_slop(summary)):
        summary = ""

    if not summary and active:
        # concrete fallback straight from the graded categories — no fluff
        label = {
            "fraud": "never-delivered reports", "quality": "quality complaints",
            "delivery": "slow/late delivery", "as_described": "not-as-described complaints",
            "responsiveness": "poor customer service",
        }
        bits = [f"{label.get(c, c.replace('_', ' '))} ({v['contributors']})" for c, v in active.items()]
        summary = "buyers flagged: " + ", ".join(bits)
    return summary, cost


def experience_for_seller(db: Session, seller_id: int) -> tuple[dict, float]:
    """Full pipeline: classify pending reviews -> aggregate -> summarize.
    Returns (experience dict for card/page/API, total_cost)."""
    from engine.issues import classify_brand_reviews

    cost = classify_brand_reviews(db, seller_id)
    profile = build_experience_profile(db, seller_id)
    notes = [i["note"] for i in _collect_evidence(db, seller_id) if i.get("note")]
    summary, synth_cost = synthesize_findings_summary(profile, notes)

    # Receipts: the threads that carried actual issue signal, so the card can
    # link to them — every graded claim must be checkable by the reader.
    sources = [
        r.source_url
        for r in db.query(BrandReview)
        .filter(BrandReview.seller_id == seller_id, BrandReview.source_url.isnot(None))
        .order_by(BrandReview.fetched_at.desc())
        .limit(10)
        if r.issue_categories and any((v or 0) >= 0.3 for v in r.issue_categories.values())
    ][:3]

    return {
        "categories": {c: v["level"] for c, v in profile["categories"].items()},
        "detail": profile["categories"],
        "has_data": profile["has_data"],
        "positive_signals": profile["positive_signals"],
        "summary": summary,
        "sources": sources,
    }, cost + synth_cost
