"""Dashboard, statistics, and threshold settings handlers for Aiogram 3."""

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
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


def build_threshold_inline_keyboard(current_val: int) -> InlineKeyboardMarkup:
    """Inline interactive buttons 1-10 to change threshold without typing."""
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅ ' if i == current_val else ''}{i}",
                callback_data=f"set_thresh:{i}",
            )
            for i in range(1, 6)
        ],
        [
            InlineKeyboardButton(
                text=f"{'✅ ' if i == current_val else ''}{i}",
                callback_data=f"set_thresh:{i}",
            )
            for i in range(6, 11)
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "cancel_state")
async def cb_cancel_state(callback: CallbackQuery, state: FSMContext):
    """Universal cancel button handler."""
    await state.clear()
    await callback.answer("Cancelled")
    await callback.message.edit_text("❌ <b>Action cancelled.</b>", parse_mode=ParseMode.HTML)


@router.message(Command("start"))
@router.message(Command("help"))
@router.message(F.text == "ℹ️ Help")
async def cmd_start_help(message: Message, state: FSMContext):
    """Show operational dashboard and help documentation with reply keyboard."""
    await state.clear()
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
        "<b>Navigation:</b>\n"
        "Tap any button below to manage targets, run user history lookups, or view stats — no typing required!"
    )
    await message.reply(text, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())


@router.message(Command("stats"))
@router.message(F.text == "📊 Statistics")
async def cmd_stats(message: Message, state: FSMContext):
    """Display comprehensive system metrics."""
    await state.clear()
    async with async_session_factory() as session:
        c_repo = CommentRepository(session)
        t_repo = TargetRepository(session)
        stats = await c_repo.get_stats()
        targets = await t_repo.list_all()

    breakdown = stats.get("by_platform", {})
    platform_lines = [f"  • {p.capitalize()}: <code>{count}</code>" for p, count in sorted(breakdown.items())]
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
async def cmd_set_threshold(message: Message, state: FSMContext):
    """Change the alert severity threshold dynamically via interactive buttons."""
    await state.clear()
    args = message.text.split() if message.text else []

    # If user provided a numeric argument with /threshold
    if len(args) >= 2 and args[1].isdigit():
        new_val = int(args[1])
        if 1 <= new_val <= 10:
            settings.LLM_ALERT_SEVERITY_THRESHOLD = new_val
            await message.reply(
                f"✅ <b>Alert severity threshold updated to:</b> <code>{new_val}/10</code>",
                parse_mode=ParseMode.HTML,
            )
            return

    current = settings.LLM_ALERT_SEVERITY_THRESHOLD
    keyboard = build_threshold_inline_keyboard(current)
    await message.reply(
        f"⚙️ <b>Alert Severity Threshold</b>\n\n"
        f"Current Threshold: <b>{current}/10</b>\n"
        f"<i>(Only comments with severity score ≥ threshold trigger alerts)</i>\n\n"
        f"Tap a number below to change the threshold immediately:",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("set_thresh:"))
async def cb_set_threshold(callback: CallbackQuery):
    """Handle one-tap threshold adjustment buttons."""
    val = int(callback.data.split(":")[-1])
    settings.LLM_ALERT_SEVERITY_THRESHOLD = val
    await callback.answer(f"Threshold set to {val}/10! ✅")
    keyboard = build_threshold_inline_keyboard(val)
    await callback.message.edit_text(
        f"✅ <b>Alert Severity Threshold Updated!</b>\n\n"
        f"Current Threshold: <b>{val}/10</b>\n"
        f"<i>(Only comments with severity score ≥ {val} will trigger alerts)</i>\n\n"
        f"Tap another number below if you wish to adjust it again:",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
