RISK_BAND_LABEL = {
    "low": "🟢 Low risk",
    "caution": "🟡 Caution",
    "high": "🔴 High risk",
    "insufficient": "⚪ Insufficient information",
}


LEVEL_BADGE = {"isolated": "△", "recurring": "▲", "severe": "⛔"}

LEVEL_LABEL = {
    "isolated": ("△", "a few reports"),
    "recurring": ("▲", "repeated reports"),
    "severe": ("⛔", "widespread complaints"),
}

CATEGORY_LABEL = {
    "fraud": "Fraud / never delivered",
    "quality": "Product quality",
    "delivery": "Delivery delays",
    "as_described": "Items not as described",
    "responsiveness": "Customer service",
}

# Plain-language reassurance for each fraud pattern that came back clean.
# The card must read agnostic: what we verified and found FINE is as much a
# finding as what we flagged.
_POSITIVE_BY_SIGNAL = {
    6: "No fraud reports on file for this seller",
    2: "Product photos appear original, not lifted from other sites",
    9: "No payment accounts linked to past scams",
    4: "Comments under posts look healthy, no complaint pattern",
    3: "Real customers tag them in their own posts",
    7: "Follower engagement looks organic",
    5: "Bio website checks out",
    8: "No pressure to move payment off Instagram",
}
_POSITIVE_ORDER = (6, 2, 9, 4, 3, 7, 5, 8)


def positive_lines(signals: list[dict], experience: dict | None = None, limit: int = 4) -> list[str]:
    """What checked out clean, strongest reassurance first."""
    out: list[str] = []
    positive = (experience or {}).get("positive_signals") or 0
    if positive:
        out.append(f"{positive} buyer(s) reported a good experience")

    clean = {s.get("number"): s for s in (signals or []) if s.get("status") == "not_matched"}
    age = ((clean.get(1) or {}).get("data") or {}).get("age_days")
    if age and age >= 365:
        years = age // 365
        out.append(f"Account has ~{years} year{'s' if years > 1 else ''} of posting history")
    for num in _POSITIVE_ORDER:
        if num in clean:
            out.append(_POSITIVE_BY_SIGNAL[num])
    return out[:limit]


def render_card(
    handle: str,
    risk_band: str,
    patterns_matched: int,
    patterns_total: int,
    evidence_lines: list[str],
    experience: dict | None = None,
    signals: list[dict] | None = None,
    weighted_score: int = 0,
) -> str:
    from engine.scoring import risk_score, score_label

    score = risk_score(weighted_score, risk_band, experience)
    emoji, read = score_label(score)

    lines = [f"@{handle}"]
    if score is None:
        lines.append("⚪ Not enough data for a verdict yet. here's what we could see:")
    else:
        # display on a 1-9 scale out of 10: never claim 0 (certainty of safety)
        # or 10 (certainty of fraud) — both are indefensible
        score10 = max(1, min(9, round(score / 10)))
        lines.append(f"{emoji} Scam likelihood: {score10}/10 ({read})")
    lines.append(f"Fraud patterns: {patterns_matched} of {patterns_total} checked patterns matched")

    exp_cats = (experience or {}).get("categories") or {}
    has_serious_complaints = any(level in ("recurring", "severe") for level in exp_cats.values())

    # A clean fraud scan with bad buyer reviews must not read as a clean bill
    # of health — the complaints are the headline for that seller.
    if has_serious_complaints and risk_band in ("insufficient", "low"):
        lines.append("⚠️ Not flagged for fraud, but real buyers report problems. read below before ordering.")

    good = positive_lines(signals or [], experience)
    if good:
        lines.append("")
        lines.append("✅ What looks good:")
        lines += [f"• {line}" for line in good]

    if evidence_lines:
        lines.append("")
        lines.append("🚩 Red flags:")
        lines += [f"• {line}" for line in evidence_lines]

    lines.append("")
    if experience and experience.get("has_data"):
        lines += experience_lines(experience)
    else:
        lines.append("⭐ Community reviews: none found yet")
    return "\n".join(lines)


def experience_lines(experience: dict) -> list[str]:
    """Plain-language block for the community-review data; shared by the
    final card and the early bot reply. Positives lead — a seller with 6 happy
    buyers and one complaint must not read like a scam warning."""
    exp_cats = experience.get("categories") or {}
    detail = experience.get("detail") or {}
    positive = experience.get("positive_signals") or 0

    lines = ["⭐ What buyers report:"]
    if positive:
        lines.append(f"👍 {positive} buyer(s) had a good experience")

    shown = 0
    for cat, level in exp_cats.items():
        if level in ("recurring", "severe"):
            badge, word = LEVEL_LABEL[level]
            n = (detail.get(cat) or {}).get("contributors")
            who = f" ({n} independent reports)" if n else ""
            lines.append(f"{badge} {CATEGORY_LABEL.get(cat, cat.replace('_', ' ').title())}: {word}{who}")
            shown += 1
    if not shown:
        if any(level == "isolated" for level in exp_cats.values()):
            lines.append("△ Only scattered one-off complaints, nothing looks systematic")
        else:
            lines.append("✓ No recurring complaints found in community reviews")
    if experience.get("summary"):
        lines.append(f"“{experience['summary']}”")
    for url in (experience.get("sources") or [])[:2]:
        lines.append(f"🔗 {url}")
    return lines


def render_experience_early(handle: str, experience: dict | None) -> str | None:
    """Fast first reply: community reviews only, sent while the slower
    Instagram scan is still running. None = nothing worth sending yet — send
    only when there's real signal (a complaint, a positive, or a concrete
    summary), otherwise the full card covers it a moment later."""
    if not experience or not experience.get("has_data"):
        return None
    cats = experience.get("categories") or {}
    has_complaint = any(lvl in ("isolated", "recurring", "severe") for lvl in cats.values())
    worth_sending = has_complaint or experience.get("positive_signals") or experience.get("summary")
    if not worth_sending:
        return None
    return "\n".join(
        [f"@{handle}: what buyers say (account scan still running)"] + experience_lines(experience)
    )


def render_from_snapshot(handle: str, snapshot) -> str:
    """Card for a stored RiskSnapshot. ALWAYS re-renders from the stored
    components so cached sellers get the current wording/format the moment
    it changes — the frozen card_text is only a fallback for ancient
    snapshots that predate component storage."""
    data = snapshot.signals or {}
    signals = data.get("signals", [])
    if signals:
        evidence = [
            s.get("evidence", "")
            for s in signals
            if s.get("status") == "matched" and s.get("evidence")
        ][:4]
        return render_card(
            handle,
            snapshot.risk_band,
            snapshot.patterns_matched,
            snapshot.patterns_total,
            evidence,
            experience=data.get("experience"),
            signals=signals,
            weighted_score=data.get("weighted_score") or 0,
        )
    return data.get("card_text") or render_card(
        handle, snapshot.risk_band, snapshot.patterns_matched, snapshot.patterns_total, []
    )
