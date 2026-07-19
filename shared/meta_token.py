"""Meta (Instagram-login) access token lifecycle.

The dashboard-generated token in IG_DM_ACCESS_TOKEN is long-lived but expires
in ~60 days. The scheduler refreshes it weekly via the official
refresh_access_token endpoint and keeps the newest token in Redis, so the env
var never needs manual rotation and a redeploy is never required.

Every consumer (DM sends, Business Discovery) must read the token through
get_meta_token(), never settings directly.
"""

import logging
import time

import httpx
import redis

from shared.config import settings

logger = logging.getLogger(__name__)

KEY = "igdm:access_token"
STAMP = "igdm:access_token_refreshed_at"
REFRESH_EVERY = 7 * 86400
RETRY_AFTER_FAILURE = 86400


def _r():
    return redis.from_url(settings.REDIS_URL)


def get_meta_token() -> str:
    """Freshest known token: Redis (refreshed) first, env var as the seed."""
    try:
        cached = _r().get(KEY)
        if cached:
            return cached.decode()
    except Exception:
        pass
    return settings.IG_DM_ACCESS_TOKEN


def maybe_refresh_meta_token() -> bool:
    """Weekly refresh; safe to call every scheduler pass. Returns True when a
    new token was stored. Tokens must be >24h old to refresh, so the weekly
    cadence stays well inside both bounds (24h min age, 60d expiry)."""
    if not settings.IG_DM_ACCESS_TOKEN:
        return False
    try:
        r = _r()
        last = float(r.get(STAMP) or 0)
    except Exception as exc:
        logger.warning("meta token refresh skipped, redis unavailable: %s", exc)
        return False
    if time.time() - last < REFRESH_EVERY:
        return False

    try:
        resp = httpx.get(
            "https://graph.instagram.com/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": get_meta_token()},
            timeout=30,
        )
        data = resp.json()
        new_token = data.get("access_token")
        if resp.status_code == 200 and new_token:
            r.set(KEY, new_token)
            r.set(STAMP, time.time())
            logger.info("meta access token refreshed (expires_in=%s)", data.get("expires_in"))
            return True
        logger.warning("meta token refresh rejected: %s", str(data)[:200])
    except Exception as exc:
        logger.warning("meta token refresh error: %s", exc)

    # back off a day, not a week, so a transient failure can't stack up
    # against the 60-day expiry
    r.set(STAMP, time.time() - REFRESH_EVERY + RETRY_AFTER_FAILURE)
    return False
