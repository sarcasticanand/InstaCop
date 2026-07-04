from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from shared.schemas import IGProfile, SignalResult

from engine.llm import analyze_website
from engine.whois_lookup import domain_age_days

from .base import matched, not_matched, unavailable


def compute(profile: IGProfile) -> tuple[SignalResult, float]:
    url = profile.bio_website_url
    if not url:
        return unavailable(5, "No website linked in bio."), 0.0

    full_url = url if url.startswith("http") else f"https://{url}"
    domain = urlparse(full_url).hostname or url
    total_cost = 0.0

    age_days, whois_cost = domain_age_days(domain)
    total_cost += whois_cost

    page_text = ""
    try:
        resp = httpx.get(full_url, timeout=10, follow_redirects=True)
        page_text = BeautifulSoup(resp.text, "html.parser").get_text(separator=" ", strip=True)
    except Exception:
        pass

    if not page_text:
        if age_days is not None and age_days < 60:
            return matched(
                5,
                f"Website domain registered {age_days} days ago; page content unreachable.",
                data={"domain_age_days": age_days},
            ), total_cost
        return unavailable(
            5,
            f"Website unreachable and domain age {'unknown' if age_days is None else f'{age_days}d'}.",
        ), total_cost

    analysis, llm_cost = analyze_website(page_text)
    total_cost += llm_cost

    red_flags = sum([
        not analysis.get("has_contact_address", True),
        not analysis.get("has_gst_number", True),
        not analysis.get("has_return_policy", True),
        bool(analysis.get("prepaid_only_language", False)),
    ])

    if age_days is not None and age_days < 60:
        return matched(
            5,
            f"Website domain registered only {age_days} days ago.",
            data={"domain_age_days": age_days, "red_flags": red_flags, **analysis},
        ), total_cost
    if red_flags >= 3:
        return matched(
            5,
            f"Website missing {red_flags} of 4 trust signals (address/GST/returns/prepaid-only).",
            data={"domain_age_days": age_days, "red_flags": red_flags, **analysis},
        ), total_cost
    age_text = f"registered {age_days} days ago" if age_days is not None else "age unknown"
    return not_matched(
        5,
        f"Website domain {age_text}, {red_flags} of 4 red flags present.",
        data={"domain_age_days": age_days, "red_flags": red_flags, **analysis},
    ), total_cost
