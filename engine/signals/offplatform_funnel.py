from shared.schemas import IGProfile, SignalResult

from engine.llm import analyze_offplatform_funnel

from .base import matched, not_matched, unavailable


def compute(profile: IGProfile) -> tuple[SignalResult, float]:
    captions = [p.caption for p in profile.recent_posts if p.caption]
    if not profile.bio_text and not captions:
        return unavailable(8, "No bio or captions available."), 0.0

    analysis, cost = analyze_offplatform_funnel(profile.bio_text or "", captions)
    strong_offplatform = bool(analysis.get("pushes_whatsapp_or_dm_only")) and bool(analysis.get("no_checkout_mentioned"))
    urgency = bool(analysis.get("urgency_language"))

    if strong_offplatform and urgency:
        quote = (analysis.get("evidence") or "").strip()
        evidence = "Pushes DM/WhatsApp-only ordering with urgency language"
        evidence += f" (e.g. “{quote[:60]}”)." if quote else "."
        return matched(8, evidence, data=analysis), cost
    return not_matched(
        8,
        analysis.get("evidence") or "No strong off-platform-plus-urgency combination detected.",
        data=analysis,
    ), cost
