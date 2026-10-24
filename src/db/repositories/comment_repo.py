"""Repository for managing Comment records with robust deduplication."""

import hashlib
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from sqlalchemy import select, update, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models import Comment, PlatformAuthor, MonitoredTarget


def compute_content_hash(platform: str, external_comment_id: str) -> str:
    """Compute deterministic SHA-256 hash for O(1) deduplication."""
    payload = f"{platform}:{external_comment_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CommentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, comment_id: str) -> Optional[Comment]:
        stmt = select(Comment).where(Comment.id == comment_id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_by_platform_and_external_id(self, platform: str, external_comment_id: str) -> Optional[Comment]:
        stmt = select(Comment).where(
            Comment.platform == platform,
            Comment.external_comment_id == str(external_comment_id)
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def insert_single(
        self,
        platform: str,
        external_comment_id: str,
        content_text: str,
        target_id: Optional[str] = None,
        author_id: Optional[str] = None,
        parent_external_id: Optional[str] = None,
        source_name: Optional[str] = None,
        post_url: Optional[str] = None,
        comment_url: Optional[str] = None,
        published_at: Optional[datetime] = None,
        child_comment_count: int = 0,
    ) -> Optional[Comment]:
        """Insert a single comment with conflict avoidance. Returns Comment if inserted, None if duplicate."""
        existing = await self.get_by_platform_and_external_id(platform, external_comment_id)
        if existing:
            # If child comment count increased, update it
            if child_comment_count > existing.child_comment_count:
                existing.child_comment_count = child_comment_count
                await self.session.flush()
            return None

        content_hash = compute_content_hash(platform, external_comment_id)
        comment = Comment(
            platform=platform,
            target_id=target_id,
            author_id=author_id,
            external_comment_id=str(external_comment_id),
            parent_external_id=parent_external_id,
            child_comment_count=child_comment_count,
            source_name=source_name,
            post_url=post_url,
            comment_url=comment_url,
            content_text=content_text,
            content_hash=content_hash,
            published_at=published_at,
            llm_status="pending",
        )
        self.session.add(comment)
        await self.session.flush()
        return comment

    async def get_pending_for_llm(self, limit: int = 40) -> List[Comment]:
        """Fetch pending comments ordered by ingestion time for micro-batching."""
        stmt = (
            select(Comment)
            .where(Comment.llm_status == "pending")
            .order_by(Comment.ingested_at.asc())
            .limit(limit)
        )
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def set_status_processing(self, comment_ids: List[str]) -> None:
        if not comment_ids:
            return
        stmt = (
            update(Comment)
            .where(Comment.id.in_(comment_ids))
            .values(llm_status="processing")
        )
        await self.session.execute(stmt)

    async def update_llm_verdict(
        self,
        comment_id: str,
        is_on_topic: bool,
        is_severely_negative: bool,
        severity_score: int,
        llm_reason: Optional[str] = None,
        llm_status: str = "evaluated",
    ) -> None:
        stmt = (
            update(Comment)
            .where(Comment.id == comment_id)
            .values(
                is_on_topic=is_on_topic,
                is_severely_negative=is_severely_negative,
                severity_score=severity_score,
                llm_reason=llm_reason,
                llm_status=llm_status,
            )
        )
        await self.session.execute(stmt)

    async def mark_skipped_prefilter(self, comment_id: str, reason: str = "Trivial or emoji-only") -> None:
        stmt = (
            update(Comment)
            .where(Comment.id == comment_id)
            .values(
                llm_status="skipped_prefilter",
                is_on_topic=False,
                is_severely_negative=False,
                severity_score=0,
                llm_reason=reason,
            )
        )
        await self.session.execute(stmt)

    async def mark_alert_sent(self, comment_id: str) -> None:
        stmt = (
            update(Comment)
            .where(Comment.id == comment_id)
            .values(
                alert_sent=True,
                alert_sent_at=datetime.now(timezone.utc)
            )
        )
        await self.session.execute(stmt)

    async def get_user_comments(
        self,
        platform: str,
        external_user_id: str,
        only_negative: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Comment]:
        """Query historical comments by an author across all monitored targets."""
        stmt = (
            select(Comment)
            .join(PlatformAuthor, Comment.author_id == PlatformAuthor.id)
            .where(
                Comment.platform == platform,
                PlatformAuthor.external_user_id == str(external_user_id),
            )
        )
        if only_negative:
            stmt = stmt.where(Comment.is_severely_negative.is_(True))

        stmt = stmt.order_by(desc(Comment.published_at), desc(Comment.ingested_at)).limit(limit).offset(offset)
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def get_stats(self) -> Dict[str, Any]:
        """Aggregate statistics across comments and platforms."""
        total_stmt = select(func.count(Comment.id))
        total = (await self.session.execute(total_stmt)).scalar() or 0

        pending_stmt = select(func.count(Comment.id)).where(Comment.llm_status == "pending")
        pending = (await self.session.execute(pending_stmt)).scalar() or 0

        flagged_stmt = select(func.count(Comment.id)).where(Comment.is_severely_negative.is_(True))
        flagged = (await self.session.execute(flagged_stmt)).scalar() or 0

        alerts_sent_stmt = select(func.count(Comment.id)).where(Comment.alert_sent.is_(True))
        alerts_sent = (await self.session.execute(alerts_sent_stmt)).scalar() or 0

        # Query actual comments grouped by platform
        platform_stmt = select(Comment.platform, func.count(Comment.id)).group_by(Comment.platform)
        platform_rows = (await self.session.execute(platform_stmt)).all()
        by_platform = {row[0]: row[1] for row in platform_rows}

        return {
            "total_comments": total,
            "pending_evaluation": pending,
            "severely_negative_flagged": flagged,
            "alerts_dispatched": alerts_sent,
            "by_platform": by_platform,
        }
