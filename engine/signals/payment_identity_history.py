from sqlalchemy.orm import Session

from shared.models import Report, SellerPaymentLink
from shared.schemas import SignalResult

from .base import matched, not_matched, unavailable


def compute(seller_id: int | None, db: Session) -> tuple[SignalResult, float]:
    if seller_id is None:
        return unavailable(9, "Seller not yet in database; no linked payment identities to check."), 0.0

    linked_ids = [
        row.payment_identity_id
        for row in db.query(SellerPaymentLink).filter(SellerPaymentLink.seller_id == seller_id).all()
    ]
    if not linked_ids:
        return unavailable(9, "No payment identity linked to this seller yet (none reported)."), 0.0

    count = (
        db.query(Report)
        .filter(Report.payment_identity_id.in_(linked_ids), Report.status == "accepted")
        .count()
    )
    if count >= 1:
        return matched(
            9,
            f"Linked payment identity has {count} accepted report(s) across all sellers.",
            data={"cross_handle_reports": count},
        ), 0.0
    return not_matched(9, "Linked payment identity has no reports on other handles.", data={"cross_handle_reports": 0}), 0.0
