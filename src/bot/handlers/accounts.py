"""Session and burner account management handlers for Aiogram 3."""

import html
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.enums import ParseMode
import structlog
from src.db.base import async_session_factory
from src.db.repositories.session_repo import SessionRepository

logger = structlog.get_logger(__name__)
router = Router(name="accounts_router")


@router.message(Command("add_session"))
async def cmd_add_session(message: Message):
    """Add a burner account session for Instagram, Facebook, or X.

    Usage: /add_session <platform> <account_identifier> <sessionid_or_cookie> [optional_proxy]
    Example: /add_session instagram burner_1 123456789%3Aabcd... http://user:pass@proxy:port
    """
    args = message.text.split(maxsplit=4)
    if len(args) < 4:
        await message.reply(
            "<b>Usage:</b> <code>/add_session &lt;platform&gt; &lt;username&gt; &lt;sessionid_or_cookies&gt; [proxy]</code>\n\n"
            "<b>Platforms:</b> <code>instagram</code>, <code>x</code>, <code>facebook</code>\n\n"
            "<b>Example for Instagram:</b>\n"
            "<code>/add_session instagram my_burner 69420%3AxYz12345</code>\n\n"
            "<i>(To get your Instagram sessionid: Open instagram.com on PC, press F12 -> Application -> Cookies -> copy 'sessionid')</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    platform = args[1].lower().strip()
    identifier = args[2].strip()
    session_val = args[3].strip()
    proxy = args[4].strip() if len(args) >= 5 else None

    if platform not in ["instagram", "x", "facebook"]:
        await message.reply("❌ Platform must be one of: <code>instagram</code>, <code>x</code>, <code>facebook</code>", parse_mode=ParseMode.HTML)
        return

    # Structure session data
    session_data = {"sessionid": session_val} if platform == "instagram" else session_val

    async with async_session_factory() as db_session:
        repo = SessionRepository(db_session)
        created = await repo.add_session(
            platform=platform,
            account_identifier=identifier,
            cookies=session_data,
            proxy_url=proxy,
        )
        await db_session.commit()

    await message.reply(
        f"✅ <b>Burner Session Stored & Encrypted!</b>\n\n"
        f"🌐 <b>Platform:</b> {platform.capitalize()}\n"
        f"👤 <b>Account:</b> <code>{html.escape(identifier)}</code>\n"
        f"🛡️ <b>Status:</b> Active\n"
        f"🔌 <b>Proxy:</b> {html.escape(proxy) if proxy else 'Direct connection'}\n\n"
        f"<i>The scraper worker will now use this account to paginate comments.</i>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("sessions"))
async def cmd_list_sessions(message: Message):
    """View status of all registered burner sessions."""
    async with async_session_factory() as db_session:
        repo = SessionRepository(db_session)
        sessions = await repo.list_sessions()

    if not sessions:
        await message.reply(
            "📋 <b>No burner account sessions registered.</b>\n"
            "Use <code>/add_session</code> to add one for Instagram, Facebook, or X.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = ["👥 <b>REGISTERED BURNER SESSIONS:</b>\n━━━━━━━━━━━━━━━━━━━━"]
    for s in sessions:
        status_icon = "🟢 Active" if s.status == "active" else f"⚠️ {s.status.capitalize()}"
        lines.append(
            f"• <b>{s.platform.capitalize()}</b> | <code>{html.escape(s.account_identifier)}</code>\n"
            f"  Status: {status_icon} | Errors: {s.error_count}\n"
            f"  Proxy: {html.escape(s.proxy_url) if s.proxy_url else 'None'}"
        )

    await message.reply("\n\n".join(lines), parse_mode=ParseMode.HTML)
