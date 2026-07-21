"""Scoped conversational layer.

Answers a buyer's follow-up questions about the ONE seller they most recently
checked ("why is it risky?", "what did buyers say about sizing?", "is it safe
to pay COD?"), grounded strictly in that check's stored data. This is NOT a
general chatbot: questions about other sellers, or anything off shopping-safety,
are declined. No fact is invented — if the check data doesn't cover it, it says
so.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from shared.db import SessionLocal
from shared.models import Check, RiskSnapshot, Seller

logger = logging.getLogger(__name__)

# only answer about a seller they looked at recently — keeps context tight and
# stops old checks from being silently used as grounding
RECENT_WINDOW_HOURS = 24

SYSTEM = (
    "You are InstaCop, replying to a buyer's follow-up question about ONE Instagram seller they just "
    "checked: @{handle}. Answer ONLY from the FACTS given below — never invent followers, reviews, "
    "prices, dates, or details that aren't there.\n"
    "Rules:\n"
    "- If the facts don't answer it, say you don't have that on @{handle}. Don't guess.\n"
    "- If the question isn't about @{handle} or about shopping/scam safety, don't answer it — say you "
    "only help with checking this shop and they can send another @handle.\n"
    "- Never give a flat 'safe'/'not safe' verdict as a promise; point to what the check actually found.\n"
    "- Voice: casual and direct, like a friend who knows their stuff. 1-4 short sentences. No emoji "
    "spam, no filler, never say you're an AI."
)


def _naive_utc(ts):
    if ts is None:
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def recent_seller_context(chat_id: str) -> dict | None:
    """Facts about the requester's most recently checked seller, or None if
    they haven't checked anyone in the recent window."""
    db = SessionLocal()
    try:
        check = (
            db.query(Check)
            .filter(Check.requested_by_chat_id == chat_id)
            .order_by(Check.requested_at.desc())
            .first()
        )
        if not check or not check.seller_id:
            return None
        when = _naive_utc(check.requested_at)
        if when and when < datetime.now(timezone.utc) - timedelta(hours=RECENT_WINDOW_HOURS):
            return None
        seller = db.get(Seller, check.seller_id)
        snap = (
            db.query(RiskSnapshot)
            .filter_by(seller_id=check.seller_id)
            .order_by(RiskSnapshot.computed_at.desc())
            .first()
        )
        if not seller or not snap:
            return None
        data = snap.signals or {}
        return {
            "handle": seller.ig_handle,
            "risk_band": snap.risk_band,
            "fraud_patterns_matched": snap.patterns_matched,
            "fraud_patterns_checked": snap.patterns_total,
            "signals": [
                {"check": s.get("title"), "result": s.get("status"), "detail": s.get("evidence")}
                for s in data.get("signals", [])
                if s.get("status") != "unavailable"
            ],
            "community_reviews": data.get("experience"),
        }
    finally:
        db.close()


def answer_about_seller(chat_id: str, question: str) -> str | None:
    """A grounded reply, or None when there's no recent seller to talk about
    (caller then falls back to the generic prompt)."""
    ctx = recent_seller_context(chat_id)
    if not ctx:
        return None
    from engine.llm import get_synth_llm

    payload = json.dumps({"question": question, "facts": ctx}, default=str)[:7000]
    try:
        raw, _cost = get_synth_llm().complete(SYSTEM.format(handle=ctx["handle"]), payload, max_tokens=280)
    except Exception as exc:
        logger.warning("conversation answer failed: %s", exc)
        return None
    return (raw or "").strip() or None
