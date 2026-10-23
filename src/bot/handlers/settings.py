"""Dashboard, statistics, and threshold settings handlers for Aiogram 3."""

from aiogram import Router, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
from aiogram.enums import ParseMode
from src.config import settings
from src.db.base import async_session_factory
from src.db.repositories.target_repo import TargetRepository
from src.db.repositories.comment_repo import CommentRepository

router = Router(name="settings_router")


def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Persistent bottom keyboard menu for instant one-tap control."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🎯 Monitored Targets"),
                KeyboardButton(text="➕ Add Target"),
            ],
            [
                KeyboardButton(text="🔍 User Lookup"),
                KeyboardButton(text="📊 Statistics"),
            ],
            [
                KeyboardButton(text="⚙️ Threshold"),
                KeyboardButton(text="ℹ️ Help"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


@router.message(Command("start"))
@router.message(Command("help"))
@router.message(F.text == "ℹ️ Help")
async def cmd_start_help(message: Message):
    """Show operational dashboard and help documentation with reply keyboard."""
    async with async_session_factory() as session:
        t_repo = TargetRepository(session)
        c_repo = CommentRepository(session)
        active_targets = len(await t_repo.get_active_targets())
        stats = await c_repo.get_stats()

    text = (
        "🛡️ <b>OPERATIVES: Multi-Platform Negative Comment Monitoring System</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>System Overview:</b>\n"
        f"• <b>Active Targets:</b> <code>{active_targets}</code>\n"
        f"• <b>Total Comments Processed:</b> <code>{stats['total_comments']}</code>\n"
        f"• <b>Severely Negative Flagged:</b> <code>{stats['severely_negative_flagged']}</code>\n"
        f"• <b>Alerts Dispatched:</b> <code>{stats['alerts_dispatched']}</code>\n"
        f"• <b>Alert Threshold:</b> <code>{settings.LLM_ALERT_SEVERITY_THRESHOLD}/10</code>\n\n"
        "<b>Available Commands:</b>\n"
        "• <code>/add_target &lt;url_or_handle&gt;</code> — Add Telegram group, channel, YouTube, IG, FB, or X post\n"
        "• <code>/targets</code> — View and manage monitored targets (pause / resume / delete)\n"
        "• <code>/lookup &lt;@username|id&gt; [all|negative]</code> — Deep scan mutual groups for user comments\n"
        "• <code>/stats</code> — Detailed ingestion and AI metrics\n"
        "• <code>/threshold &lt;1-10&gt;</code> — Set minimum severity score for alerts (default: 7)\n"
        "• <code>/help</code> — Show this guide\n\n"
        "<i>Use the buttons below for quick navigation.</i>"
    )
    await message.reply(text, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())


@router.message(Command("stats"))
@router.message(F.text == "📊 Statistics")
async def cmd_stats(message: Message):
    """Display comprehensive system metrics."""
    async with async_session_factory() as session:
        c_repo = CommentRepository(session)
        t_repo = TargetRepository(session)
        stats = await c_repo.get_stats()
        targets = await t_repo.list_all()

    breakdown = {}
    for t in targets:
        breakdown[t.platform] = breakdown.get(t.platform, 0) + t.total_comments_collected

    platform_lines = [f"  • {p.capitalize()}: <code>{count}</code>" for p, count in breakdown.items()]
    platform_str = "\n".join(platform_lines) if platform_lines else "  • No data yet"

    text = (
        "📊 <b>SYSTEM STATISTICS & METRICS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💬 <b>Total Comments Ingested:</b> <code>{stats['total_comments']}</code>\n"
        f"⏳ <b>Pending LLM Classification:</b> <code>{stats['pending_evaluation']}</code>\n"
        f"🚨 <b>Flagged Severe Negative:</b> <code>{stats['severely_negative_flagged']}</code>\n"
        f"📬 <b>Alerts Dispatched:</b> <code>{stats['alerts_dispatched']}</code>\n\n"
        "<b>Comments by Platform:</b>\n"
        f"{platform_str}\n\n"
        f"⚙️ <b>LLM Model:</b> <code>{settings.DEEPSEEK_MODEL}</code>\n"
        f"📦 <b>Batch Size:</b> <code>{settings.LLM_BATCH_SIZE}</code>\n"
        f"🎯 <b>Current Alert Threshold:</b> <code>{settings.LLM_ALERT_SEVERITY_THRESHOLD}/10</code>"
    )
    await message.reply(text, parse_mode=ParseMode.HTML)


@router.message(Command("threshold"))
@router.message(F.text == "⚙️ Threshold")
async def cmd_set_threshold(message: Message):
    """Change the alert severity threshold dynamically."""
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.reply(
            f"<b>Current Severity Threshold:</b> <code>{settings.LLM_ALERT_SEVERITY_THRESHOLD}/10</code>\n\n"
            f"To change, specify a number from 1 to 10:\n"
            f"<code>/threshold 8</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    new_val = int(args[1])
    if 1 <= new_val <= 10:
        settings.LLM_ALERT_SEVERITY_THRESHOLD = new_val
        await message.reply(
            f"✅ <b>Alert severity threshold updated to:</b> <code>{new_val}/10</code>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.reply("❌ Threshold must be between 1 and 10.", parse_mode=ParseMode.HTML)
