"""User History Lookup command handlers for Telegram deep scanning with interactive FSM support."""

import re
import html
from typing import Optional, List, Dict, Any
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode
import structlog
from src.db.base import async_session_factory
from src.db.repositories.author_repo import AuthorRepository
from src.db.repositories.comment_repo import CommentRepository
from src.bot.states import LookupStates

logger = structlog.get_logger(__name__)
router = Router(name="lookup_router")

# Shared MTProto collector instance reference
_mtproto_collector = None


def set_mtproto_collector_instance(collector):
    global _mtproto_collector
    _mtproto_collector = collector


def clean_filename(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)


def generate_html_report(author_name: str, user_id: str, mode: str, comments: List[Dict[str, Any]]) -> str:
    """Generate self-contained HTML report document for extensive comment histories."""
    rows = []
    for c in comments:
        severity_badge = f'<span style="color: {"red" if c["severity_score"] >= 7 else "gray"}; font-weight: bold;">{c["severity_score"]}/10</span>'
        link_html = f'<a href="{c["url"]}" target="_blank">View</a>' if c.get("url") else "N/A"
        rows.append(
            f"""
            <tr>
                <td>{html.escape(c["published_at"])}</td>
                <td>{html.escape(c["source"] or "Unknown")}</td>
                <td>{severity_badge}</td>
                <td>{html.escape(c["text"])}</td>
                <td><i>{html.escape(c.get("llm_reason") or "")}</i></td>
                <td>{link_html}</td>
            </tr>
            """
        )

    tbody = "\n".join(rows)
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>User History Report - {html.escape(author_name)} ({user_id})</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 30px; background: #f8f9fa; }}
        h1 {{ color: #212529; }}
        .header {{ background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 20px; }}
        table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        th, td {{ padding: 12px 15px; text-align: left; border-bottom: 1px solid #dee2e6; }}
        th {{ background: #007bff; color: white; }}
        tr:hover {{ background-color: #f1f3f5; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>User Comment History Report</h1>
        <p><strong>User:</strong> {html.escape(author_name)} | <strong>ID:</strong> {user_id} | <strong>Mode:</strong> {mode.upper()}</p>
        <p><strong>Total Comments Found:</strong> {len(comments)}</p>
    </div>
    <table>
        <thead>
            <tr>
                <th style="width: 140px;">Date (UTC)</th>
                <th style="width: 180px;">Source / Group</th>
                <th style="width: 80px;">Severity</th>
                <th>Comment Text</th>
                <th>AI Reason</th>
                <th style="width: 70px;">Link</th>
            </tr>
        </thead>
        <tbody>
            {tbody}
        </tbody>
    </table>
</body>
</html>"""


async def execute_lookup(reply_target: Message, target_input: str, filter_mode: str = "negative"):
    """Core execution logic for searching user comment history across mutual groups."""
    progress_msg = await reply_target.reply(
        f"🔎 <b>Searching mutual groups & channels for</b> <code>{html.escape(target_input)}</code>...\n"
        f"<i>Mode: {filter_mode.upper()}</i>",
        parse_mode=ParseMode.HTML,
    )

    comments: List[Dict[str, Any]] = []
    author_info = None

    # Try live MTProto scan if collector is active
    if _mtproto_collector and _mtproto_collector.client and _mtproto_collector.client.is_connected():
        try:
            author, comments, total_scanned = await _mtproto_collector.lookup_user_history(
                username_or_id=target_input,
                filter_mode=filter_mode,
            )
            author_info = author
        except Exception as scan_err:
            logger.error("MTProto lookup failed", error=str(scan_err))

    # If MTProto scan returned nothing or not connected, query local database
    if not comments:
        async with async_session_factory() as session:
            author_repo = AuthorRepository(session)
            comment_repo = CommentRepository(session)

            clean = target_input.lstrip("@")
            if clean.isdigit():
                author = await author_repo.get_by_platform_and_user_id("telegram", clean)
            else:
                author = await author_repo.get_by_platform_and_username("telegram", clean)

            if author:
                author_info = author
                db_comments = await comment_repo.get_user_comments(
                    platform="telegram",
                    external_user_id=author.external_user_id,
                    only_negative=(filter_mode == "negative"),
                    limit=100,
                )
                comments = [
                    {
                        "id": c.id,
                        "text": c.content_text,
                        "source": c.source_name,
                        "url": c.comment_url,
                        "published_at": c.published_at.strftime("%Y-%m-%d %H:%M:%S") if c.published_at else "Unknown",
                        "severity_score": c.severity_score or 0,
                        "is_severely_negative": c.is_severely_negative,
                        "llm_reason": c.llm_reason,
                    }
                    for c in db_comments
                ]

    if not comments:
        await progress_msg.edit_text(
            f"ℹ️ <b>No {'negative ' if filter_mode == 'negative' else ''}comments found for</b> <code>{html.escape(target_input)}</code>.\n\n"
            f"<i>Note: If live MTProto userbot is not yet connected, only comments already archived in the database are queried.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Build summary header
    author_name = author_info.display_name if author_info else target_input
    user_id_str = author_info.external_user_id if author_info else "N/A"
    username_str = f"@{author_info.username}" if author_info and author_info.username else "No Username"

    header = (
        f"👤 <b>User History:</b> {html.escape(author_name)} ({html.escape(username_str)})\n"
        f"🆔 <b>User ID:</b> <code>{html.escape(user_id_str)}</code>\n"
        f"📊 <b>Matching Comments Found:</b> <b>{len(comments)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    # Render up to 5 items directly in chat
    body_items = []
    for idx, c in enumerate(comments[:5], 1):
        link_str = f' (<a href="{c["url"]}">Jump to Msg</a>)' if c.get("url") else ""
        item_text = (
            f"<b>{idx}. [{html.escape(c['source'] or 'Group')}]</b>{link_str}\n"
            f"🕒 <i>{c['published_at']}</i> | 🔥 <b>Severity:</b> {c['severity_score']}/10\n"
            f"<blockquote>{html.escape(c['text'][:300])}{'...' if len(c['text']) > 300 else ''}</blockquote>\n"
        )
        body_items.append(item_text)

    footer = ""
    if len(comments) > 5:
        footer = f"\n<i>...and {len(comments) - 5} more comments included in attached report file below.</i>"

    await progress_msg.edit_text(header + "\n".join(body_items) + footer, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    # If >5 comments, also send full HTML report as a file
    if len(comments) > 5:
        html_content = generate_html_report(author_name, user_id_str, filter_mode, comments)
        file_bytes = html_content.encode("utf-8")
        filename = f"user_history_{clean_filename(target_input)}_{filter_mode}.html"
        await reply_target.answer_document(
            document=BufferedInputFile(file_bytes, filename=filename),
            caption=f"📄 Full history report for <b>{html.escape(author_name)}</b> ({len(comments)} comments)",
            parse_mode=ParseMode.HTML,
        )


@router.message(F.text == "🔍 User Lookup")
async def btn_user_lookup(message: Message, state: FSMContext):
    """Start interactive lookup mode without typing slash commands."""
    await state.set_state(LookupStates.waiting_for_user_input)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_state")]]
    )
    await message.reply(
        "🔍 <b>Telegram User History Lookup</b>\n\n"
        "Send the person's Telegram <b>@username</b> or numeric <b>User ID</b> below:\n\n"
        "<i>Examples: <code>@D0nlyor</code> or <code>1140366830</code></i>\n"
        "<i>(Just type the username or ID directly — no slash commands needed!)</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


@router.message(LookupStates.waiting_for_user_input)
async def process_lookup_user_input(message: Message, state: FSMContext):
    """Handle username or ID provided by user in lookup state."""
    if not message.text:
        return

    text = message.text.strip()
    parts = text.split()
    target_input = parts[0]

    # If user already typed mode like "@username negative" or "@username all"
    if len(parts) >= 2 and parts[1].lower() in ["negative", "neg", "all"]:
        filter_mode = "negative" if parts[1].lower() in ["negative", "neg"] else "all"
        await state.clear()
        await execute_lookup(message, target_input, filter_mode)
        return

    # Otherwise, present one-tap mode selection buttons!
    await state.update_data(target_input=target_input)
    await state.set_state(LookupStates.waiting_for_mode)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🚨 Negative / Hostile Only", callback_data="lkp_mode:negative"),
                InlineKeyboardButton(text="📜 All Comments", callback_data="lkp_mode:all"),
            ],
            [
                InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_state"),
            ],
        ]
    )
    await message.reply(
        f"👤 <b>Target Selected:</b> <code>{html.escape(target_input)}</code>\n\n"
        f"Choose what comments to search for:",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("lkp_mode:"))
async def cb_execute_lookup_mode(callback: CallbackQuery, state: FSMContext):
    """Execute search based on tapped mode button."""
    mode = callback.data.split(":")[-1]
    data = await state.get_data()
    target_input = data.get("target_input")
    await state.clear()

    if not target_input:
        await callback.answer("Session expired. Please tap '🔍 User Lookup' again.", show_alert=True)
        return

    await callback.answer(f"Searching mode: {mode.upper()}...")
    await callback.message.edit_reply_markup(reply_markup=None)
    await execute_lookup(callback.message, target_input, filter_mode=mode)


@router.message(Command("lookup"))
async def cmd_lookup_user(message: Message, state: FSMContext):
    """Traditional /lookup command handler."""
    await state.clear()
    args = message.text.split()
    if len(args) < 2:
        await btn_user_lookup(message, state)
        return

    target_input = args[1].strip()
    filter_mode = "all"
    if len(args) >= 3 and args[2].lower() in ["negative", "neg", "flagged"]:
        filter_mode = "negative"

    await execute_lookup(message, target_input, filter_mode)


@router.callback_query(F.data.startswith("userhist:"))
async def cb_user_history(callback: CallbackQuery):
    """Inline button handler from alert messages to jump directly to user history."""
    parts = callback.data.split(":")
    if len(parts) >= 3:
        platform = parts[1]
        user_id = parts[2]
        if platform == "telegram":
            await callback.answer("Running user history lookup...")
            await execute_lookup(callback.message, user_id, filter_mode="negative")
        else:
            async with async_session_factory() as session:
                c_repo = CommentRepository(session)
                comments = await c_repo.get_user_comments(platform, user_id, only_negative=True, limit=10)
            await callback.answer(f"Found {len(comments)} previous flagged comments for user in DB.", show_alert=True)


@router.callback_query(F.data.startswith("fp:"))
async def cb_mark_false_positive(callback: CallbackQuery):
    """Mark a flagged comment as a false positive."""
    comment_id = callback.data.split("fp:")[-1]
    async with async_session_factory() as session:
        c_repo = CommentRepository(session)
        await c_repo.update_llm_verdict(
            comment_id=comment_id,
            is_on_topic=False,
            is_severely_negative=False,
            severity_score=0,
            llm_reason="Marked as False Positive by Operator",
            llm_status="false_positive",
        )
        await session.commit()
    await callback.answer("Marked as False Positive ✅")
    await callback.message.edit_reply_markup(reply_markup=None)
