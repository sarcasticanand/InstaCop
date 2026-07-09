"""Verified-seller onboarding (spec Phase 6).

Flow: seller applies -> operator approves in admin -> Razorpay subscription
created (test mode until keys are live) -> verified flips on. Auto-revoke: >=2
accepted reports in 30 days flips the seller back publicly regardless of
payment (spec: verification is revocable).
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from shared.config import settings
from shared.db import get_db
from shared.models import Report, Seller, Verification

logger = logging.getLogger(__name__)

router = APIRouter()

PLANS = {"basic": 29900, "featured": 99900}  # paise/month


class VerificationApplication(BaseModel):
    ig_handle: str = Field(pattern=r"^[a-zA-Z0-9._]{1,30}$")
    legal_name: str = Field(min_length=3, max_length=200)
    gstin: str = Field(pattern=r"^\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z\d][A-Z]$")
    pan_last4: str = Field(pattern=r"^\d{4}$")
    contact_email: EmailStr
    plan: str = "basic"


@router.post("/api/verify/apply")
def apply(application: VerificationApplication, db: Session = Depends(get_db)):
    handle = application.ig_handle.lower()
    seller = db.query(Seller).filter(Seller.ig_handle == handle).one_or_none()
    if seller is None:
        seller = Seller(ig_handle=handle)
        db.add(seller)
        db.flush()
    if seller.verification_status in ("pending", "active"):
        raise HTTPException(409, f"application already {seller.verification_status}")

    if application.plan not in PLANS:
        raise HTTPException(422, "plan must be basic or featured")

    db.add(
        Verification(
            seller_id=seller.id,
            legal_name=application.legal_name,
            gstin=application.gstin,
            pan_last4=application.pan_last4,
            kyc_doc_refs={"contact_email": application.contact_email, "plan": application.plan},
        )
    )
    seller.verification_status = "pending"
    db.commit()
    return {"status": "pending", "message": "Application received; we'll verify KYC and get back to you."}


def _razorpay_create_subscription(plan: str) -> str | None:
    """Returns a razorpay subscription id, or None when keys aren't configured
    (dev / pre-revenue) — the flow still completes so it can be tested end to end."""
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        logger.warning("Razorpay keys unset; skipping real subscription creation")
        return None
    try:
        resp = httpx.post(
            "https://api.razorpay.com/v1/subscriptions",
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
            json={
                # NOTE: needs a pre-created plan in the Razorpay dashboard;
                # store its id in kyc flow config when going live.
                "plan_id": f"plan_{plan}",
                "total_count": 12,
            },
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("id")
    except Exception as exc:
        logger.error("razorpay subscription failed: %s", exc)
        raise HTTPException(502, "payment provider error")


from .main import require_admin  # noqa: E402  (shared admin guard)


@router.post("/admin/verify/{handle}/approve", dependencies=[Depends(require_admin)])
def approve(handle: str, db: Session = Depends(get_db)):
    seller = db.query(Seller).filter(Seller.ig_handle == handle.lower()).one_or_none()
    if seller is None or seller.verification_status != "pending":
        raise HTTPException(404, "no pending application for this handle")
    verification = (
        db.query(Verification).filter(Verification.seller_id == seller.id).order_by(Verification.id.desc()).first()
    )
    plan = (verification.kyc_doc_refs or {}).get("plan", "basic")
    subscription_id = _razorpay_create_subscription(plan)

    verification.razorpay_subscription_id = subscription_id
    verification.started_at = datetime.now(timezone.utc)
    seller.is_verified = True
    seller.verification_status = "active"
    db.commit()
    return {"status": "active", "razorpay_subscription_id": subscription_id}


@router.post("/admin/verify/{handle}/revoke", dependencies=[Depends(require_admin)])
def revoke(handle: str, reason: str = "manual", db: Session = Depends(get_db)):
    seller = db.query(Seller).filter(Seller.ig_handle == handle.lower()).one_or_none()
    if seller is None:
        raise HTTPException(404, "unknown seller")
    _revoke(db, seller, reason)
    db.commit()
    return {"status": "revoked"}


def _revoke(db: Session, seller: Seller, reason: str) -> None:
    seller.is_verified = False
    seller.verification_status = "revoked"
    verification = (
        db.query(Verification).filter(Verification.seller_id == seller.id).order_by(Verification.id.desc()).first()
    )
    if verification:
        verification.revoked_at = datetime.now(timezone.utc)
        verification.revoke_reason = reason


@router.get("/admin/reports", dependencies=[Depends(require_admin)])
def list_unreviewed_reports(
    include: str = "unreviewed",  # unreviewed|all|quarantined
    limit: int = 50,  # capped at 200 below
    db: Session = Depends(get_db),
):
    """E4 triage queue: shows reporter trust + spam score + evidence flag so
    the operator can accept/reject by the right criteria."""
    q = (
        db.query(Report, Seller.ig_handle)
        .join(Seller, Seller.id == Report.seller_id)
        .filter(Report.kind != "dispute")
    )
    if include == "unreviewed":
        q = q.filter(Report.status == "unreviewed")
    elif include == "quarantined":
        q = q.filter(Report.status == "quarantined")
    rows = q.order_by(Report.created_at.desc()).limit(min(limit, 200)).all()
    return {
        "reports": [
            {
                "id": r.id,
                "handle": h,
                "kind": r.kind,
                "narrative": r.narrative,
                "status": r.status,
                "reporter_trust": r.reporter_trust,
                "spam_score": r.spam_score,
                "has_evidence": bool(r.evidence_file_id or r.payment_identity_id),
                "issue_categories": r.issue_categories,
                "created_at": r.created_at.isoformat(),
            }
            for r, h in rows
        ]
    }


@router.post("/admin/reports/{report_id}/{decision}", dependencies=[Depends(require_admin)])
def review_report(report_id: int, decision: str, db: Session = Depends(get_db)):
    if decision not in ("accept", "reject"):
        raise HTTPException(422, "decision must be accept or reject")
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(404, "unknown report")
    report.status = "accepted" if decision == "accept" else "rejected"
    db.commit()

    # Cache policy: a newly accepted report invalidates the seller's snapshot
    # -> queue a fresh check so signals 6/9 pick it up.
    if report.status == "accepted" and report.seller_id:
        seller = db.get(Seller, report.seller_id)
        if seller:
            try:
                from bot.service import enqueue_cold_check

                if not enqueue_cold_check(seller.ig_handle, None, False):
                    logger.warning("recheck for %s skipped: global daily cap", seller.ig_handle)
            except Exception as exc:
                logger.warning("recheck enqueue failed for %s: %s", seller.ig_handle, exc)
    return {"id": report.id, "status": report.status}


class DisputeSubmission(BaseModel):
    ig_handle: str = Field(pattern=r"^[a-zA-Z0-9._]{1,30}$")
    contact_email: EmailStr
    statement: str = Field(min_length=20, max_length=4000)
    evidence_urls: list[str] = Field(default_factory=list, max_length=10)


@router.post("/api/dispute")
def submit_dispute(body: DisputeSubmission, db: Session = Depends(get_db)):
    """Free dispute path (Workstream D5): any seller can contest their rating
    without paying. Page flips to 'disputed — under review', a re-check queues,
    and the case lands in the operator queue."""
    seller = db.query(Seller).filter(Seller.ig_handle == body.ig_handle.lower()).one_or_none()
    if seller is None:
        raise HTTPException(404, "no page exists for this handle")

    open_dispute = (
        db.query(Report)
        .filter(Report.seller_id == seller.id, Report.kind == "dispute", Report.status == "disputed")
        .first()
    )
    if open_dispute:
        return {"status": "disputed", "message": "A dispute is already under review for this seller."}

    db.add(
        Report(
            seller_id=seller.id,
            reporter_chat_id=None,
            kind="dispute",
            narrative=f"[dispute] contact={body.contact_email}\n{body.statement}\nevidence: {', '.join(body.evidence_urls[:5])}",
            status="disputed",
        )
    )
    db.commit()

    try:
        from bot.service import enqueue_cold_check

        enqueue_cold_check(seller.ig_handle, None, False)
    except Exception as exc:
        logger.warning("dispute re-check enqueue failed: %s", exc)
    return {"status": "disputed", "message": "Dispute received — the page now shows 'under review' and a fresh check is queued."}


@router.get("/admin/disputes", dependencies=[Depends(require_admin)])
def list_disputes(db: Session = Depends(get_db)):
    rows = (
        db.query(Report, Seller.ig_handle)
        .join(Seller, Seller.id == Report.seller_id)
        .filter(Report.kind == "dispute", Report.status == "disputed")
        .order_by(Report.created_at.desc())
        .all()
    )
    return {"disputes": [{"id": r.id, "handle": h, "narrative": r.narrative, "created_at": r.created_at.isoformat()} for r, h in rows]}


@router.post("/admin/disputes/{report_id}/resolve", dependencies=[Depends(require_admin)])
def resolve_dispute(report_id: int, db: Session = Depends(get_db)):
    report = db.get(Report, report_id)
    if report is None or report.kind != "dispute":
        raise HTTPException(404, "unknown dispute")
    report.status = "rejected"  # dispute closed; page banner clears
    db.commit()
    return {"id": report.id, "status": "resolved"}


class OperatorLinkOverride(BaseModel):
    handle_a: str
    handle_b: str
    decision: str  # 'linked' | 'not_linked'


@router.post("/admin/operator-links", dependencies=[Depends(require_admin)])
def override_operator_link(body: OperatorLinkOverride, db: Session = Depends(get_db)):
    """Operator confirms or breaks a same-operator link (Workstream C3)."""
    if body.decision not in ("linked", "not_linked"):
        raise HTTPException(422, "decision must be 'linked' or 'not_linked'")
    from engine.operator_graph import record_link
    from shared.models import OperatorLink

    a = db.query(Seller).filter(Seller.ig_handle == body.handle_a.lower()).one_or_none()
    b = db.query(Seller).filter(Seller.ig_handle == body.handle_b.lower()).one_or_none()
    if a is None or b is None:
        raise HTTPException(404, "unknown seller handle(s)")
    sa_, sb_ = (a.id, b.id) if a.id < b.id else (b.id, a.id)

    links = db.query(OperatorLink).filter_by(seller_a=sa_, seller_b=sb_).all()
    if not links:
        link = record_link(db, body.handle_a, body.handle_b, basis="manual", confidence=1.0)
        links = [link] if link else []
    for link in links:
        link.manual_override = body.decision
    db.commit()
    return {"pair": [body.handle_a, body.handle_b], "decision": body.decision, "links_updated": len(links)}


@router.get("/admin/operator-links", dependencies=[Depends(require_admin)])
def list_operator_links(db: Session = Depends(get_db)):
    from shared.models import OperatorLink

    rows = db.query(OperatorLink).order_by(OperatorLink.created_at.desc()).limit(200).all()
    handles = {s.id: s.ig_handle for s in db.query(Seller).all()}
    return {
        "links": [
            {
                "a": handles.get(l.seller_a),
                "b": handles.get(l.seller_b),
                "basis": l.basis,
                "confidence": l.confidence,
                "manual_override": l.manual_override,
            }
            for l in rows
        ]
    }


def auto_revoke_job() -> list[str]:
    """Cron: any ACTIVE verified seller with >=2 accepted reports in the last 30
    days gets revoked publicly. Returns revoked handles (operator gets notified
    by the caller). Runnable: python -c 'from api.app.verification import auto_revoke_job; print(auto_revoke_job())'"""
    from shared.db import SessionLocal

    db = SessionLocal()
    revoked = []
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        candidates = (
            db.query(Seller)
            .join(Report, Report.seller_id == Seller.id)
            .filter(
                Seller.verification_status == "active",
                Report.status == "accepted",
                Report.created_at >= cutoff,
            )
            .distinct()
            .all()
        )
        for seller in candidates:
            reports = (
                db.query(Report)
                .filter(
                    Report.seller_id == seller.id,
                    Report.status == "accepted",
                    Report.created_at >= cutoff,
                    Report.reporter_chat_id.isnot(None),  # first-party only — ingested mentions never revoke
                )
                .all()
            )
            # Plan D4 rule: >=2 accepted reports from DISTINCT reporters whose
            # combined trust >= 1.5, at least one evidenced. A two-fake-account
            # smear cannot clear this bar.
            by_reporter: dict[str, list[Report]] = {}
            for r in reports:
                by_reporter.setdefault(r.reporter_chat_id, []).append(r)
            if len(by_reporter) < 2:
                continue
            combined_trust = sum(
                max((r.reporter_trust if r.reporter_trust is not None else 0.6) for r in rs)
                for rs in by_reporter.values()
            )
            evidenced = any(r.evidence_file_id or r.payment_identity_id for r in reports)
            if combined_trust >= 1.5 and evidenced:
                _revoke(
                    db,
                    seller,
                    f"auto: {len(reports)} accepted reports from {len(by_reporter)} reporters "
                    f"(trust {combined_trust:.1f}, evidenced) in 30 days",
                )
                revoked.append(seller.ig_handle)
        db.commit()
        if revoked:
            logger.warning("auto-revoked verified sellers: %s", revoked)
        return revoked
    finally:
        db.close()
