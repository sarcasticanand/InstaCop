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


def render_card(
    handle: str,
    risk_band: str,
    patterns_matched: int,
    patterns_total: int,
    evidence_lines: list[str],
    experience: dict | None = None,
) -> str:
    lines = [
        f"@{handle}",
        f"{RISK_BAND_LABEL.get(risk_band, risk_band)} — matches {patterns_matched} of {patterns_total} fraud patterns"
        f" (checked {patterns_total} of 9)",
    ]

    exp_cats = (experience or {}).get("categories") or {}
    has_serious_complaints = any(level in ("recurring", "severe") for level in exp_cats.values())

    # A clean fraud scan with bad buyer reviews must not read as a clean bill
    # of health — the complaints are the headline for that seller.
    if has_serious_complaints and risk_band in ("insufficient", "low"):
        lines.append("⚠️ Not flagged for fraud, but real buyers report problems — read below before ordering.")

    lines += [f"• {line}" for line in evidence_lines]

    if experience and experience.get("has_data"):
        lines.append("⭐ What buyers report:")
        shown = 0
        for cat, level in exp_cats.items():
            if level in ("recurring", "severe"):
                badge, word = LEVEL_LABEL[level]
                lines.append(f"{badge} {CATEGORY_LABEL.get(cat, cat.replace('_', ' ').title())}: {word}")
                shown += 1
        if not shown:
            if any(level == "isolated" for level in exp_cats.values()):
                lines.append("△ Only scattered one-off complaints — nothing looks systematic")
            else:
                lines.append("✓ No recurring complaints found in community reviews")
        positive = experience.get("positive_signals") or 0
        if positive:
            lines.append(f"👍 {positive} buyer(s) reported a good experience")
        if experience.get("summary"):
            lines.append(f"“{experience['summary']}”")
    else:
        lines.append("⭐ Community reviews: none found yet")
    return "\n".join(lines)


def render_from_snapshot(handle: str, snapshot) -> str:
    """Rebuild a card from a stored RiskSnapshot; prefers the stored card_text,
    falls back to matched signals' evidence for older snapshots."""
    stored = (snapshot.signals or {}).get("card_text")
    if stored:
        return stored
    evidence = [
        s.get("evidence", "")
        for s in (snapshot.signals or {}).get("signals", [])
        if s.get("status") == "matched" and s.get("evidence")
    ][:4]
    return render_card(handle, snapshot.risk_band, snapshot.patterns_matched, snapshot.patterns_total, evidence)
