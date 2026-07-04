from sqlalchemy.orm import Session

from shared.models import Report
from shared.schemas import SignalResult

from .base import matched, not_matched

DEFAULT_TRUST = 0.6
MATCH_THRESHOLD = 0.8  # ~one solid trusted report


def compute(seller_id: int | None, db: Session) -> tuple[SignalResult, float]:
    if seller_id is None:
        return not_matched(6, "Seller not yet in database; no prior reports to check."), 0.0

    reports = (
        db.query(Report)
        .filter(Report.seller_id == seller_id, Report.status == "accepted")
        .all()
    )
    if not reports:
        return not_matched(6, "No corroborated fraud reports on file yet.", data={"accepted_reports": 0}), 0.0

    weighted = 0.0
    first_party_evidenced = False
    for r in reports:
        trust = r.reporter_trust if r.reporter_trust is not None else DEFAULT_TRUST
        weighted += trust * (1 - (r.spam_score or 0.0))
        if r.reporter_chat_id and (r.evidence_file_id or r.payment_identity_id):
            first_party_evidenced = True

    data = {
        "accepted_reports": len(reports),
        "weighted_report_score": round(weighted, 2),
        # scoring reads this: ingestion-only corroboration caps at weight 2;
        # evidenced first-party reports carry the full weight 3 (plan D4)
        "first_party": first_party_evidenced,
    }

    if weighted >= MATCH_THRESHOLD or first_party_evidenced:
        kind = "evidenced buyer report(s)" if first_party_evidenced else "public mentions"
        return matched(
            6,
            f"{len(reports)} corroborated report(s) on file ({kind}, weighted score {weighted:.1f}).",
            data=data,
        ), 0.0
    return not_matched(
        6,
        f"{len(reports)} accepted mention(s) on file but below the corroboration threshold "
        f"(weighted {weighted:.1f} < {MATCH_THRESHOLD}).",
        data=data,
    ), 0.0
