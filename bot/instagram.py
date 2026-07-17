"""Instagram DM bot — Meta Instagram Messaging API (the official, ban-safe
surface; users DM our IG professional account, Meta POSTs the messages here).

Mirrors the Telegram bot feature-for-feature and writes to the SAME tables:
checks, follow-ups (quick replies instead of inline keyboards) and the /report
flow all land in Postgres. Conversation state for the report flow lives in
Redis (no aiogram FSM on this surface).

Chat ids are namespaced "ig:<IGSID>" so workers know which channel to answer
on (see workers.jobs.send_user).

Setup (Meta app -> Instagram > API setup with Instagram login):
  IG_DM_ACCESS_TOKEN  — access token for the connected professional account
  IG_DM_VERIFY_TOKEN  — any string; repeat it in the Meta webhook config
  IG_DM_APP_SECRET    — app secret, for X-Hub-Signature-256 verification
Webhook URL: https://<host>/instagram/webhook, subscribe to "messages".
"""

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from shared.config import settings
from shared.db import SessionLocal

from bot import service

logger = logging.getLogger(__name__)

router = APIRouter()

GRAPH_URL = "https://graph.instagram.com/v23.0/me/messages"
MAX_MSG_CHARS = 950  # IG DM hard limit is 1000; chunk long cards
STATE_TTL = 1800

START_TEXT = (
    "I check Instagram shops for known fraud patterns before you pay. "
    "Send me the shop's @handle or profile link."
)

# quick-reply titles are capped at 20 chars by Meta
REPORT_KINDS = [
    ("never_delivered", "Never delivered"),
    ("fake_product", "Fake or wrong item"),
    ("bad_quality", "Very poor quality"),
    ("late", "Months late"),
    ("other", "Other"),
]


# --- outbound ----------------------------------------------------------------

def send_instagram(igsid: str, text: str, quick_replies: list[tuple[str, str]] | None = None) -> None:
    """Send a DM (sync — callable from worker threads). quick_replies:
    [(title, payload)]. Long texts are chunked; quick replies ride the last chunk."""
    if not settings.IG_DM_ACCESS_TOKEN:
        logger.warning("IG_DM_ACCESS_TOKEN unset; would have sent to %s: %s", igsid, text[:80])
        return
    chunks = _chunks(text)
    for i, chunk in enumerate(chunks):
        message: dict = {"text": chunk}
        if quick_replies and i == len(chunks) - 1:
            message["quick_replies"] = [
                {"content_type": "text", "title": title[:20], "payload": payload[:1000]}
                for title, payload in quick_replies[:13]
            ]
        try:
            httpx.post(
                GRAPH_URL,
                params={"access_token": settings.IG_DM_ACCESS_TOKEN},
                json={"recipient": {"id": igsid}, "message": message},
                timeout=15,
            ).raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("IG send to %s failed: %s — %s", igsid, exc, exc.response.text[:300])
            return
        except Exception as exc:
            logger.error("IG send to %s failed: %s", igsid, exc)
            return


def _chunks(text: str) -> list[str]:
    if len(text) <= MAX_MSG_CHARS:
        return [text]
    out, current = [], ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > MAX_MSG_CHARS:
            out.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        out.append(current)
    return out


# --- conversation state (report flow) ----------------------------------------

def _state_key(igsid: str) -> str:
    return f"igdm:state:{igsid}"


def _get_state(igsid: str) -> dict | None:
    raw = service.get_redis().get(_state_key(igsid))
    return json.loads(raw) if raw else None


def _set_state(igsid: str, state: dict | None) -> None:
    r = service.get_redis()
    if state is None:
        r.delete(_state_key(igsid))
    else:
        r.set(_state_key(igsid), json.dumps(state), ex=STATE_TTL)


# --- webhook endpoints --------------------------------------------------------

@router.get("/instagram/webhook")
async def verify_webhook(
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_verify_token: str = Query(default="", alias="hub.verify_token"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
):
    if hub_mode == "subscribe" and settings.IG_DM_VERIFY_TOKEN and hmac.compare_digest(
        hub_verify_token, settings.IG_DM_VERIFY_TOKEN
    ):
        return PlainTextResponse(hub_challenge)
    raise HTTPException(403, "verify token mismatch")


@router.post("/instagram/webhook")
async def receive_webhook(request: Request):
    body = await request.body()
    if settings.IG_DM_APP_SECRET:
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(settings.IG_DM_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(401, "bad signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "not json")

    for entry in payload.get("entry", []):
        for event in entry.get("messaging", []):
            try:
                await asyncio.to_thread(_handle_event, event)
            except Exception:
                logger.exception("IG DM event handling failed")
    return {"ok": True}


# --- event handling (sync; runs in a thread) ----------------------------------

def _handle_event(event: dict) -> None:
    message = event.get("message") or {}
    igsid = str((event.get("sender") or {}).get("id") or "")
    if not igsid or message.get("is_echo"):
        return

    quick_reply = (message.get("quick_reply") or {}).get("payload")
    if quick_reply:
        _handle_payload(igsid, quick_reply)
        return

    attachments = message.get("attachments") or []
    images = [a for a in attachments if a.get("type") == "image" and (a.get("payload") or {}).get("url")]
    if images:
        _handle_image(igsid, images[0]["payload"]["url"])
        return

    text = (message.get("text") or "").strip()
    if text:
        _handle_text(igsid, text)


def _handle_text(igsid: str, text: str) -> None:
    state = _get_state(igsid)
    if state and state.get("state") == "awaiting_narrative":
        narrative = None if text.lower() in ("skip", "/skip") else text
        service.save_report(state["data"], f"ig:{igsid}", narrative)
        _set_state(igsid, None)
        send_instagram(igsid, "Logged. This protects the next buyer. 🙏")
        return
    if state and state.get("state") == "awaiting_screenshot":
        if text.lower() in ("skip", "/skip"):
            _set_state(igsid, {"state": "awaiting_narrative", "data": state["data"]})
            send_instagram(igsid, "One line about what happened (or reply Skip):")
        else:
            send_instagram(igsid, "Send the payment screenshot as a photo, or reply Skip.")
        return

    lowered = text.lower().lstrip("/")
    if lowered in ("start", "hi", "hello", "help"):
        send_instagram(igsid, START_TEXT)
        return
    if lowered == "report":
        _start_report(igsid)
        return

    handle = service.parse_handle(text)
    if handle is None:
        send_instagram(igsid, "Send an Instagram profile link or @handle to check it.")
        return
    _run_check(igsid, handle)


def _handle_image(igsid: str, url: str) -> None:
    state = _get_state(igsid)
    try:
        image_bytes = httpx.get(url, timeout=20).content
    except Exception as exc:
        logger.warning("IG image download failed: %s", exc)
        send_instagram(igsid, "Couldn't read that image — try again?")
        return

    if state and state.get("state") == "awaiting_screenshot":
        payment = None
        try:
            payment = service.extract_payment_identity(image_bytes)
        except Exception as exc:
            logger.warning("payment screenshot extraction failed: %s", exc)
        data = state["data"]
        data["evidence_file_id"] = url  # IG CDN URL stands in for a file id
        data["payment"] = payment
        _set_state(igsid, {"state": "awaiting_narrative", "data": data})
        note = f" (found {payment['kind']}: {payment['value']})" if payment else ""
        send_instagram(igsid, f"Got the screenshot{note}. One line about what happened (or reply Skip):")
        return

    # not mid-report: treat it as a profile screenshot to check
    send_instagram(igsid, "Reading the screenshot…")
    try:
        from engine.ig_provider import ScreenshotVisionIGProvider

        profile, _ = ScreenshotVisionIGProvider().extract_from_screenshot(image_bytes)
        handle = profile.handle if profile.handle != "unknown" else None
    except Exception as exc:
        logger.warning("screenshot handle extraction failed: %s", exc)
        handle = None
    if handle is None:
        send_instagram(igsid, "Couldn't read a handle from that screenshot — type the @handle instead.")
        return
    _run_check(igsid, handle)


def _handle_payload(igsid: str, payload: str) -> None:
    if payload.startswith("fu:"):
        _record_followup(igsid, payload)
        return
    if payload.startswith("rk:"):
        state = _get_state(igsid)
        if not state or state.get("state") != "choosing_kind":
            send_instagram(igsid, "That report expired — type 'report' to start again.")
            return
        data = state["data"]
        data["kind"] = payload.removeprefix("rk:")
        _set_state(igsid, {"state": "awaiting_screenshot", "data": data})
        send_instagram(igsid, "Send a payment screenshot if you have one (or reply Skip).")
        return
    logger.info("IG DM: unknown payload %r from %s", payload, igsid)


def _run_check(igsid: str, handle: str) -> None:
    user_id = int(igsid) if igsid.isdigit() else int(hashlib.sha256(igsid.encode()).hexdigest()[:12], 16)
    outcome = service.start_check(handle, f"ig:{igsid}", user_id)

    if outcome.kind == "rate_limited_cold":
        send_instagram(igsid, "You've used today's 5 fresh checks. Cached sellers still work — try tomorrow for new ones.")
        return
    if outcome.kind == "rate_limited_cached":
        send_instagram(igsid, "You've hit today's check limit. Try again tomorrow.")
        return
    if outcome.kind == "global_capped":
        send_instagram(igsid, "We're at today's system-wide check capacity. Cached sellers still work — try this one tomorrow.")
        return
    if outcome.kind == "queued":
        send_instagram(igsid, f"🔍 Checking @{handle} — community reviews arrive in a moment, full report in ~3 min.")
        return

    text = outcome.card_text or f"@{handle}: report unavailable, try again."
    if outcome.banner:
        text = f"{text}\n\n{outcome.banner}"
    text += "\n─ Got scammed by them? Reply: report"
    send_instagram(igsid, text)


def _start_report(igsid: str) -> None:
    from shared.models import Check, Seller

    chat_id = f"ig:{igsid}"
    db = SessionLocal()
    try:
        from engine.reporter_trust import report_rate_ok

        if not report_rate_ok(db, chat_id):
            send_instagram(igsid, "We've logged enough from you today — thank you. Try again tomorrow.")
            return
        last = (
            db.query(Seller)
            .join(Check, Check.seller_id == Seller.id)
            .filter(Check.requested_by_chat_id == chat_id)
            .order_by(Check.requested_at.desc())
            .first()
        )
    finally:
        db.close()

    if last is None:
        send_instagram(igsid, "Which seller? Check them first (send their @handle), then reply 'report'.")
        return

    _set_state(igsid, {"state": "choosing_kind", "data": {"seller_id": last.id, "handle": last.ig_handle}})
    send_instagram(
        igsid,
        f"Reporting @{last.ig_handle}. What happened?",
        quick_replies=[(label, f"rk:{kind}") for kind, label in REPORT_KINDS],
    )


def _record_followup(igsid: str, payload: str) -> None:
    from shared.models import Followup

    _, followup_id, response = payload.split(":", 2)
    db = SessionLocal()
    try:
        fu = db.get(Followup, int(followup_id))
        if fu:
            fu.response = response
            fu.responded_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()

    if response in ("never_arrived", "bought_bad"):
        send_instagram(igsid, "Sorry to hear that. Reply 'report' to log it — it protects the next buyer.")
    else:
        send_instagram(igsid, "Thanks — this helps other buyers!")
