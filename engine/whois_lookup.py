import logging
from datetime import datetime, timezone

import httpx
import whois

from shared.config import settings

logger = logging.getLogger(__name__)

WHOISXML_COST_INR = 0.15  # flat estimate for logging; free tier has no per-call billing


def domain_age_days(domain: str) -> tuple[int | None, float]:
    """Returns (age_in_days or None if undeterminable, cost_inr)."""
    try:
        w = whois.whois(domain)
        created = w.creation_date
        if isinstance(created, list):
            created = created[0]
        if created:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - created).days, 0.0
    except Exception as exc:
        logger.info("python-whois failed for %s: %s", domain, exc)

    if settings.WHOISXML_KEY:
        return _whoisxml_age_days(domain)
    return None, 0.0


def _whoisxml_age_days(domain: str) -> tuple[int | None, float]:
    try:
        resp = httpx.get(
            "https://www.whoisxmlapi.com/whoisserver/WhoisService",
            params={"apiKey": settings.WHOISXML_KEY, "domainName": domain, "outputFormat": "JSON"},
            timeout=10,
        )
        resp.raise_for_status()
        created_str = resp.json().get("WhoisRecord", {}).get("createdDate")
        if not created_str:
            return None, WHOISXML_COST_INR
        created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created).days, WHOISXML_COST_INR
    except Exception as exc:
        logger.warning("WhoisXML lookup failed for %s: %s", domain, exc)
        return None, 0.0
