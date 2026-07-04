"""Brand-identity helpers: extract the brand *stem* from a handle so we can
recognize the same operator across platforms and suffix variants.

shiromani.india -> shiromani     dotmen.india -> dotmen
shiromani_official -> shiromani  trendfactory_clothing -> trendfactory
"""

import re

# Suffix tokens that decorate handles without changing brand identity.
# Order matters: strip repeatedly until stable.
NOISE_TOKENS = {
    "india", "in", "official", "officials", "store", "stores", "shop", "shops",
    "online", "hq", "co", "com", "the", "real", "original", "new", "big",
    "boutique", "collection", "collections", "clothing", "wear", "fashion",
}

_SEPARATORS = re.compile(r"[._\-]+")
_TRAILING_DIGITS = re.compile(r"\d+$")

# longest first so 'officials' strips before 'official' etc.
_NOISE_BY_LENGTH = sorted(NOISE_TOKENS, key=len, reverse=True)
_MIN_STEM_REMAINDER = 4


def _strip_glued_noise(token: str) -> str:
    """'fabindiaofficial' -> 'fabindia'; 'shiromanistore' -> 'shiromani'.
    Only strips while a substantial (>=4 char) remainder survives, so
    'gapstore' stays 'gapstore' rather than collapsing to 'gap'."""
    changed = True
    while changed:
        changed = False
        for noise in _NOISE_BY_LENGTH:
            if token.endswith(noise) and len(token) - len(noise) >= _MIN_STEM_REMAINDER:
                token = token[: -len(noise)]
                changed = True
                break
    return token


def brand_stem(handle: str) -> str:
    """Best-effort brand stem. Falls back to the cleaned handle when stripping
    would destroy identity entirely (e.g. handle IS a noise word)."""
    cleaned = (handle or "").lower().lstrip("@").strip()
    if not cleaned:
        return ""
    tokens = [t for t in _SEPARATORS.split(cleaned) if t]
    # strip trailing digits per token ("trendy1" -> "trendy"), then drop noise
    stripped = []
    for t in tokens:
        t = _TRAILING_DIGITS.sub("", t)
        t = _strip_glued_noise(t)
        if t and t not in NOISE_TOKENS:
            stripped.append(t)
    if stripped:
        return "".join(stripped)
    # everything was noise -> fall back to joined tokens minus separators
    return "".join(_TRAILING_DIGITS.sub("", t) for t in tokens if t) or cleaned


def same_operator(handle_a: str, handle_b: str) -> bool:
    """True when two handles plausibly belong to the same brand/operator."""
    a, b = brand_stem(handle_a), brand_stem(handle_b)
    if not a or not b:
        return False
    if a == b:
        return True
    # one stem containing the other (>=4 chars to avoid junk matches):
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= 4 and shorter in longer


def stem_in_url(handle: str, url_or_title: str) -> bool:
    """Does this URL or page title look like it belongs to the handle's brand?
    Catches their own site (shiromani.com), their marketplace store page
    (/shiromani-store/), retail listings/press about them ("W for Woman Kurta —
    Ajio"), and their other social profiles."""
    stem = brand_stem(handle)
    if not stem or len(stem) < 4:
        return False
    normalized = re.sub(r"[\s._\-/|·–—]+", "", (url_or_title or "").lower())
    return stem in normalized
