from datetime import datetime, timezone

from shared.schemas import IGProfile, SignalResult

from .base import matched, not_matched, unavailable

SELLING_KEYWORDS = [
    "price", "dm to order", "dm for order", "cod", "cash on delivery",
    "shipping", "shop now", "order now", "₹", "rs.", "rs ", "book now",
]


def _age_days(ts) -> int | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).days


def compute(profile: IGProfile) -> tuple[SignalResult, float]:
    bio = (profile.bio_text or "").lower()
    is_selling = any(k in bio for k in SELLING_KEYWORDS)

    fetched = len(profile.recent_posts or [])
    total = profile.post_count if profile.post_count is not None else fetched

    # A real creation date is authoritative; the oldest-post date is only a
    # proxy — prefer the former when a provider gives it.
    age_days = _age_days(profile.account_created_at)
    source = "account age"

    if age_days is None:
        oldest = _age_days(profile.oldest_post_at)
        if oldest is None:
            return unavailable(1, "Account age could not be determined (no posts / private account)."), 0.0
        # The oldest post we fetched only reflects the account's true age when
        # our window reaches the very first post. Active sellers post daily, so
        # 12 recent posts can span weeks and make a years-old brand look new.
        # If the account has many more posts than we sampled, it's established
        # and the proxy is meaningless — never flag it as young.
        if total > fetched + 1:
            return not_matched(
                1,
                f"Established seller: ~{total} posts, far more than the recent window shows.",
                data={"post_count": total, "established": True},
            ), 0.0
        age_days = oldest
        source = "proxy via oldest post"

    if age_days <= 90 and is_selling:
        return matched(
            1,
            f"Account ~{age_days} days old ({source}), already selling in bio.",
            data={"age_days": age_days, "post_count": total},
        ), 0.0
    return not_matched(
        1,
        f"Account ~{age_days} days old ({source}); {'selling language present' if is_selling else 'no clear selling signal'} in bio.",
        data={"age_days": age_days, "post_count": total},
    ), 0.0
