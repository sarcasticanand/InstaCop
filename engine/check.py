import sys
from datetime import datetime, timezone

from shared.db import SessionLocal
from shared.models import Check, RiskSnapshot, Seller
from shared.schemas import RiskCard, SignalResult

from engine.cost import CostLedger
from engine.ig_provider import IGProviderError, get_provider
from engine.llm import synthesize_card
from engine.render import render_card
from engine.scoring import compute_band
from engine.signals import (
    comment_red_flags,
    no_customer_tags,
    offplatform_funnel,
    payment_identity_history,
    prior_fraud_mentions,
    purchased_followers,
    stolen_photos,
    suspicious_website,
    young_account,
)
from engine.signals.base import unavailable

PROFILE_SIGNALS = [young_account, stolen_photos, no_customer_tags, comment_red_flags, suspicious_website, purchased_followers, offplatform_funnel]
DB_SIGNALS = [prior_fraud_mentions, payment_identity_history]
PROFILE_SIGNAL_NUMBERS = {
    young_account: 1,
    stolen_photos: 2,
    no_customer_tags: 3,
    comment_red_flags: 4,
    suspicious_website: 5,
    purchased_followers: 7,
    offplatform_funnel: 8,
}



def _get_or_create_seller(db, handle: str, ig_user_id: str | None) -> Seller:
    seller = db.query(Seller).filter(Seller.ig_handle == handle).one_or_none()
    if seller is None:
        seller = Seller(ig_handle=handle, ig_user_id=ig_user_id)
        db.add(seller)
        db.flush()
    elif ig_user_id and not seller.ig_user_id:
        seller.ig_user_id = ig_user_id
    seller.last_checked_at = datetime.now(timezone.utc)
    return seller


def run_check(handle: str, on_profile=None, requested_by: str = "cli") -> RiskCard:
    """Runs the full check AND records the checks row (every path must produce
    one: follow-ups, cost logging, and the outcome dataset depend on it).

    on_profile: optional callback invoked with the IGProfile as soon as the
    profile fetch lands — lets callers (the bot worker) push an early signal to
    the user while the slower image/LLM signals still run."""
    handle = handle.lower().lstrip("@")
    ledger = CostLedger()
    db = SessionLocal()

    try:
        provider = get_provider()
        try:
            profile, fetch_cost = provider.fetch_profile(handle)
        except IGProviderError as exc:
            raise SystemExit(f"Could not fetch @{handle}: {exc}")
        ledger.add("ig_provider.fetch_profile", fetch_cost)

        if on_profile is not None:
            try:
                on_profile(profile)
            except Exception:
                pass

        seller = _get_or_create_seller(db, handle, profile.ig_user_id)
        db.commit()

        signals: list[SignalResult] = []
        for module in PROFILE_SIGNALS:
            if ledger.over_cap():
                signals.append(unavailable(PROFILE_SIGNAL_NUMBERS[module], "skipped: cost cap reached"))
                continue
            result, cost = module.compute(profile)
            ledger.add(module.__name__, cost)
            signals.append(result)

        for module in DB_SIGNALS:
            result, cost = module.compute(seller.id, db)
            ledger.add(module.__name__, cost)
            signals.append(result)

        # Workstream A: read-first brand-review cache (refreshes only if stale
        # and budget allows). Workstream B: graded experience profile on top.
        experience = None
        try:
            from workers.ingestion.reddit_brand import ensure_brand_reviews

            _reviews, reddit_cost = ensure_brand_reviews(db, seller, budget_ok=not ledger.over_cap())
            ledger.add("reddit_brand_cache", reddit_cost)

            from engine.experience import experience_for_seller

            experience, exp_cost = experience_for_seller(db, seller.id)
            ledger.add("experience_profile", exp_cost)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning("experience pipeline failed for %s: %s", handle, exc)

        patterns_matched, patterns_total, weighted_score, risk_band = compute_band(signals)

        signals_json = [s.model_dump(mode="json") for s in signals]
        synthesis, synth_cost = synthesize_card(handle, patterns_matched, patterns_total, signals_json)
        ledger.add("llm.synthesize_card", synth_cost)
        evidence_lines = synthesis.get("evidence_lines") or [s.evidence for s in signals if s.matched]

        card_text = render_card(handle, risk_band, patterns_matched, patterns_total, evidence_lines, experience)

        snapshot = RiskSnapshot(
            seller_id=seller.id,
            signals={
                "signals": signals_json,
                "cost_inr": ledger.total_inr,
                "weighted_score": weighted_score,
                "card_text": card_text,
                "experience": experience,
            },
            patterns_matched=patterns_matched,
            patterns_total=patterns_total,
            risk_band=risk_band,
        )
        db.add(snapshot)

        check = Check(
            seller_id=seller.id,
            requested_by_chat_id=requested_by,
            served_from_cache=False,
            result={"cost_inr": ledger.total_inr, "risk_band": risk_band},
        )
        db.add(check)
        db.commit()

        return RiskCard(
            handle=handle,
            risk_band=risk_band,
            patterns_matched=patterns_matched,
            patterns_total=patterns_total,
            signals=signals,
            card_text=card_text,
            cost_inr=ledger.total_inr,
            check_id=check.id,
        )
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m engine.check <handle>")
        sys.exit(1)

    card = run_check(sys.argv[1])
    print(card.card_text)
    print(f"\n— cost: ₹{card.cost_inr:.2f}")
