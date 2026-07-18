from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Float, func
from sqlalchemy.orm import Session

from shared.cache import cache_state, latest_snapshot
from shared.config import settings
from shared.db import get_db
from shared.models import Check, Followup, RawMention, Report, RiskSnapshot, Seller
from shared.observability import init_sentry

init_sentry("api")

app = FastAPI(title="InstaCop API")

_cors_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/privacy")
def privacy_policy():
    """Static privacy policy — required by Meta to switch the app Live."""
    from fastapi.responses import HTMLResponse

    return HTMLResponse(
        """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>InstaCop — Privacy Policy</title>
<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 20px;line-height:1.6;color:#222}h1{font-size:1.6em}h2{font-size:1.15em;margin-top:1.6em}</style>
</head><body>
<h1>InstaCop Privacy Policy</h1>
<p>Last updated: July 2026</p>
<p>InstaCop is a seller-safety service: you send us an Instagram shop's handle
(via Instagram DM or Telegram) and we reply with a risk report built from public
information and community reviews.</p>
<h2>What we collect</h2>
<ul>
<li><b>Messages you send us</b> — the handles you ask us to check, your replies
to follow-up questions, and reports you choose to submit.</li>
<li><b>Your messaging id</b> — the platform-scoped id (Instagram or Telegram)
needed to reply to you. We cannot see your password, email, or phone number.</li>
<li><b>Public seller information</b> — public profile data of the shops being
checked (follower counts, posts, public comments) and public community reviews
(e.g. Reddit threads).</li>
<li><b>Screenshots you send</b> — if you submit a payment screenshot with a scam
report, we extract payment identifiers (e.g. UPI id) to link scam reports about
the same operator. Screenshots are processed for this purpose only.</li>
</ul>
<h2>What we do with it</h2>
<p>We use this data solely to produce risk reports and to protect future buyers
(your report about a seller strengthens the next buyer's check). We do not sell
personal data, run ads, or share your identity with the sellers being checked.</p>
<h2>Retention &amp; deletion</h2>
<p>Check history and reports are retained to keep the fraud database useful. To
have your data deleted, message us on Instagram
(<a href="https://instagram.com/insta_cop_">@insta_cop_</a>) or email
<a href="mailto:sagaranand.001@gmail.com">sagaranand.001@gmail.com</a> and we
will remove your messages, reports, and messaging id within 30 days.</p>
<h2>Third parties</h2>
<p>We use hosting and data providers (Render, Supabase, Upstash, Meta Platforms,
HikerAPI, Google Gemini) as processors to run the service. Public seller data is
processed under legitimate-interest grounds for fraud prevention.</p>
<h2>Contact</h2>
<p>Questions: <a href="mailto:sagaranand.001@gmail.com">sagaranand.001@gmail.com</a></p>
</body></html>"""
    )


@app.get("/health/config")
def health_config(probe_admin_id: str = ""):
    """Deploy diagnostics: which commit is live and whether key env vars
    loaded. Booleans and counts only — never echoes secret values."""
    import os

    from bot.service import _is_admin

    out = {
        "git_commit": (os.environ.get("RENDER_GIT_COMMIT") or "unknown")[:8],
        "ig_provider": settings.IG_PROVIDER,
        "ig_session_set": bool(settings.IG_SESSION_ID),
        "hikerapi_token_set": bool(settings.HIKERAPI_TOKEN),
        "ig_dm_configured": bool(settings.IG_DM_ACCESS_TOKEN and settings.IG_DM_VERIFY_TOKEN),
        "admin_ids_configured": len([x for x in settings.ADMIN_USER_IDS.split(",") if x.strip()]),
        "webhook_url_set": bool(settings.WEBHOOK_URL),
    }
    if probe_admin_id:
        out["probe_is_admin"] = _is_admin(probe_admin_id)
    return out


@app.get("/health/data")
def health_data(db: Session = Depends(get_db)):
    """Data-pipeline diagnostics: is the sweep/ingestion actually landing rows?
    Counts only — no content leaves this endpoint."""
    import redis

    from shared.models import BrandReview

    out = {
        "sellers": db.query(func.count(Seller.id)).scalar(),
        "brand_reviews": db.query(func.count(BrandReview.id)).scalar(),
        "checks": db.query(func.count(Check.id)).scalar(),
        "reports": db.query(func.count(Report.id)).scalar(),
    }
    try:
        r = redis.from_url(settings.REDIS_URL)
        out["queued_jobs"] = r.llen("rq:queue:checks")
        last = r.get("sweep:last_run")
        out["last_monthly_sweep"] = (
            datetime.fromtimestamp(float(last), tz=timezone.utc).isoformat() if last else None
        )
    except Exception:
        out["queued_jobs"] = None
    return out


FOLLOWUP_LABELS = {
    "bought_good": "Bought — all good",
    "bought_late": "Bought — arrived late",
    "bought_bad": "Bought — bad quality",
    "never_arrived": "Never arrived",
}


@app.get("/api/sellers/{handle}")
def get_seller(handle: str, db: Session = Depends(get_db)):
    handle = handle.lower().lstrip("@")
    seller = db.query(Seller).filter(Seller.ig_handle == handle).one_or_none()
    if seller is None:
        raise HTTPException(404, "seller not checked yet")
    snapshot = latest_snapshot(db, seller.id)
    if snapshot is None:
        raise HTTPException(404, "no completed check yet")

    signals = (snapshot.signals or {}).get("signals", [])
    evidence = [s["evidence"] for s in signals if s.get("status") == "matched" and s.get("evidence")]

    # Customer experience: follow-up responses (real buyer outcomes)
    followups = (
        db.query(Followup.response, func.count())
        .join(Check, Check.id == Followup.check_id)
        .filter(Check.seller_id == seller.id, Followup.response.isnot(None), Followup.response != "didnt_buy")
        .group_by(Followup.response)
        .all()
    )
    review_counts = {FOLLOWUP_LABELS.get(r, r): c for r, c in followups}

    corroboration = [
        r.narrative.split("] ", 1)[-1]
        for r in db.query(Report)
        .filter(Report.seller_id == seller.id, Report.narrative.like("[ingestion%"))
        .limit(5)
        .all()
        if r.narrative and "http" in r.narrative
    ]

    from engine.publication import is_publishable

    has_evidenced = bool(
        db.query(Report.id)
        .filter(
            Report.seller_id == seller.id,
            Report.status == "accepted",
            Report.reporter_chat_id.isnot(None),
            (Report.evidence_file_id.isnot(None)) | (Report.payment_identity_id.isnot(None)),
        )
        .first()
    )

    return {
        "handle": seller.ig_handle,
        "display_name": seller.display_name,
        # E2 gate: thin algorithmic High/Caution stays private (bot-DM only).
        # The web layer must noindex + exclude from sitemap when False.
        "publishable": is_publishable(snapshot, has_evidenced),
        "risk_band": snapshot.risk_band,
        "patterns_matched": snapshot.patterns_matched,
        "patterns_total": snapshot.patterns_total,
        "evidence": evidence,
        "cache_state": cache_state(snapshot),
        "last_checked": snapshot.computed_at.isoformat(),
        "is_verified": seller.is_verified,
        "verification_status": seller.verification_status,
        "review_counts": review_counts,
        "corroboration_links": corroboration,
        "experience": (snapshot.signals or {}).get("experience"),
        "disputed": bool(
            db.query(Report.id)
            .filter(Report.seller_id == seller.id, Report.kind == "dispute", Report.status == "disputed")
            .first()
        ),
    }


@app.get("/api/sellers")
def list_sellers(limit: int = 500, db: Session = Depends(get_db)):
    """PUBLISHABLE sellers only — this feeds the sitemap, so the E2 gate
    applies here: thin algorithmic verdicts never reach an indexed URL."""
    from engine.publication import is_publishable

    sellers = (
        db.query(Seller)
        .join(RiskSnapshot, RiskSnapshot.seller_id == Seller.id)
        .distinct()
        .limit(limit)
        .all()
    )
    out = []
    for seller in sellers:
        snapshot = latest_snapshot(db, seller.id)
        if snapshot is None:
            continue
        has_evidenced = bool(
            db.query(Report.id)
            .filter(
                Report.seller_id == seller.id,
                Report.status == "accepted",
                Report.reporter_chat_id.isnot(None),
                (Report.evidence_file_id.isnot(None)) | (Report.payment_identity_id.isnot(None)),
            )
            .first()
        )
        if is_publishable(snapshot, has_evidenced):
            out.append({"handle": seller.ig_handle, "last_checked": snapshot.computed_at.isoformat()})
    return {"sellers": out}


# --- admin (Phase 6/7) -------------------------------------------------------

def require_admin(x_admin_token: str = Header(default="")):
    import hmac

    if not settings.ADMIN_TOKEN or not hmac.compare_digest(x_admin_token, settings.ADMIN_TOKEN):
        raise HTTPException(401, "bad admin token")


@app.get("/admin/metrics", dependencies=[Depends(require_admin)])
def admin_metrics(db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)

    checks_today = db.query(func.count(Check.id)).filter(Check.requested_at >= midnight).scalar()
    cached_today = (
        db.query(func.count(Check.id))
        .filter(Check.requested_at >= midnight, Check.served_from_cache.is_(True))
        .scalar()
    )
    followups_sent = db.query(func.count(Followup.id)).filter(Followup.sent_at.isnot(None)).scalar()
    followups_answered = db.query(func.count(Followup.id)).filter(Followup.responded_at.isnot(None)).scalar()
    reports_week = db.query(func.count(Report.id)).filter(Report.created_at >= week_ago).scalar()
    avg_cost = db.query(func.avg(RiskSnapshot.signals["cost_inr"].astext.cast(Float))).scalar()

    return {
        "checks_today": checks_today,
        "cache_hit_rate_today": (cached_today / checks_today) if checks_today else None,
        "followup_response_rate": (followups_answered / followups_sent) if followups_sent else None,
        "reports_last_7d": reports_week,
        "avg_cost_per_check_inr": round(avg_cost, 2) if avg_cost is not None else None,
        "sellers_total": db.query(func.count(Seller.id)).scalar(),
        "raw_mentions_unprocessed": db.query(func.count(RawMention.id)).filter(RawMention.processed.is_(False)).scalar(),
    }


from . import verification  # noqa: E402  (needs require_admin defined above)

app.include_router(verification.router)

# E3: prod bot rides on the API process in webhook mode; no-op in dev
# (long polling is bot/main.py).
try:
    from bot.webhook import mount_webhook

    mount_webhook(app)
except Exception as exc:
    import logging

    logging.getLogger(__name__).warning("webhook mount skipped: %s", exc)

# Instagram DM bot: Meta POSTs DMs to /instagram/webhook. Mounting is safe
# with the env vars unset — the verify endpoint just 403s until configured.
try:
    from bot.instagram import router as instagram_router

    app.include_router(instagram_router)
except Exception as exc:
    import logging

    logging.getLogger(__name__).warning("instagram webhook mount skipped: %s", exc)
