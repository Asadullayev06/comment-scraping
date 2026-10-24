"""Target management command handlers for Aiogram 3."""

import re
from typing import Optional, Tuple
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode
import structlog
from src.db.base import async_session_factory
from src.db.repositories.target_repo import TargetRepository
from src.bot.states import TargetStates

logger = structlog.get_logger(__name__)
router = Router(name="targets_router")


def detect_platform_and_id(input_str: str) -> Tuple[Optional[str], Optional[str], Optional[str], str]:
    """Parse URL or handle to determine platform, target_type, external_id, and normalized URL.

    Returns:
        (platform, target_type, external_id, clean_url)
    """
    text = input_str.strip()

    # Telegram
    if "t.me/" in text or text.startswith("@") or text.lstrip("-").isdigit():
        target_type = "telegram_chat"
        platform = "telegram"
        if text.startswith("@"):
            clean_id = text
            clean_url = f"https://t.me/{text.lstrip('@')}"
        elif "t.me/" in text:
            # Extract channel username or joinchat hash
            clean_url = text if text.startswith("http") else f"https://{text}"
            parts = text.split("t.me/")[-1].split("?")[0].split("/")[0]
            clean_id = parts
        else:
            clean_id = text
            clean_url = None
        return platform, target_type, clean_id, clean_url

    # YouTube
    if "youtube.com" in text or "youtu.be" in text:
        platform = "youtube"
        target_type = "post_url"
        match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", text)
        video_id = match.group(1) if match else text
        return platform, target_type, video_id, text

    # Instagram
    if "instagram.com" in text:
        platform = "instagram"
        target_type = "post_url"
        match = re.search(r"/(?:p|reel)/([A-Za-z0-9_-]+)", text)
        shortcode = match.group(1) if match else text
        return platform, target_type, shortcode, text

    # X / Twitter
    if "x.com" in text or "twitter.com" in text:
        platform = "x"
        target_type = "post_url"
        match = re.search(r"/status/(\d+)", text)
        tweet_id = match.group(1) if match else text
        return platform, target_type, tweet_id, text

    # Facebook
    if "facebook.com" in text or "fb.watch" in text:
        platform = "facebook"
        target_type = "post_url"
        return platform, target_type, text, text

    return None, None, None, text


@router.message(Command("add_target"))
async def cmd_add_target(message: Message):
    """Add a new monitored channel, group, or post URL."""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(
            "<b>Usage:</b> <code>/add_target &lt;url_or_handle&gt;</code>\n\n"
            "<b>Examples:</b>\n"
            "• <code>/add_target @channel_username</code> (Telegram group/channel)\n"
            "• <code>/add_target https://t.me/c/1234567890/1</code> (Telegram private group)\n"
            "• <code>/add_target https://www.youtube.com/watch?v=dQw4w9WgXcQ</code>\n"
            "• <code>/add_target https://www.instagram.com/p/C7XyZ.../</code>\n"
            "• <code>/add_target https://x.com/user/status/179...</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    raw_input = args[1].strip()
    platform, target_type, external_id, clean_url = detect_platform_and_id(raw_input)

    if not platform or not external_id:
        await message.reply("❌ <b>Could not recognize platform from URL or handle.</b>", parse_mode=ParseMode.HTML)
        return

    async with async_session_factory() as session:
        repo = TargetRepository(session)
        target = await repo.create_or_activate(
            platform=platform,
            target_type=target_type,
            external_id=external_id,
            url=clean_url,
            title_or_name=f"{platform.capitalize()} Target: {external_id}",
        )
        await session.commit()

    # Enqueue immediate scrape job via Arq
    if platform != "telegram":
        try:
            from arq import create_pool
            from arq.connections import RedisSettings
            from src.config import settings
            pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
            await pool.enqueue_job("scrape_target_job", target.id)
            await pool.aclose()
        except Exception as q_err:
            logger.debug("Could not enqueue immediate scrape job", error=str(q_err))

    session_hint = ""
    if platform in ["instagram", "x", "facebook"]:
        async with async_session_factory() as s_sess:
            from src.db.repositories.session_repo import SessionRepository
            s_repo = SessionRepository(s_sess)
            healthy = await s_repo.get_healthy_session(platform)
            if not healthy:
                session_hint = (
                    f"\n\n⚠️ <i>Note: No active burner account found for {platform.capitalize()}.\n"
                    f"To scrape comments without blocks, register a burner session with:</i>\n"
                    f"<code>/add_session {platform} &lt;username&gt; &lt;sessionid&gt;</code>"
                )

    await message.reply(
        f"✅ <b>Target Registered & Activated!</b>\n\n"
        f"🌐 <b>Platform:</b> {platform.capitalize()}\n"
        f"🎯 <b>Type:</b> <code>{target_type}</code>\n"
        f"🆔 <b>Target ID:</b> <code>{external_id}</code>\n"
        f"🔗 <b>URL:</b> {clean_url or 'N/A'}\n\n"
        f"<i>Initial scrape job enqueued.{session_hint}</i>",
        parse_mode=ParseMode.HTML,
    )


@router.message(F.text == "➕ Add Target")
async def btn_add_target(message: Message, state: FSMContext):
    """Start interactive target addition without slash commands."""
    await state.set_state(TargetStates.waiting_for_target_input)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_state")]]
    )
    await message.reply(
        "➕ <b>Add Monitored Target</b>\n\n"
        "Send the link, @username, or numeric Chat ID below:\n\n"
        "• <b>Telegram:</b> <code>@channel_name</code> or <code>-1001234567890</code>\n"
        "• <b>Instagram:</b> <code>https://www.instagram.com/p/POST_ID/</code>\n"
        "• <b>YouTube:</b> <code>https://www.youtube.com/watch?v=VIDEO_ID</code>\n"
        "• <b>X (Twitter):</b> <code>https://x.com/user/status/POST_ID</code>\n"
        "• <b>Facebook:</b> <code>https://www.facebook.com/posts/POST_ID</code>\n\n"
        "<i>(Just paste the URL directly — no slash commands needed!)</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


@router.message(TargetStates.waiting_for_target_input)
async def process_target_input(message: Message, state: FSMContext):
    """Process direct URL/handle sent by user while in add target state."""
    if not message.text:
        return

    raw_input = message.text.strip()
    platform, target_type, external_id, clean_url = detect_platform_and_id(raw_input)

    if not platform or not external_id:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_state")]]
        )
        await message.reply(
            "❌ <b>Could not recognize platform from input.</b>\n\n"
            "Please send a valid social media URL, @username, or numeric Chat ID:",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return

    await state.clear()
    async with async_session_factory() as session:
        repo = TargetRepository(session)
        target = await repo.create_or_activate(
            platform=platform,
            target_type=target_type,
            external_id=external_id,
            url=clean_url,
            title_or_name=f"{platform.capitalize()} Target: {external_id}",
        )
        await session.commit()

    # Enqueue immediate scrape job via Arq
    if platform != "telegram":
        try:
            from arq import create_pool
            from arq.connections import RedisSettings
            from src.config import settings
            pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
            await pool.enqueue_job("scrape_target_job", target.id)
            await pool.aclose()
        except Exception as q_err:
            logger.debug("Could not enqueue immediate scrape job", error=str(q_err))

    session_hint = ""
    if platform in ["instagram", "x", "facebook"]:
        async with async_session_factory() as s_sess:
            from src.db.repositories.session_repo import SessionRepository
            s_repo = SessionRepository(s_sess)
            healthy = await s_repo.get_healthy_session(platform)
            if not healthy:
                session_hint = (
                    f"\n\n⚠️ <i>Note: No active burner account found for {platform.capitalize()}.\n"
                    f"To scrape comments without blocks, register a burner session with:</i>\n"
                    f"<code>/add_session {platform} &lt;username&gt; &lt;sessionid&gt;</code>"
                )

    await message.reply(
        f"✅ <b>Target Registered & Activated!</b>\n\n"
        f"🌐 <b>Platform:</b> {platform.capitalize()}\n"
        f"🎯 <b>Type:</b> <code>{target_type}</code>\n"
        f"🆔 <b>Target ID:</b> <code>{external_id}</code>\n"
        f"🔗 <b>URL:</b> {clean_url or 'N/A'}\n\n"
        f"<i>Initial scrape job enqueued.{session_hint}</i>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("targets"))
@router.message(F.text == "🎯 Monitored Targets")
async def cmd_list_targets(message: Message, state: FSMContext):
    """List all registered targets with interactive management buttons."""
    await state.clear()
    """List all registered targets with interactive management buttons."""
    async with async_session_factory() as session:
        repo = TargetRepository(session)
        targets = await repo.list_all(limit=20)

    if not targets:
        await message.reply("📋 <b>No monitored targets registered yet.</b>\nUse <code>/add_target</code> to add one.", parse_mode=ParseMode.HTML)
        return

    for t in targets:
        status_icon = "🟢 Active" if t.is_active else "⏸ Paused"
        toggle_label = "⏸ Pause" if t.is_active else "▶️ Resume"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔄 Scrape Now", callback_data=f"tgt:scrape:{t.id}"),
                    InlineKeyboardButton(text=toggle_label, callback_data=f"tgt:toggle:{t.id}"),
                    InlineKeyboardButton(text="🗑 Delete", callback_data=f"tgt:del:{t.id}"),
                ]
            ]
        )
        text = (
            f"🎯 <b>{t.title_or_name or t.external_id}</b>\n"
            f"🌐 <b>Platform:</b> {t.platform.capitalize()} | <b>Status:</b> {status_icon}\n"
            f"💬 <b>Collected Comments:</b> {t.total_comments_collected}\n"
            f"🔗 <b>Link:</b> {t.url or 'N/A'}"
        )
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=keyboard, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("tgt:scrape:"))
async def cb_scrape_target(callback: CallbackQuery):
    """Manually trigger immediate scrape job for a target."""
    target_id = callback.data.split("tgt:scrape:")[-1]
    
    # Check if target exists and if session is required
    async with async_session_factory() as session:
        t_repo = TargetRepository(session)
        target = await t_repo.get_by_id(target_id)
        if not target:
            await callback.answer("Target not found.", show_alert=True)
            return

        if target.platform in ["instagram", "x", "facebook"]:
            from src.db.repositories.session_repo import SessionRepository
            s_repo = SessionRepository(session)
            healthy = await s_repo.get_healthy_session(target.platform)
            if not healthy:
                all_sessions = await s_repo.list_sessions(platform=target.platform)
                if not all_sessions:
                    await callback.answer(
                        f"⚠️ No burner account found for {target.platform.capitalize()}!\n"
                        f"Use /add_session {target.platform} <name> <sessionid> first.",
                        show_alert=True,
                    )
                    return
                else:
                    await callback.answer(
                        f"⚠️ Burner account for {target.platform.capitalize()} is cooling down.\n"
                        f"Click /reset_cooldowns or wait a few minutes.",
                        show_alert=True,
                    )
                    return

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        from src.config import settings
        pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
        await pool.enqueue_job("scrape_target_job", target_id)
        await pool.aclose()
        await callback.answer("🔄 Scrape job enqueued! Ingesting comments...", show_alert=True)
    except Exception as e:
        await callback.answer(f"Failed to trigger: {str(e)[:50]}", show_alert=True)


@router.callback_query(F.data.startswith("tgt:toggle:"))
async def cb_toggle_target(callback: CallbackQuery):
    target_id = callback.data.split("tgt:toggle:")[-1]
    async with async_session_factory() as session:
        repo = TargetRepository(session)
        t = await repo.get_by_id(target_id)
        if t:
            new_state = not t.is_active
            await repo.set_active(target_id, new_state)
            await session.commit()
            status_text = "Active" if new_state else "Paused"
            await callback.answer(f"Target set to {status_text}!")
            # Update message text
            await callback.message.edit_reply_markup(
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="⏸ Pause" if new_state else "▶️ Resume",
                                callback_data=f"tgt:toggle:{t.id}",
                            ),
                            InlineKeyboardButton(text="🗑 Delete", callback_data=f"tgt:del:{t.id}"),
                        ]
                    ]
                )
            )
        else:
            await callback.answer("Target not found.")


@router.callback_query(F.data.startswith("tgt:del:"))
async def cb_delete_target(callback: CallbackQuery):
    target_id = callback.data.split("tgt:del:")[-1]
    async with async_session_factory() as session:
        repo = TargetRepository(session)
        await repo.delete_by_id(target_id)
        await session.commit()
    await callback.answer("Target deleted.")
    await callback.message.delete()


@router.message(F.forward_from_chat)
async def handle_forwarded_group_message(message: Message):
    """Automatically detect private group/channel from any forwarded message."""
    import html
    chat = message.forward_from_chat
    chat_id = str(chat.id)
    chat_title = chat.title or chat.username or f"Private Chat {chat_id}"
    clean_url = f"https://t.me/{chat.username}" if chat.username else None

    # Sanitize callback data (max 64 bytes)
    callback_data = f"fwd_add:{chat_id}"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Add to Monitored Targets",
                    callback_data=callback_data,
                )
            ]
        ]
    )

    await message.reply(
        f"🎯 <b>Detected Group from Forwarded Message!</b>\n\n"
        f"📢 <b>Title:</b> {html.escape(chat_title)}\n"
        f"🆔 <b>Numeric Chat ID:</b> <code>{chat_id}</code>\n"
        f"🌐 <b>Chat Type:</b> <code>{chat.type}</code>\n\n"
        f"Click the button below to start monitoring this group:",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("fwd_add:"))
async def cb_add_forwarded_target(callback: CallbackQuery):
    """One-click add target from forwarded message button."""
    chat_id = callback.data.split("fwd_add:")[-1]
    title = "Monitored Private Group"
    if callback.message and callback.message.text:
        # Extract title from text if present
        for line in callback.message.text.splitlines():
            if "Title:" in line:
                title = line.replace("Title:", "").strip()

    async with async_session_factory() as session:
        repo = TargetRepository(session)
        target = await repo.create_or_activate(
            platform="telegram",
            target_type="telegram_chat",
            external_id=chat_id,
            url=None,
            title_or_name=title,
        )
        await session.commit()

    await callback.answer("Target added and activated! ✅")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(
        f"✅ <b>Successfully added '{title}' (ID: <code>{chat_id}</code>) to monitored targets!</b>",
        parse_mode=ParseMode.HTML,
    )
