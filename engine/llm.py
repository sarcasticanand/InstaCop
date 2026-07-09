"""LLM access for the risk engine. One provider-agnostic interface
(LLMProvider.complete) so extraction/classification and synthesis can each
point at whichever model is cheapest-for-quality, per call-site, via config.
Swapping provider or model never touches signal code."""

import json
import logging
import re
from abc import ABC, abstractmethod

import anthropic
from google import genai
from google.genai import types as genai_types

from shared.config import settings

logger = logging.getLogger(__name__)

USD_TO_INR = 83.0

# USD per 1M tokens: (input, output). Pricing shifts often for both vendors —
# this is the one place to update it. Unknown models fall back to a
# conservative estimate rather than crashing cost tracking.
PRICING_USD_PER_MTOK = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-5": (3.00, 15.00),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.1-pro-preview": (2.00, 12.00),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
}
DEFAULT_PRICING_USD_PER_MTOK = (2.00, 10.00)


def _cost_inr(model: str, input_tokens: int, output_tokens: int) -> float:
    in_price, out_price = PRICING_USD_PER_MTOK.get(model, DEFAULT_PRICING_USD_PER_MTOK)
    usd = input_tokens / 1_000_000 * in_price + output_tokens / 1_000_000 * out_price
    return usd * USD_TO_INR


class LLMProvider(ABC):
    @abstractmethod
    def complete(self, system: str, user: str, max_tokens: int = 1024, json_mode: bool = False) -> tuple[str, float]:
        """Returns (text, cost_inr)."""

    @abstractmethod
    def complete_vision(self, system: str, image_bytes: bytes, mime_type: str, max_tokens: int = 512) -> tuple[str, float]:
        """Vision extraction. Returns (text, cost_inr)."""


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str, api_key: str | None = None):
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key or settings.ANTHROPIC_API_KEY)

    def complete(self, system: str, user: str, max_tokens: int = 1024, json_mode: bool = False) -> tuple[str, float]:
        message = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = message.content[0].text if message.content else ""
        cost_inr = _cost_inr(self.model, message.usage.input_tokens, message.usage.output_tokens)
        return text, cost_inr

    def complete_vision(self, system: str, image_bytes: bytes, mime_type: str, max_tokens: int = 512) -> tuple[str, float]:
        import base64

        b64 = base64.standard_b64encode(image_bytes).decode()
        message = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                        {"type": "text", "text": "Extract as instructed. JSON only."},
                    ],
                }
            ],
        )
        text = message.content[0].text if message.content else ""
        return text, _cost_inr(self.model, message.usage.input_tokens, message.usage.output_tokens)


class GeminiProvider(LLMProvider):
    def __init__(self, model: str, api_key: str | None = None):
        self.model = model
        self.client = genai.Client(api_key=api_key or settings.GEMINI_API_KEY)

    def complete(self, system: str, user: str, max_tokens: int = 1024, json_mode: bool = False) -> tuple[str, float]:
        config = genai_types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
            **({"response_mime_type": "application/json"} if json_mode else {}),
        )
        response = self.client.models.generate_content(model=self.model, contents=user, config=config)
        text = response.text or ""
        usage = response.usage_metadata
        cost_inr = _cost_inr(self.model, usage.prompt_token_count or 0, usage.candidates_token_count or 0)
        return text, cost_inr

    def complete_vision(self, system: str, image_bytes: bytes, mime_type: str, max_tokens: int = 512) -> tuple[str, float]:
        from google.genai import types as genai_types

        response = self.client.models.generate_content(
            model=self.model,
            contents=[genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type), system],
            config=genai_types.GenerateContentConfig(max_output_tokens=max_tokens, response_mime_type="application/json"),
        )
        usage = response.usage_metadata
        return response.text or "", _cost_inr(self.model, usage.prompt_token_count or 0, usage.candidates_token_count or 0)


def _build(provider: str, model: str) -> LLMProvider:
    if provider == "gemini":
        return GeminiProvider(model)
    return AnthropicProvider(model)


def get_extract_llm() -> LLMProvider:
    return _build(settings.MODEL_EXTRACT_PROVIDER, settings.MODEL_EXTRACT)


def get_synth_llm() -> LLMProvider:
    return _build(settings.MODEL_SYNTH_PROVIDER, settings.MODEL_SYNTH)


def get_pii_llm() -> LLMProvider:
    """PII-bearing vision calls (payment screenshots)."""
    return _build(settings.MODEL_PII_PROVIDER, settings.MODEL_PII)


def _parse_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


COMMENT_CLASSIFY_SYSTEM = (
    "Classify each Instagram comment into exactly one category: "
    "'complaint' (reports non-delivery, fake product, bad quality, scam concern), "
    "'generic_bot' (emoji-only, generic praise with no specifics, spam-like), "
    "'organic' (specific, plausibly genuine customer reaction), or "
    "'question_unanswered' (asks the seller something, e.g. price/availability). "
    "Return ONLY a JSON object: {\"classifications\": [{\"index\": int, \"category\": str}, ...]}."
)


def classify_comments(comments: list[str]) -> tuple[dict, float]:
    if not comments:
        return {"classifications": []}, 0.0
    llm = get_extract_llm()
    numbered = "\n".join(f"{i}: {c}" for i, c in enumerate(comments))
    text, cost = llm.complete(COMMENT_CLASSIFY_SYSTEM, numbered, max_tokens=1024, json_mode=True)
    return _parse_json(text), cost


OFFPLATFORM_FUNNEL_SYSTEM = (
    "Analyze this Instagram seller's bio and recent captions for off-platform "
    "sales pressure. Return ONLY a JSON object: "
    '{"pushes_whatsapp_or_dm_only": bool, "no_checkout_mentioned": bool, '
    '"urgency_language": bool, "evidence": str (one short quote or paraphrase)}.'
)


def analyze_offplatform_funnel(bio_text: str, captions: list[str]) -> tuple[dict, float]:
    llm = get_extract_llm()
    user = f"Bio: {bio_text or '(empty)'}\n\nRecent captions:\n" + "\n---\n".join(captions[:12] or ["(none)"])
    text, cost = llm.complete(OFFPLATFORM_FUNNEL_SYSTEM, user, max_tokens=512, json_mode=True)
    return _parse_json(text), cost


WEBSITE_ANALYSIS_SYSTEM = (
    "Analyze this seller website's page text. Return ONLY a JSON object: "
    '{"has_contact_address": bool, "has_gst_number": bool, "has_return_policy": bool, '
    '"prepaid_only_language": bool, "looks_like_template_or_stock_content": bool, '
    '"evidence": str (one short supporting quote or paraphrase per notable finding)}.'
)


def analyze_website(page_text: str) -> tuple[dict, float]:
    llm = get_extract_llm()
    truncated = page_text[:8000]
    text, cost = llm.complete(WEBSITE_ANALYSIS_SYSTEM, truncated, max_tokens=768, json_mode=True)
    return _parse_json(text), cost


SYNTHESIS_SYSTEM = (
    "You write a short, factual risk card for an Instagram seller trust-check tool. "
    "You are given a JSON list of fraud-pattern signals that were computed algorithmically. "
    "Write 3-4 short evidence lines in plain language, one per matched or notable signal. "
    "Words like 'fraud' and 'scam' ARE allowed as topic/category words, e.g. 'matches 6 of 9 "
    "known fraud patterns' or 'known scam patterns' — that phrasing is required, not banned. "
    "HARD RULE: never use a verdict label that accuses the seller/person directly — banned "
    "words are 'scammer', 'fraudster', 'con artist', 'criminal', 'thief', 'cheat', 'cheater'. "
    "Describe pattern matches factually (e.g. 'account created recently and already running "
    "ads'), never a conclusion about who the seller is. Do not recommend buying or not buying. "
    "Return ONLY a JSON object: {\"evidence_lines\": [str, ...]}."
)

# Defense in depth: these verdict nouns have no legitimate use in this product's copy, so any
# occurrence means the model ignored the system prompt. Caught here in code rather than trusted
# to instruction-following, since this is a legal constraint, not a style preference. Deliberately
# excludes 'fraud'/'scam' themselves — spec-mandated phrasing like "fraud patterns" needs those.
BANNED_VERDICT_WORDS = [
    "scammer", "scammers", "fraudster", "fraudsters", "con artist", "con artists",
    "criminal", "criminals", "thief", "thieves", "cheat", "cheater", "cheaters",
]
_BANNED_WORD_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in BANNED_VERDICT_WORDS) + r")\b", re.IGNORECASE)


def _violates_banned_words(lines: list[str]) -> bool:
    return any(_BANNED_WORD_RE.search(line) for line in lines)


def synthesize_card(handle: str, patterns_matched: int, patterns_total: int, signals_json: list[dict]) -> tuple[dict, float]:
    llm = get_synth_llm()
    user = (
        f"Seller handle: @{handle}\n"
        f"Patterns matched: {patterns_matched} of {patterns_total} computable\n"
        f"Signals: {json.dumps(signals_json)}"
    )
    text, cost = llm.complete(SYNTHESIS_SYSTEM, user, max_tokens=1024, json_mode=True)
    parsed = _parse_json(text)
    lines = parsed.get("evidence_lines") or []
    if _violates_banned_words(lines):
        logger.warning("Synthesis output contained a banned verdict word; discarding, caller falls back to raw signal evidence.")
        return {"evidence_lines": []}, cost
    return parsed, cost
