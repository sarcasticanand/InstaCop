from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Seller(Base):
    __tablename__ = "sellers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ig_handle: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    ig_user_id: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_verified: Mapped[bool] = mapped_column(Boolean, server_default="false")
    verification_status: Mapped[str] = mapped_column(Text, server_default="none")


class SellerAlias(Base):
    __tablename__ = "seller_aliases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    seller_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("sellers.id"))
    ig_handle: Mapped[str] = mapped_column(Text, nullable=False)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaymentIdentity(Base):
    __tablename__ = "payment_identities"
    __table_args__ = (UniqueConstraint("kind", "value"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class SellerPaymentLink(Base):
    __tablename__ = "seller_payment_links"
    __table_args__ = (PrimaryKeyConstraint("seller_id", "payment_identity_id"),)

    seller_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sellers.id"))
    payment_identity_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("payment_identities.id"))
    source: Mapped[str | None] = mapped_column(Text)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Check(Base):
    __tablename__ = "checks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    seller_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("sellers.id"))
    requested_by_chat_id: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    served_from_cache: Mapped[bool | None] = mapped_column(Boolean)
    result: Mapped[dict | None] = mapped_column(JSONB)


class RiskSnapshot(Base):
    __tablename__ = "risk_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    seller_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("sellers.id"))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    signals: Mapped[dict] = mapped_column(JSONB, nullable=False)
    patterns_matched: Mapped[int | None] = mapped_column(Integer)
    patterns_total: Mapped[int | None] = mapped_column(Integer)
    risk_band: Mapped[str | None] = mapped_column(Text)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    seller_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("sellers.id"))
    reporter_chat_id: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    narrative: Mapped[str | None] = mapped_column(Text)
    evidence_file_id: Mapped[str | None] = mapped_column(Text)
    payment_identity_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("payment_identities.id"))
    status: Mapped[str] = mapped_column(Text, server_default="unreviewed")  # unreviewed|accepted|rejected|quarantined|disputed
    issue_categories: Mapped[dict | None] = mapped_column(JSONB)  # Workstream B classifier output
    spam_score: Mapped[float | None] = mapped_column(Float)       # Workstream D, 0=trusted 1=spam
    reporter_trust: Mapped[float | None] = mapped_column(Float)   # Workstream D, 0..1 at submit time
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Followup(Base):
    __tablename__ = "followups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    check_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("checks.id"))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response: Mapped[str | None] = mapped_column(Text)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RawMention(Base):
    __tablename__ = "raw_mentions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    content: Mapped[str | None] = mapped_column(Text)
    processed: Mapped[bool] = mapped_column(Boolean, server_default="false")


class BrandReview(Base):
    """Persistent per-brand review/mention cache (Workstream A). Read-first on
    checks; refreshed only when stale. Structured fields filled by the issue
    classifier (Workstream B) and spam scorer (Workstream D)."""

    __tablename__ = "brand_reviews"
    __table_args__ = (UniqueConstraint("source_url", "seller_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    seller_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sellers.id"))
    source: Mapped[str] = mapped_column(Text, nullable=False)  # reddit|complaint_site|serpapi
    source_url: Mapped[str | None] = mapped_column(Text)
    source_author: Mapped[str | None] = mapped_column(Text)
    source_author_age_days: Mapped[int | None] = mapped_column(Integer)
    source_subreddit: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    issue_categories: Mapped[dict | None] = mapped_column(JSONB)
    sentiment: Mapped[str | None] = mapped_column(Text)
    spam_score: Mapped[float | None] = mapped_column(Float)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BrandReviewFreshness(Base):
    __tablename__ = "brand_review_freshness"
    __table_args__ = (PrimaryKeyConstraint("seller_id", "source"),)

    seller_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sellers.id"))
    source: Mapped[str] = mapped_column(Text)
    last_scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OperatorLink(Base):
    """Evidence that two seller accounts are run by the same operator — the
    burn-and-reopen catch (same photos/UPI under a 'new' brand). Pairs are
    stored normalized: seller_a < seller_b."""

    __tablename__ = "operator_links"
    __table_args__ = (UniqueConstraint("seller_a", "seller_b", "basis"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    seller_a: Mapped[int] = mapped_column(BigInteger, ForeignKey("sellers.id"))
    seller_b: Mapped[int] = mapped_column(BigInteger, ForeignKey("sellers.id"))
    basis: Mapped[str] = mapped_column(Text, nullable=False)  # shared_photo|shared_upi|shared_phone|manual
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    manual_override: Mapped[str | None] = mapped_column(Text)  # 'linked' | 'not_linked'


class Verification(Base):
    __tablename__ = "verifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    seller_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("sellers.id"))
    legal_name: Mapped[str | None] = mapped_column(Text)
    gstin: Mapped[str | None] = mapped_column(Text)
    pan_last4: Mapped[str | None] = mapped_column(Text)
    kyc_doc_refs: Mapped[dict | None] = mapped_column(JSONB)
    razorpay_subscription_id: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lapsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(Text)
