from shared.schemas import IGProfile, SignalResult

from .base import matched, not_matched, unavailable


def compute(profile: IGProfile) -> tuple[SignalResult, float]:
    if not profile.follower_count or not profile.recent_posts:
        return unavailable(7, "Follower count or recent posts unavailable."), 0.0

    engagements = [
        (p.like_count or 0) + (p.comment_count or 0)
        for p in profile.recent_posts[:12]
        if p.like_count is not None or p.comment_count is not None
    ]
    if not engagements:
        return unavailable(7, "No engagement data on recent posts."), 0.0

    avg_engagement = sum(engagements) / len(engagements)
    engagement_rate = avg_engagement / profile.follower_count if profile.follower_count else 0.0

    if engagement_rate < 0.003 and profile.follower_count > 10000:
        return matched(
            7,
            f"Engagement rate {engagement_rate:.2%} on {profile.follower_count:,} followers (expected >0.3%).",
            data={"engagement_rate": engagement_rate, "follower_count": profile.follower_count},
        ), 0.0
    return not_matched(
        7,
        f"Engagement rate {engagement_rate:.2%} on {profile.follower_count:,} followers.",
        data={"engagement_rate": engagement_rate, "follower_count": profile.follower_count},
    ), 0.0
