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
import re
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
    "hey, I'm InstaCop. before you buy from an Instagram shop, I check if it's "
    "legit: buyer reviews, scam reports, account red flags.\n\n"
    "send me any of these:\n"
    "• the shop's @handle or link\n"
    "• a forwarded ad, story or post\n"
    "• a screenshot of their profile\n"
    "• or their website\n\n"
    "already got scammed by a shop? reply 'report' and I'll log it to warn the next person."
)

# quick-reply titles are capped at 20 chars by Meta
REPORT_KINDS = [
    ("never_delivered", "Never delivered"),
    ("fake_product", "Fake or wrong item"),
    ("bad_quality", "Very poor quality"),
    ("late", "Months late"),
    ("other", "Other"),
]


# --- diagnostics --------------------------------------------------------------
# Redis counters surfaced by /health/data so a dead DM pipeline can be
# localized remotely: did Meta call us at all, did the signature pass, did
# our reply send? (Render free tier has no log access from outside.)

def _bump(key: str) -> None:
    try:
        service.get_redis().incr(f"igdm:stat:{key}")
    except Exception:
        pass


def _note(key: str, value: str) -> None:
    try:
        service.get_redis().set(f"igdm:stat:{key}", (value or "")[:300])
    except Exception:
        pass


# --- outbound ----------------------------------------------------------------

def send_instagram(igsid: str, text: str, quick_replies: list[tuple[str, str]] | None = None) -> None:
    """Send a DM (sync — callable from worker threads). quick_replies:
    [(title, payload)]. Long texts are chunked; quick replies ride the last chunk."""
    from shared.meta_token import get_meta_token

    token = get_meta_token()
    if not token:
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
                params={"access_token": token},
                json={"recipient": {"id": igsid}, "message": message},
                timeout=15,
            ).raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("IG send to %s failed: %s — %s", igsid, exc, exc.response.text[:300])
            _bump("send_fail")
            _note("last_send_error", exc.response.text)
            return
        except Exception as exc:
            logger.error("IG send to %s failed: %s", igsid, exc)
            _bump("send_fail")
            _note("last_send_error", str(exc))
            return
    _bump("send_ok")


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
        _bump("verify_ok")
        return PlainTextResponse(hub_challenge)
    _bump("verify_fail")
    raise HTTPException(403, "verify token mismatch")


@router.post("/instagram/webhook")
async def receive_webhook(request: Request):
    body = await request.body()
    _bump("post")
    if settings.IG_DM_APP_SECRET:
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(settings.IG_DM_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            _bump("sig_fail")
            raise HTTPException(401, "bad signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "not json")

    for entry in payload.get("entry", []):
        for event in entry.get("messaging", []):
            _bump("event")
            # Meta redelivers aggressively when we answer slowly (free-tier
            # cold starts take ~1 min) — every message must be processed at
            # most once, keyed by its mid.
            mid = (event.get("message") or {}).get("mid") or ""
            if mid:
                try:
                    if not service.get_redis().set(f"igdm:mid:{mid}", 1, nx=True, ex=86400):
                        _bump("dedup_skip")
                        continue
                except Exception:
                    pass
            # ack immediately; handling (checks, page fetches) can take far
            # longer than Meta's delivery timeout and must not block the 200
            task = asyncio.create_task(asyncio.to_thread(_safe_handle, event))
            _BG_TASKS.add(task)
            task.add_done_callback(_BG_TASKS.discard)
    return {"ok": True}


_BG_TASKS: set = set()


def _safe_handle(event: dict) -> None:
    try:
        _handle_event(event)
    except Exception:
        logger.exception("IG DM event handling failed")
        _bump("event_error")


# --- event handling (sync; runs in a thread) ----------------------------------

def _maybe_welcome(igsid: str) -> None:
    """First contact ever -> introduce what the bot can do, then continue
    processing whatever they sent."""
    try:
        if service.get_redis().set(f"igdm:seen:{igsid}", 1, nx=True):
            send_instagram(igsid, START_TEXT)
    except Exception:
        pass


def _handle_event(event: dict) -> None:
    message = event.get("message") or {}
    igsid = str((event.get("sender") or {}).get("id") or "")
    if not igsid or message.get("is_echo"):
        return

    quick_reply = (message.get("quick_reply") or {}).get("payload")
    if quick_reply:
        _handle_payload(igsid, quick_reply)
        return

    _maybe_welcome(igsid)

    attachments = [a for a in (message.get("attachments") or []) if (a.get("payload") or {}).get("url")]
    if attachments:
        _handle_attachment(igsid, attachments[0])
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
        send_instagram(igsid, "logged. this warns the next person who checks them. appreciate you")
        return
    if state and state.get("state") == "awaiting_screenshot":
        if text.lower() in ("skip", "/skip"):
            _set_state(igsid, {"state": "awaiting_narrative", "data": state["data"]})
            send_instagram(igsid, "one line on what happened? (or reply skip)")
        else:
            send_instagram(igsid, "send the payment screenshot as a photo, or reply skip")
        return

    lowered = text.lower().lstrip("/")
    if lowered in ("start", "hi", "hello", "help", "hey"):
        send_instagram(igsid, START_TEXT)
        return
    if lowered == "report":
        _start_report(igsid)
        return

    site = service.parse_website(text)
    if site:
        _run_website(igsid, site)
        return

    handle = service.parse_handle(text)
    if handle is None:
        send_instagram(igsid, "send me a shop's @handle, their link or website, or forward their ad. I'll take it from there")
        return
    _run_check(igsid, handle)


def _run_website(igsid: str, url: str) -> None:
    from urllib.parse import urlparse

    host = urlparse(url).hostname or url
    send_instagram(igsid, f"looking into {host}, give me a sec")
    try:
        from engine.website import check_website

        handle, reply, _cost = check_website(url)
    except Exception as exc:
        logger.warning("website check failed for %s: %s", url, exc)
        send_instagram(igsid, "couldn't load that site. if the shop has an Instagram, send me the @handle")
        return
    if reply:
        send_instagram(igsid, reply)
    if handle:
        _run_check(igsid, handle)


# instagram.com/stories/<owner>/<id> puts the owner right in the path; the
# /p|reel|tv/ forms put it before the media segment.
_PERMALINK_OWNER_RE = re.compile(r"instagram\.com/(?:stories/)?([a-z0-9._]{2,30})/(?:p/|reel/|tv/|\d)", re.I)
_PERMALINK_RE = re.compile(r"instagram\.com/(?:p|reel|tv|stories)/", re.I)

AD_EXTRACT_PROMPT = (
    "This is an Instagram ad, story, post, reel or profile a user forwarded — it may be an image "
    "or a short video. Watch/read all of it and identify the seller/brand it belongs to. The @username "
    "usually shows as the story header, an @mention sticker, a tag, or on-screen text. Return ONLY JSON: "
    '{"handle": str|null, "brand_name": str|null}. '
    "handle only if an @username or profile name is actually visible — do not guess."
)

# Gemini inline media rides in the request body; keep well under the ~20MB
# request cap. Shared stories/reels (<=60s) are almost always a few MB.
_INLINE_MEDIA_CAP = 18 * 1024 * 1024


def _brand_from_permalink(url: str) -> str | None:
    """Shared posts/reels arrive with an instagram.com permalink. Owner is in
    the URL for the /<user>/p/<code> form; otherwise resolve the media via
    HikerAPI (1 paid request)."""
    m = _PERMALINK_OWNER_RE.search(url or "")
    if m:
        return m.group(1).lower()
    if not _PERMALINK_RE.search(url or ""):
        return None
    try:
        from engine.ig_provider import HikerAPIIGProvider

        media = HikerAPIIGProvider()._get("/v1/media/by/url", url=url)
        username = ((media or {}).get("user") or {}).get("username")
        return username.lower() if username else None
    except Exception as exc:
        logger.warning("shared-post owner lookup failed for %s: %s", url, exc)
        return None


def _brand_from_media(media_bytes: bytes, mime_type: str) -> tuple[str | None, str | None]:
    """(handle, brand_name) extracted from a forwarded image OR video via
    Gemini vision, which reads video frames natively."""
    import re as _re

    from engine.llm import get_pii_llm

    try:
        text, _ = get_pii_llm().complete_vision(AD_EXTRACT_PROMPT, media_bytes, mime_type, max_tokens=200)
        m = _re.search(r"\{.*\}", text or "", _re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
    except Exception as exc:
        logger.warning("ad media extraction failed (%s): %s", mime_type, exc)
        return None, None
    handle = (data.get("handle") or "").lstrip("@").strip().lower() or None
    return handle, data.get("brand_name")


def _handle_attachment(igsid: str, attachment: dict) -> None:
    """Anything forwarded into the chat: shared post/reel/story, ad, or a
    plain image. Goal is always the same — figure out WHICH seller this is
    and run the check."""
    payload = attachment.get("payload") or {}
    url = payload.get("url") or ""
    state = _get_state(igsid)

    # shared post/reel with a permalink → owner is knowable without vision
    handle = _brand_from_permalink(url) or _brand_from_permalink(payload.get("title") or "")
    if handle:
        send_instagram(igsid, f"that's @{handle}, checking them now")
        _run_check(igsid, handle)
        return

    try:
        resp = httpx.get(url, timeout=30)
        content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        media_bytes = resp.content
    except Exception as exc:
        logger.warning("IG attachment download failed: %s", exc)
        send_instagram(igsid, "couldn't open that. try again, or just type the shop's @handle")
        return

    att_type = attachment.get("type") or ""
    is_image = "image" in content_type or att_type in ("image", "story_mention")
    is_video = "video" in content_type or att_type in ("video", "ig_reel", "reel", "story")
    if not content_type and not (is_image or is_video):
        # no content-type header and untyped: assume image (most shares are)
        is_image = True

    # mid-report: an image here is the payment screenshot
    if state and state.get("state") == "awaiting_screenshot" and is_image:
        payment = None
        try:
            payment = service.extract_payment_identity(media_bytes)
        except Exception as exc:
            logger.warning("payment screenshot extraction failed: %s", exc)
        data = state["data"]
        data["evidence_file_id"] = url  # IG CDN URL stands in for a file id
        data["payment"] = payment
        _set_state(igsid, {"state": "awaiting_narrative", "data": data})
        note = f" (found {payment['kind']}: {payment['value']})" if payment else ""
        send_instagram(igsid, f"got the screenshot{note}. one line on what happened? (or reply skip)")
        return

    if not (is_image or is_video):
        send_instagram(igsid, "couldn't read that. type the shop's @handle and I'll check them")
        return
    if is_video and len(media_bytes) > _INLINE_MEDIA_CAP:
        send_instagram(igsid, "that clip's too big for me to read. type the shop's @handle and I'll run it")
        return

    mime = content_type or ("video/mp4" if is_video else "image/jpeg")
    send_instagram(igsid, "reading that, one sec" if is_image else "watching that clip, give me a sec")
    handle, brand_name = _brand_from_media(media_bytes, mime)
    if handle:
        _run_check(igsid, handle)
        return
    if brand_name:
        send_instagram(
            igsid,
            f"looks like “{brand_name}” but I can't see their exact @handle in this. "
            "type it out and I'll run the full check",
        )
        return
    send_instagram(igsid, "couldn't tell which shop that is. type their @handle and I'll check them")


def _handle_payload(igsid: str, payload: str) -> None:
    if payload.startswith("fu:"):
        _record_followup(igsid, payload)
        return
    if payload.startswith("rk:"):
        state = _get_state(igsid)
        if not state or state.get("state") != "choosing_kind":
            send_instagram(igsid, "that report timed out. reply 'report' to start over")
            return
        data = state["data"]
        data["kind"] = payload.removeprefix("rk:")
        _set_state(igsid, {"state": "awaiting_screenshot", "data": data})
        send_instagram(igsid, "got a payment screenshot? send it over, it makes the report way stronger (or reply skip)")
        return
    logger.info("IG DM: unknown payload %r from %s", payload, igsid)


def _run_check(igsid: str, handle: str) -> None:
    user_id = int(igsid) if igsid.isdigit() else int(hashlib.sha256(igsid.encode()).hexdigest()[:12], 16)
    outcome = service.start_check(handle, f"ig:{igsid}", user_id)

    if outcome.kind == "rate_limited_cold":
        send_instagram(igsid, "you've used today's 5 fresh checks. shops we've already checked still work, new ones reset tomorrow")
        return
    if outcome.kind == "rate_limited_cached":
        send_instagram(igsid, "you've hit today's limit, resets tomorrow")
        return
    if outcome.kind == "global_capped":
        send_instagram(igsid, "we're at capacity today. already-checked shops still work, try this one tomorrow")
        return
    if outcome.kind == "queued":
        send_instagram(igsid, f"on it, checking @{handle}. what buyers say lands in a few seconds, the full account scan takes a couple of minutes")
        return

    text = outcome.card_text or f"couldn't pull a report for @{handle} right now, try again in a bit"
    if outcome.banner:
        text = f"{text}\n\n{outcome.banner}"
    text += "\ngot scammed by them? reply 'report'"
    send_instagram(igsid, text)


def _start_report(igsid: str) -> None:
    from shared.models import Check, Seller

    chat_id = f"ig:{igsid}"
    db = SessionLocal()
    try:
        from engine.reporter_trust import report_rate_ok

        if not report_rate_ok(db, chat_id):
            send_instagram(igsid, "we've logged enough from you today, thanks. try again tomorrow")
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
        send_instagram(igsid, "which shop? check them first (send their @handle), then reply 'report'")
        return

    _set_state(igsid, {"state": "choosing_kind", "data": {"seller_id": last.id, "handle": last.ig_handle}})
    send_instagram(
        igsid,
        f"reporting @{last.ig_handle}. what happened?",
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
        send_instagram(igsid, "sorry that happened. reply 'report' and I'll log it, it warns the next buyer")
    else:
        send_instagram(igsid, "noted, thanks. this genuinely helps other buyers")
