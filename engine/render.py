RISK_BAND_LABEL = {
    "low": "🟢 Low risk",
    "caution": "🟡 Caution",
    "high": "🔴 High risk",
    "insufficient": "⚪ Insufficient information",
}


LEVEL_BADGE = {"isolated": "△", "recurring": "▲", "severe": "⛔"}


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
    lines += [f"• {line}" for line in evidence_lines]

    if experience and experience.get("has_data"):
        active = [
            f"{cat.replace('_', ' ')} {LEVEL_BADGE.get(level, '')}{level}"
            for cat, level in (experience.get("categories") or {}).items()
            if level != "none"
        ]
        header = "⭐ Experience: " + (" · ".join(active) if active else "no recurring issues reported")
        lines.append(header)
        if experience.get("summary"):
            lines.append(f"“{experience['summary']}”")
    else:
        lines.append("⭐ Customer reviews: none yet")
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
