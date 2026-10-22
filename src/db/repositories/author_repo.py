"""Repository for managing PlatformAuthor profiles."""

from typing import Optional, List
from datetime import datetime, timezone
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models import PlatformAuthor


class AuthorRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, author_id: str) -> Optional[PlatformAuthor]:
        stmt = select(PlatformAuthor).where(PlatformAuthor.id == author_id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_by_platform_and_user_id(self, platform: str, external_user_id: str) -> Optional[PlatformAuthor]:
        stmt = select(PlatformAuthor).where(
            PlatformAuthor.platform == platform,
            PlatformAuthor.external_user_id == str(external_user_id)
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_by_platform_and_username(self, platform: str, username: str) -> Optional[PlatformAuthor]:
        clean_username = username.lstrip("@").lower().strip()
        stmt = select(PlatformAuthor).where(
            PlatformAuthor.platform == platform,
            PlatformAuthor.username.ilike(clean_username)
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def upsert_author(
        self,
        platform: str,
        external_user_id: str,
        username: Optional[str] = None,
        display_name: Optional[str] = None,
        profile_url: Optional[str] = None,
    ) -> PlatformAuthor:
        clean_user_id = str(external_user_id)
        clean_username = username.lstrip("@").lower().strip() if username else None

        existing = await self.get_by_platform_and_user_id(platform, clean_user_id)
        now = datetime.now(timezone.utc)

        if existing:
            existing.last_seen_at = now
            if clean_username:
                existing.username = clean_username
            if display_name:
                existing.display_name = display_name
            if profile_url:
                existing.profile_url = profile_url
            await self.session.flush()
            return existing

        author = PlatformAuthor(
            platform=platform,
            external_user_id=clean_user_id,
            username=clean_username,
            display_name=display_name,
            profile_url=profile_url,
            first_seen_at=now,
            last_seen_at=now,
            total_comments=0,
            flagged_comments_count=0,
        )
        self.session.add(author)
        await self.session.flush()
        return author

    async def increment_comment_count(self, author_id: str, is_flagged: bool = False) -> None:
        values = {"total_comments": PlatformAuthor.total_comments + 1}
        if is_flagged:
            values["flagged_comments_count"] = PlatformAuthor.flagged_comments_count + 1
        stmt = update(PlatformAuthor).where(PlatformAuthor.id == author_id).values(**values)
        await self.session.execute(stmt)
