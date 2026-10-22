"""Aiogram 3 middleware for administrator authorization and security."""

from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User
import structlog
from src.config import settings

logger = structlog.get_logger(__name__)


class AdminAuthMiddleware(BaseMiddleware):
    """Enforces that only Telegram User IDs specified in ADMIN_TELEGRAM_IDS can use the bot."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user: User = data.get("event_from_user")

        if not user:
            return await handler(event, data)

        if not settings.is_admin(user.id):
            logger.warning("Unauthorized access attempt", user_id=user.id, username=user.username)
            # Silently reject unauthorized users
            return None

        return await handler(event, data)
