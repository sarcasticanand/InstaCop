from datetime import datetime, timezone

from shared.schemas import IGProfile, SignalResult

from .base import matched, not_matched, unavailable

SELLING_KEYWORDS = [
    "price", "dm to order", "dm for order", "cod", "cash on delivery",
    "shipping", "shop now", "order now", "₹", "rs.", "rs ", "book now",
]


def compute(profile: IGProfile) -> tuple[SignalResult, float]:
    age_days = None
    reference = profile.oldest_post_at or profile.account_created_at
    if reference:
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - reference).days

    if age_days is None:
        return unavailable(1, "Account age could not be determined (no posts / private account)."), 0.0

    bio = (profile.bio_text or "").lower()
    is_selling = any(k in bio for k in SELLING_KEYWORDS)

    if age_days <= 90 and is_selling:
        return matched(
            1,
            f"Account ~{age_days} days old (proxy via oldest post), already selling in bio.",
            data={"age_days": age_days},
        ), 0.0
    return not_matched(
        1,
        f"Account ~{age_days} days old; {'selling language present' if is_selling else 'no clear selling signal'} in bio.",
        data={"age_days": age_days},
    ), 0.0
