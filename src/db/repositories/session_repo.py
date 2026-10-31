"""Repository for managing burner ScraperSession cookies with Fernet encryption."""

import json
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone, timedelta
from cryptography.fernet import Fernet
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from src.config import settings
from src.db.models import ScraperSession


class SessionRepository:
    def __init__(self, session: AsyncSession, encryption_key: Optional[str] = None):
        self.session = session
        key = (encryption_key or settings.SESSION_ENCRYPTION_KEY).encode("utf-8")
        # Ensure key is valid Fernet 32-byte url-safe base64
        try:
            self.fernet = Fernet(key)
        except Exception:
            # Fallback to deterministic key padding for testing/safe defaults
            import base64
            padded = base64.urlsafe_b64encode(key.ljust(32, b"0")[:32])
            self.fernet = Fernet(padded)

    def encrypt_data(self, data: Any) -> str:
        """Serialize data to JSON and Fernet-encrypt it."""
        serialized = json.dumps(data)
        return self.fernet.encrypt(serialized.encode("utf-8")).decode("utf-8")

    def decrypt_data(self, encrypted_str: str) -> Any:
        """Decrypt Fernet-encrypted string and parse JSON."""
        decrypted = self.fernet.decrypt(encrypted_str.encode("utf-8"))
        return json.loads(decrypted.decode("utf-8"))

    async def get_healthy_session(self, platform: str) -> Optional[ScraperSession]:
        """Fetch the healthiest available active session not in cooldown."""
        now = datetime.now(timezone.utc)
        stmt = (
            select(ScraperSession)
            .where(
                ScraperSession.platform == platform,
                ScraperSession.status == "active",
                (ScraperSession.cooldown_until.is_(None)) | (ScraperSession.cooldown_until <= now),
            )
            .order_by(ScraperSession.last_used_at.asc().nulls_first())
            .limit(1)
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def add_session(
        self,
        platform: str,
        account_identifier: str,
        cookies: Any,
        user_agent: Optional[str] = None,
        proxy_url: Optional[str] = None,
    ) -> ScraperSession:
        """Encrypt and store or update an existing burner session."""
        encrypted_cookies = self.encrypt_data(cookies)

        stmt = select(ScraperSession).where(
            ScraperSession.platform == platform,
            ScraperSession.account_identifier == account_identifier,
        )
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing:
            existing.cookies_encrypted = encrypted_cookies
            existing.proxy_url = proxy_url
            existing.user_agent = user_agent
            existing.status = "active"
            existing.cooldown_until = None
            existing.error_count = 0
            return existing

        session_obj = ScraperSession(
            platform=platform,
            account_identifier=account_identifier,
            cookies_encrypted=encrypted_cookies,
            user_agent=user_agent,
            proxy_url=proxy_url,
            status="active",
        )
        self.session.add(session_obj)
        await self.session.flush()
        return session_obj

    async def reset_all_cooldowns(self, platform: Optional[str] = None) -> int:
        """Reset cooldown and reactivate all sessions."""
        stmt = (
            update(ScraperSession)
            .values(status="active", cooldown_until=None)
        )
        if platform:
            stmt = stmt.where(ScraperSession.platform == platform)
        res = await self.session.execute(stmt)
        return res.rowcount

    async def mark_used(self, session_id: str) -> None:
        stmt = (
            update(ScraperSession)
            .where(ScraperSession.id == session_id)
            .values(last_used_at=datetime.now(timezone.utc))
        )
        await self.session.execute(stmt)

    async def set_cooldown(self, session_id: str, minutes: int = 15) -> None:
        cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        stmt = (
            update(ScraperSession)
            .where(ScraperSession.id == session_id)
            .values(
                status="cooldown",
                cooldown_until=cooldown_until,
                error_count=ScraperSession.error_count + 1,
            )
        )
        await self.session.execute(stmt)

    async def increment_error(self, session_id: str) -> None:
        """Increment error_count on a session when authentication or scrape fails."""
        stmt = (
            update(ScraperSession)
            .where(ScraperSession.id == session_id)
            .values(error_count=ScraperSession.error_count + 1)
        )
        await self.session.execute(stmt)

    async def update_cookies(self, session_id: str, cookies: Any) -> None:
        """Persist updated/rotated cookies (e.g. rotated ct0) back to the session."""
        encrypted_cookies = self.encrypt_data(cookies)
        stmt = (
            update(ScraperSession)
            .where(ScraperSession.id == session_id)
            .values(cookies_encrypted=encrypted_cookies)
        )
        await self.session.execute(stmt)

    async def list_sessions(self, platform: Optional[str] = None) -> List[ScraperSession]:
        stmt = select(ScraperSession)
        if platform:
            stmt = stmt.where(ScraperSession.platform == platform)
        res = await self.session.execute(stmt)
        return list(res.scalars().all())
