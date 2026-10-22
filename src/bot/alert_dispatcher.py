"""Rate-limited HTML alert dispatcher for delivering high-priority alerts to admins."""

import html
import asyncio
from datetime import datetime, timezone
from typing import Optional, List
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import structlog
from src.config import settings
from src.schemas.alert import AlertPayload
from src.db.base import async_session_factory
from src.db.repositories.comment_repo import CommentRepository

logger = structlog.get_logger(__name__)

PLATFORM_ICONS = {
    "telegram": "✈️ Telegram",
    "youtube": "▶️ YouTube",
    "instagram": "📸 Instagram",
    "facebook": "📘 Facebook",
    "x": "🐦 X (Twitter)",
}


def format_alert_html(alert: AlertPayload) -> str:
    """Construct safe HTML message layout with strict entity escaping."""
    platform_name = PLATFORM_ICONS.get(alert.platform.lower(), f"🌐 {alert.platform.capitalize()}")
    source_name = html.escape(alert.source_name or "Unknown Source")
    display_name = html.escape(alert.author_display_name or "Anonymous")
    username_str = f"@{html.escape(alert.author_username)}" if alert.author_username else "No Username"
    comment_text = html.escape(alert.content_text)
    reason = html.escape(alert.llm_reason)
    user_id = html.escape(str(alert.author_external_id))
    timestamp = alert.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")

    # Deep links
    links_part = []
    if alert.post_url:
        links_part.append(f'<a href="{alert.post_url}">🔗 View Post</a>')
    if alert.comment_url:
        links_part.append(f'<a href="{alert.comment_url}">🎯 Jump to Exact Comment</a>')
    links_html = "  |  ".join(links_part) if links_part else "<i>Direct link not available</i>"

    message = (
        f"🚨 <b>SEVERE NEGATIVE COMMENT DETECTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>Platform:</b> {platform_name}\n"
        f"📢 <b>Source:</b> {source_name}\n"
        f"🔥 <b>Severity:</b> <b>{alert.severity_score}/10</b>\n\n"
        f"👤 <b>Author:</b> {display_name} ({username_str})\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n\n"
        f"💬 <b>Comment:</b>\n"
        f"<blockquote>{comment_text}</blockquote>\n\n"
        f"🧠 <b>AI Assessment:</b>\n"
        f"<i>{reason}</i>\n\n"
        f"{links_html}\n"
        f"🕒 <b>Time:</b> <code>{timestamp}</code>"
    )
    return message


def build_alert_inline_keyboard(alert: AlertPayload) -> InlineKeyboardMarkup:
    """Construct inline actions for user history investigation and false positive marking."""
    buttons = [
        [
            InlineKeyboardButton(
                text="🔍 User History",
                callback_data=f"userhist:{alert.platform}:{alert.author_external_id[:30]}",
            ),
            InlineKeyboardButton(
                text="✅ False Positive",
                callback_data=f"fp:{alert.comment_id}",
            ),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


class AlertDispatcher:
    """Manages dispatching formatted alerts to all administrators with flood protection."""

    def __init__(self, bot: Bot, min_interval_seconds: float = 1.2):
        self.bot = bot
        self.min_interval = min_interval_seconds
        self._last_sent_time = 0.0

    async def send_alert(self, alert: AlertPayload) -> bool:
        """Send a single alert to all configured admin chat IDs."""
        admin_ids = settings.ADMIN_TELEGRAM_IDS
        if not admin_ids:
            logger.warning("No ADMIN_TELEGRAM_IDS configured; alert cannot be dispatched.")
            return False

        message_text = format_alert_html(alert)
        keyboard = build_alert_inline_keyboard(alert)
        success = True

        for admin_id in admin_ids:
            # Enforce rate-limiting interval between sends
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_sent_time
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)

            try:
                await self.bot.send_message(
                    chat_id=admin_id,
                    text=message_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
                self._last_sent_time = asyncio.get_event_loop().time()
            except Exception as e:
                logger.error("Failed to send alert to admin", admin_id=admin_id, error=str(e))
                success = False

        # Mark as alert_sent in database
        if success:
            async with async_session_factory() as session:
                repo = CommentRepository(session)
                await repo.mark_alert_sent(alert.comment_id)
                await session.commit()

        return success
