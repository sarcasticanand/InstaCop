from datetime import datetime, timezone

from shared.schemas import IGProfile, SignalResult

from .base import matched, not_matched, unavailable

# Structural signal only: tag counts and burst timing. Complaint analysis of
# tagged-post comments lives in signal 4 (comment_red_flags), which classifies
# own-post and tagged-post comments in a single LLM call.


def compute(profile: IGProfile) -> tuple[SignalResult, float]:
    age_days = None
    if profile.oldest_post_at:
        oldest = profile.oldest_post_at
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - oldest).days

    if age_days is None:
        return unavailable(3, "Account age unknown; cannot judge expected tag count."), 0.0

    tag_count = len(profile.tagged_posts)

    if age_days >= 60 and tag_count < 3:
        return matched(
            3,
            f"Account ~{age_days} days old with only {tag_count} customer-tagged post(s).",
            data={"tag_count": tag_count, "age_days": age_days},
        ), 0.0

    burst_dates = sorted(t.tagger_account_created_at for t in profile.tagged_posts if t.tagger_account_created_at)
    if len(burst_dates) >= 3 and (burst_dates[-1] - burst_dates[0]).days <= 7:
        return matched(
            3,
            f"{len(burst_dates)} tags from accounts all created within the same week.",
            data={"tag_count": tag_count, "burst": True},
        ), 0.0

    return not_matched(
        3,
        f"{tag_count} organic customer-tagged post(s), no burst pattern detected.",
        data={"tag_count": tag_count},
    ), 0.0
