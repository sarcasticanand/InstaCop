"""Same-operator graph (Workstream C3). Links seller accounts that behave like
one operator: shared photos, shared UPI/phone, or manual operator confirmation.
Catches the burn-and-reopen move (@trendy1 dies, @trendy_new opens with the
same photos and the same UPI) that account-age alone can't."""

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from shared.models import OperatorLink, PaymentIdentity, Report, Seller, SellerPaymentLink

logger = logging.getLogger(__name__)


def _pair(id_a: int, id_b: int) -> tuple[int, int]:
    return (id_a, id_b) if id_a < id_b else (id_b, id_a)


def record_link(db: Session, handle_a: str, handle_b: str, basis: str, confidence: float = 0.5) -> OperatorLink | None:
    a = db.query(Seller).filter(Seller.ig_handle == handle_a.lower()).one_or_none()
    b = db.query(Seller).filter(Seller.ig_handle == handle_b.lower()).one_or_none()
    if a is None or b is None or a.id == b.id:
        return None
    sa, sb = _pair(a.id, b.id)
    existing = db.query(OperatorLink).filter_by(seller_a=sa, seller_b=sb, basis=basis).one_or_none()
    if existing:
        if confidence > (existing.confidence or 0):
            existing.confidence = confidence
        return existing
    link = OperatorLink(seller_a=sa, seller_b=sb, basis=basis, confidence=confidence)
    db.add(link)
    db.flush()  # visible to later queries in this session (dedup depends on it)
    logger.info("operator link: %s <-> %s (%s, %.2f)", handle_a, handle_b, basis, confidence)
    return link


def linked_seller_ids(db: Session, seller_id: int) -> set[int]:
    """All sellers linked to this one, honoring manual overrides."""
    out = set()
    rows = db.query(OperatorLink).filter(
        (OperatorLink.seller_a == seller_id) | (OperatorLink.seller_b == seller_id)
    ).all()
    for link in rows:
        if link.manual_override == "not_linked":
            continue
        out.add(link.seller_b if link.seller_a == seller_id else link.seller_a)
    return out


def cluster_fraud_corroborated(db: Session, handle: str, partner_handles: set[str]) -> bool:
    """True when the photo-sharing cluster also shows fraud behavior:
    a shared payment identity between the pair, or accepted reports on any
    cluster member. (Shared photos alone are never enough.)"""
    seller = db.query(Seller).filter(Seller.ig_handle == handle.lower()).one_or_none()
    if seller is None:
        return False
    partners = (
        db.query(Seller).filter(Seller.ig_handle.in_([h.lower() for h in partner_handles])).all()
    )
    if not partners:
        return False
    partner_ids = [p.id for p in partners]

    # manual override wins
    for p_id in partner_ids:
        sa, sb = _pair(seller.id, p_id)
        override = (
            db.query(OperatorLink.manual_override)
            .filter(OperatorLink.seller_a == sa, OperatorLink.seller_b == sb, OperatorLink.manual_override.isnot(None))
            .first()
        )
        if override and override[0] == "not_linked":
            partner_ids.remove(p_id)
    if not partner_ids:
        return False

    # shared payment identity between this seller and any partner?
    my_identities = {
        r.payment_identity_id for r in db.query(SellerPaymentLink).filter_by(seller_id=seller.id).all()
    }
    if my_identities:
        shared = (
            db.query(SellerPaymentLink)
            .filter(
                SellerPaymentLink.seller_id.in_(partner_ids),
                SellerPaymentLink.payment_identity_id.in_(my_identities),
            )
            .first()
        )
        if shared:
            return True

    # accepted reports on any cluster member?
    accepted = (
        db.query(func.count(Report.id))
        .filter(Report.seller_id.in_(partner_ids + [seller.id]), Report.status == "accepted")
        .scalar()
    )
    return bool(accepted)


def autopopulate_from_payment_identities(db: Session) -> int:
    """Cron: link every pair of sellers sharing a payment identity. Returns
    number of links created/updated."""
    count = 0
    rows = (
        db.query(SellerPaymentLink.payment_identity_id, func.array_agg(SellerPaymentLink.seller_id))
        .group_by(SellerPaymentLink.payment_identity_id)
        .having(func.count(SellerPaymentLink.seller_id) > 1)
        .all()
    )
    for identity_id, seller_ids in rows:
        kind = db.query(PaymentIdentity.kind).filter_by(id=identity_id).scalar() or "upi"
        basis = "shared_phone" if kind == "phone" else "shared_upi"
        handles = {
            s.id: s.ig_handle for s in db.query(Seller).filter(Seller.id.in_(seller_ids)).all()
        }
        ids = sorted(handles)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if record_link(db, handles[a], handles[b], basis=basis, confidence=0.9):
                    count += 1
    db.commit()
    return count
