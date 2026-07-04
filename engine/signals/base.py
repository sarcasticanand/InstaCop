from shared.schemas import SignalResult

SIGNAL_DEFS: dict[int, tuple[str, str]] = {
    1: ("young_account", "Young account, commercial intent"),
    2: ("stolen_photos", "Stolen product photos"),
    3: ("no_customer_tags", "No organic customer tags"),
    4: ("comment_red_flags", "Comment red flags"),
    5: ("suspicious_website", "Suspicious bio website"),
    6: ("prior_fraud_mentions", "Prior fraud mentions"),
    7: ("purchased_followers", "Purchased followers"),
    8: ("offplatform_funnel", "Off-platform funnel pressure"),
    9: ("payment_identity_history", "Linked payment identity has history"),
}


def matched(number: int, evidence: str, confidence: float = 0.8, data: dict | None = None) -> SignalResult:
    key, title = SIGNAL_DEFS[number]
    return SignalResult(key=key, number=number, title=title, status="matched", evidence=evidence, confidence=confidence, data=data or {})


def not_matched(number: int, evidence: str, confidence: float = 0.8, data: dict | None = None) -> SignalResult:
    key, title = SIGNAL_DEFS[number]
    return SignalResult(key=key, number=number, title=title, status="not_matched", evidence=evidence, confidence=confidence, data=data or {})


def unavailable(number: int, evidence: str, data: dict | None = None) -> SignalResult:
    key, title = SIGNAL_DEFS[number]
    return SignalResult(key=key, number=number, title=title, status="unavailable", evidence=evidence, confidence=0.0, data=data or {})
