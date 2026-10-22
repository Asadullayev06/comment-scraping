"""Session and Proxy Pool Manager for Instagram, Facebook, and X burner accounts."""

import json
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timezone, timedelta
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.repositories.session_repo import SessionRepository
from src.db.models import ScraperSession

logger = structlog.get_logger(__name__)


class SessionPoolManager:
    """Manages rotating burner sessions, proxy pinning, and cooldown protection."""

    def __init__(self, db_session: AsyncSession):
        self.db_session = db_session
        self.session_repo = SessionRepository(db_session)

    async def acquire_session(self, platform: str) -> Optional[Tuple[ScraperSession, Dict[str, Any], Optional[str]]]:
        """Fetch the healthiest session for a platform and decrypt its cookies/settings.

        Returns:
            Tuple of (ScraperSession, decrypted_settings_dict, proxy_url) or None
        """
        session = await self.session_repo.get_healthy_session(platform)
        if not session:
            logger.warning("No healthy burner session available for platform", platform=platform)
            return None

        try:
            settings_data = self.session_repo.decrypt_data(session.cookies_encrypted)
            await self.session_repo.mark_used(session.id)
            await self.db_session.commit()
            return session, settings_data, session.proxy_url
        except Exception as e:
            logger.error("Failed to decrypt session settings", session_id=session.id, error=str(e))
            await self.session_repo.set_cooldown(session.id, minutes=60)
            await self.db_session.commit()
            return None

    async def report_rate_limit(self, session_id: str, cooldown_minutes: int = 20) -> None:
        """Trigger temporary cooldown for a session due to HTTP 429."""
        logger.warning("Session rate-limited; placing into cooldown", session_id=session_id, cooldown_minutes=cooldown_minutes)
        await self.session_repo.set_cooldown(session_id, minutes=cooldown_minutes)
        await self.db_session.commit()

    async def report_challenge(
        self,
        session_id: str,
        error_type: str = "ChallengeRequired",
        cooldown_hours: int = 6,
    ) -> None:
        """Place an account into long cooldown when an Instagram challenge or checkpoint occurs."""
        cooldown_until = datetime.now(timezone.utc) + timedelta(hours=cooldown_hours)
        logger.warning(
            "Account challenged by Instagram anti-bot; pausing account",
            session_id=session_id,
            error=error_type,
            cooldown_hours=cooldown_hours,
        )
        session = await self.session_repo.session.get(ScraperSession, session_id)
        if session:
            session.status = "challenged"
            session.cooldown_until = cooldown_until
            session.error_count += 1
            await self.db_session.commit()
