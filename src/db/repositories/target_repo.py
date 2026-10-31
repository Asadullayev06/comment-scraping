"""Repository for managing MonitoredTarget records."""

from typing import List, Optional
from datetime import datetime, timezone
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models import MonitoredTarget


class TargetRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, target_id: str) -> Optional[MonitoredTarget]:
        stmt = select(MonitoredTarget).where(MonitoredTarget.id == target_id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_by_platform_and_external_id(self, platform: str, external_id: str) -> Optional[MonitoredTarget]:
        stmt = select(MonitoredTarget).where(
            MonitoredTarget.platform == platform,
            MonitoredTarget.external_id == str(external_id)
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_active_targets(self, platform: Optional[str] = None) -> List[MonitoredTarget]:
        stmt = select(MonitoredTarget).where(MonitoredTarget.is_active.is_(True))
        if platform:
            stmt = stmt.where(MonitoredTarget.platform == platform)
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def list_all(self, limit: int = 100, offset: int = 0) -> List[MonitoredTarget]:
        stmt = select(MonitoredTarget).order_by(MonitoredTarget.created_at.desc()).limit(limit).offset(offset)
        res = await self.session.execute(stmt)
        return list(res.scalars().all())

    async def create_or_activate(
        self,
        platform: str,
        target_type: str,
        external_id: str,
        url: Optional[str] = None,
        title_or_name: Optional[str] = None,
        poll_interval_seconds: int = 300,
        is_active: bool = True,
    ) -> MonitoredTarget:
        existing = await self.get_by_platform_and_external_id(platform, external_id)
        if existing:
            if is_active:
                existing.is_active = True
            if url:
                existing.url = url
            if title_or_name:
                existing.title_or_name = title_or_name
            await self.session.flush()
            return existing

        target = MonitoredTarget(
            platform=platform,
            target_type=target_type,
            external_id=str(external_id),
            url=url,
            title_or_name=title_or_name,
            poll_interval_seconds=poll_interval_seconds,
            is_active=is_active,
        )
        self.session.add(target)
        await self.session.flush()
        return target

    async def set_active(self, target_id: str, is_active: bool) -> bool:
        stmt = update(MonitoredTarget).where(MonitoredTarget.id == target_id).values(is_active=is_active)
        res = await self.session.execute(stmt)
        return res.rowcount > 0

    async def update_cursor(
        self,
        target_id: str,
        cursor: Optional[str] = None,
        increment_comments: int = 0
    ) -> None:
        values = {"last_scraped_at": datetime.now(timezone.utc)}
        if cursor is not None:
            values["last_cursor"] = cursor
        if increment_comments > 0:
            values["total_comments_collected"] = MonitoredTarget.total_comments_collected + increment_comments

        stmt = update(MonitoredTarget).where(MonitoredTarget.id == target_id).values(**values)
        await self.session.execute(stmt)

    async def delete_by_id(self, target_id: str) -> bool:
        stmt = delete(MonitoredTarget).where(MonitoredTarget.id == target_id)
        res = await self.session.execute(stmt)
        return res.rowcount > 0
