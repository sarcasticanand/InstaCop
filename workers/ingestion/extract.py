"""Shared extraction job (spec 5): pulls structured entities out of unprocessed
raw_mentions with the cheap LLM and upserts sellers / payment identities /
ingestion-sourced reports. Usage: python -m workers.ingestion.extract [batch]"""

import json
import logging
import re
import sys
import time

from shared.db import SessionLocal
from shared.models import PaymentIdentity, RawMention, Report, Seller, SellerPaymentLink

from engine.llm import get_extract_llm

logger = logging.getLogger(__name__)

EXTRACT_SYSTEM = (
    "You extract structured entities from social posts about Instagram sellers in India. "
    "Return ONLY JSON: {\"ig_handles\": [str], \"upi_ids\": [str], \"phones\": [str], "
    '"narrative_kind": "never_delivered"|"fake_product"|"bad_quality"|"late"|"positive"|"unrelated"}. '
    "ig_handles: instagram usernames explicitly named as SELLERS (not the poster, not friends). "
    "Lowercase them, no @. upi_ids: full UPI ids like name@bank. phones: 10-digit Indian numbers. "
    "If the text isn't about buying from an Instagram seller, narrative_kind=unrelated with empty lists. Do not guess."
)

HANDLE_RE = re.compile(r"^[a-z0-9._]{2,30}$")
COMPLAINT_KINDS = {"never_delivered", "fake_product", "bad_quality", "late"}


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def _get_or_create_seller(db, handle: str) -> Seller:
    seller = db.query(Seller).filter(Seller.ig_handle == handle).one_or_none()
    if seller is None:
        seller = Seller(ig_handle=handle)
        db.add(seller)
        db.flush()
    return seller


def _get_or_create_identity(db, kind: str, value: str) -> PaymentIdentity:
    pi = db.query(PaymentIdentity).filter_by(kind=kind, value=value).one_or_none()
    if pi is None:
        pi = PaymentIdentity(kind=kind, value=value)
        db.add(pi)
        db.flush()
    return pi


def process_batch(batch_size: int = 50) -> tuple[int, int]:
    """Returns (mentions_processed, entities_extracted)."""
    llm = get_extract_llm()
    db = SessionLocal()
    entities = 0
    try:
        mentions = db.query(RawMention).filter(RawMention.processed.is_(False)).limit(batch_size).all()
        for mention in mentions:
            try:
                text, _cost = llm.complete(EXTRACT_SYSTEM, mention.content[:6000], max_tokens=400, json_mode=True)
                data = _parse(text)
            except Exception as exc:
                if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
                    logger.info("rate limited; sleeping 45s then retrying mention %s", mention.id)
                    time.sleep(45)
                    try:
                        text, _cost = llm.complete(EXTRACT_SYSTEM, mention.content[:6000], max_tokens=400, json_mode=True)
                        data = _parse(text)
                    except Exception as exc2:
                        logger.warning("extraction failed for mention %s after retry: %s", mention.id, exc2)
                        continue
                else:
                    logger.warning("extraction failed for mention %s: %s", mention.id, exc)
                    continue

            handles = [h.lower().lstrip("@") for h in data.get("ig_handles") or [] if HANDLE_RE.match(h.lower().lstrip("@"))]
            kind = data.get("narrative_kind") or "unrelated"

            sellers = [_get_or_create_seller(db, h) for h in handles]
            identities = []
            for upi in data.get("upi_ids") or []:
                if "@" in upi:
                    identities.append(_get_or_create_identity(db, "upi", upi.lower().strip()))
            for phone in data.get("phones") or []:
                digits = re.sub(r"\D", "", phone)[-10:]
                if len(digits) == 10:
                    identities.append(_get_or_create_identity(db, "phone", digits))

            for seller in sellers:
                for pi in identities:
                    if not db.query(SellerPaymentLink).filter_by(seller_id=seller.id, payment_identity_id=pi.id).first():
                        db.add(SellerPaymentLink(seller_id=seller.id, payment_identity_id=pi.id, source="ingestion"))
                if kind in COMPLAINT_KINDS:
                    db.add(
                        Report(
                            seller_id=seller.id,
                            reporter_chat_id=None,
                            kind=kind,
                            narrative=f"[ingestion:{mention.source}] {mention.source_url or ''}".strip(),
                            payment_identity_id=identities[0].id if identities else None,
                            status="unreviewed",
                        )
                    )
                entities += 1

            mention.processed = True
            db.commit()
        return len(mentions), entities
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    batch = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    processed, extracted = process_batch(batch)
    print(f"processed {processed} mentions, extracted entities for {extracted} seller-mentions")
