"""SQLAlchemy 2.0 database models for the monitoring system."""

import uuid
from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy import (
    String,
    Text,
    Integer,
    Boolean,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    Index,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.db.base import Base


def generate_uuid() -> str:
    """Generate a UUID4 string."""
    return str(uuid.uuid4())


def utc_now() -> datetime:
    """Return current UTC datetime."""
    return datetime.now(timezone.utc)


class MonitoredTarget(Base):
    """Represents a monitored channel, group, page, or individual post/video."""

    __tablename__ = "monitored_targets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    platform: Mapped[str] = mapped_column(String(32), index=True)  # telegram, youtube, instagram, facebook, x
    target_type: Mapped[str] = mapped_column(String(32))  # telegram_chat, post_url, account_feed
    external_id: Mapped[str] = mapped_column(String(255), index=True)  # e.g., Telegram chat ID, video ID, tweet ID
    url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    title_or_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, default=300)
    last_cursor: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_scraped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    total_comments_collected: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    comments: Mapped[List["Comment"]] = relationship("Comment", back_populates="target", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_target_platform_external_id"),
        Index("idx_targets_active_platform", "is_active", "platform"),
    )


class PlatformAuthor(Base):
    """Normalized profile of a commenter or participant across platforms."""

    __tablename__ = "platform_authors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    platform: Mapped[str] = mapped_column(String(32), index=True)  # telegram, youtube, instagram, facebook, x
    external_user_id: Mapped[str] = mapped_column(String(128), index=True)  # e.g., Telegram numeric user ID
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    profile_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    total_comments: Mapped[int] = mapped_column(Integer, default=0)
    flagged_comments_count: Mapped[int] = mapped_column(Integer, default=0)

    comments: Mapped[List["Comment"]] = relationship("Comment", back_populates="author")

    __table_args__ = (
        UniqueConstraint("platform", "external_user_id", name="uq_author_platform_user_id"),
        Index("idx_authors_platform_username", "platform", "username"),
    )


class Comment(Base):
    """Normalized comment or message from any monitored platform."""

    __tablename__ = "comments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("monitored_targets.id", ondelete="SET NULL"), nullable=True, index=True)
    author_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("platform_authors.id", ondelete="SET NULL"), nullable=True, index=True)
    external_comment_id: Mapped[str] = mapped_column(String(255))
    parent_external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    child_comment_count: Mapped[int] = mapped_column(Integer, default=0)
    source_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    post_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    comment_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    content_text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    # LLM Evaluation Fields
    llm_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)  # pending, processing, evaluated, skipped_prefilter, failed
    is_on_topic: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    is_severely_negative: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, index=True)
    severity_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 0 to 10
    llm_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Alert Fields
    alert_sent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    alert_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    target: Mapped[Optional["MonitoredTarget"]] = relationship("MonitoredTarget", back_populates="comments")
    author: Mapped[Optional["PlatformAuthor"]] = relationship("PlatformAuthor", back_populates="comments")

    __table_args__ = (
        UniqueConstraint("platform", "external_comment_id", name="uq_comment_platform_external_id"),
        Index("idx_comments_severity_flag", "is_severely_negative", "severity_score"),
        Index("idx_comments_pending_eval", "llm_status", "ingested_at"),
    )


class ScraperSession(Base):
    """Burner account cookies and proxy association for IG, FB, and X."""

    __tablename__ = "scraper_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    platform: Mapped[str] = mapped_column(String(32), index=True)  # instagram, facebook, x, telegram
    account_identifier: Mapped[str] = mapped_column(String(255))
    cookies_encrypted: Mapped[str] = mapped_column(Text)
    user_agent: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    proxy_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)  # active, cooldown, challenged, banned
    cooldown_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
