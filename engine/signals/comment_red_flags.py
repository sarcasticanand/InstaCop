from datetime import datetime, timedelta, timezone

from shared.schemas import IGComment, IGProfile, SignalResult

from engine.llm import classify_comments

from .base import matched, not_matched, unavailable

RECENT_WINDOW_DAYS = 60
TAGGED_COMPLAINT_THRESHOLD = 2  # complaints on tagged posts are brand-directed


def _recent_count(comments: list[IGComment], indices: set[int]) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_WINDOW_DAYS)
    count = 0
    for i in indices:
        posted = comments[i].posted_at
        if posted is None:
            continue
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        if posted >= cutoff:
            count += 1
    return count


def compute(profile: IGProfile) -> tuple[SignalResult, float]:
    if profile.comments_disabled:
        return matched(4, "Comments are disabled on a selling account.", data={"comments_disabled": True}), 0.0

    own = [c for c in profile.comments if c.text]
    tagged = [c for c in profile.tagged_post_comments if c.text]
    if not own and not tagged:
        return unavailable(4, "No comments available to classify."), 0.0

    # One classification call covers both surfaces. Own-post comments feed the
    # complaint/bot ratios. Tagged-post comments live on other accounts, so
    # praise there is usually for the poster (an influencer), not the seller --
    # per product decision we ignore positives on tagged posts and count only
    # complaints, which are almost always brand-directed ("never arrived",
    # "cheap quality", "scam").
    all_comments = own + tagged
    result, cost = classify_comments([c.text for c in all_comments])
    by_index = {c.get("index"): c.get("category") for c in result.get("classifications", [])}

    own_total = len(own)
    own_complaints = {i for i in range(own_total) if by_index.get(i) == "complaint"}
    own_bots = sum(1 for i in range(own_total) if by_index.get(i) == "generic_bot")
    tagged_complaints = {
        i for i in range(own_total, own_total + len(tagged)) if by_index.get(i) == "complaint"
    }

    complaint_ratio = len(own_complaints) / own_total if own_total else 0.0
    bot_ratio = own_bots / own_total if own_total else 0.0
    recent_complaints = _recent_count(all_comments, own_complaints | tagged_complaints)

    data = {
        "complaint_ratio": complaint_ratio,
        "bot_ratio": bot_ratio,
        "own_total": own_total,
        "own_complaints": len(own_complaints),
        "tagged_total": len(tagged),
        "tagged_complaints": len(tagged_complaints),
        "recent_complaints_60d": recent_complaints,
    }

    recency_note = f" {recent_complaints} complaint(s) posted within the last {RECENT_WINDOW_DAYS} days." if recent_complaints else ""

    if own_total and complaint_ratio > 0.10:
        return matched(
            4,
            f"{len(own_complaints)} of {own_total} comments on the seller's posts are complaints ({complaint_ratio:.0%})."
            + recency_note,
            data=data,
        ), cost
    if len(tagged_complaints) >= TAGGED_COMPLAINT_THRESHOLD:
        return matched(
            4,
            f"{len(tagged_complaints)} complaint(s) about the seller found in comments on tagged posts."
            + recency_note,
            data=data,
        ), cost
    if own_total and bot_ratio > 0.70:
        return matched(
            4,
            f"{own_bots} of {own_total} comments on the seller's posts look generic/bot-like ({bot_ratio:.0%}).",
            data=data,
        ), cost
    return not_matched(
        4,
        f"{own_total} own-post and {len(tagged)} tagged-post comments checked: "
        f"{len(own_complaints)} + {len(tagged_complaints)} complaints, {own_bots} generic." + recency_note,
        data=data,
    ), cost
