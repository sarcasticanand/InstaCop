from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class IGPost(BaseModel):
    id: str | None = None
    shortcode: str | None = None
    image_url: str | None = None
    caption: str | None = None
    posted_at: datetime | None = None
    like_count: int | None = None
    comment_count: int | None = None


class IGTag(BaseModel):
    """A post where someone else tagged this seller (organic customer proof)."""

    post_id: str | None = None
    tagger_username: str | None = None
    tagger_account_created_at: datetime | None = None
    posted_at: datetime | None = None


class IGComment(BaseModel):
    post_id: str | None = None
    author_username: str | None = None
    text: str
    posted_at: datetime | None = None


class IGProfile(BaseModel):
    """Normalized shape every ig_provider implementation must return."""

    handle: str
    ig_user_id: str | None = None
    display_name: str | None = None
    bio_text: str | None = None
    bio_website_url: str | None = None
    follower_count: int | None = None
    following_count: int | None = None
    post_count: int | None = None
    is_private: bool = False
    is_business_account: bool | None = None
    business_category: str | None = None
    comments_disabled: bool = False
    oldest_post_at: datetime | None = None
    account_created_at: datetime | None = None
    recent_posts: list[IGPost] = Field(default_factory=list)
    tagged_posts: list[IGTag] = Field(default_factory=list)
    comments: list[IGComment] = Field(default_factory=list)
    tagged_post_comments: list[IGComment] = Field(default_factory=list)
    source: Literal[
        "apify", "instaloader", "screenshot_vision", "hikerapi", "meta_bd", "meta_bd+hikerapi"
    ] = "apify"
    fetched_at: datetime = Field(default_factory=datetime.utcnow)
    raw: dict = Field(default_factory=dict)


SignalStatus = Literal["matched", "not_matched", "unavailable"]


class SignalResult(BaseModel):
    key: str
    number: int
    title: str
    status: SignalStatus
    evidence: str
    confidence: float = 0.0
    data: dict = Field(default_factory=dict)

    @property
    def matched(self) -> bool:
        return self.status == "matched"

    @property
    def computable(self) -> bool:
        return self.status != "unavailable"


RiskBand = Literal["low", "caution", "high", "insufficient"]


class RiskCard(BaseModel):
    handle: str
    risk_band: RiskBand
    patterns_matched: int
    patterns_total: int
    signals: list[SignalResult]
    card_text: str
    cost_inr: float
    check_id: int | None = None
    computed_at: datetime = Field(default_factory=datetime.utcnow)
