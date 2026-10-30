"""Telethon MTProto Collector & Mutual Group User History Scanner.

Provides:
1. Real-time streaming of messages from monitored groups and channel discussion chats (without admin access).
2. Deep links for comments on channel posts and group messages.
3. User history deep lookup across mutual groups and monitored chats.
"""

import asyncio
from typing import AsyncIterator, List, Optional, Tuple, Dict, Any, Union
from datetime import datetime, timezone
import structlog
from telethon import TelegramClient, events
from telethon.tl import functions, types
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, ChannelPrivateError, UserNotMutualContactError
from src.config import settings
from src.collectors.base import BaseCollector
from src.schemas.comment import NormalizedComment
from src.db.base import async_session_factory
from src.db.models import MonitoredTarget, PlatformAuthor
from src.db.repositories.target_repo import TargetRepository
from src.db.repositories.author_repo import AuthorRepository
from src.db.repositories.comment_repo import CommentRepository
from src.llm.processor import MicroBatchProcessor

logger = structlog.get_logger(__name__)


def clean_channel_id(chat_id: int) -> int:
    """Strip Telegram's -100 prefix to construct valid web deep links (t.me/c/<clean_id>/<msg_id>)."""
    s = str(chat_id)
    if s.startswith("-100"):
        return int(s[4:])
    elif s.startswith("-"):
        return int(s[1:])
    return chat_id


def build_telegram_message_url(
    chat_username: Optional[str],
    chat_id: int,
    message_id: int,
    channel_post_id: Optional[int] = None,
    channel_username: Optional[str] = None,
) -> str:
    """Generate exact deep links for channel post comments or group messages."""
    # If this is a comment under a broadcast channel post
    if channel_post_id:
        if channel_username:
            return f"https://t.me/{channel_username}/{channel_post_id}?comment={message_id}"
        return f"https://t.me/c/{clean_channel_id(chat_id)}/{channel_post_id}?comment={message_id}"

    # Regular group/supergroup message
    if chat_username:
        return f"https://t.me/{chat_username}/{message_id}"
    return f"https://t.me/c/{clean_channel_id(chat_id)}/{message_id}"


class TelegramMTProtoCollector(BaseCollector):
    """Passive MTProto observer and user history scanner."""

    platform = "telegram"

    def __init__(
        self,
        api_id: Optional[int] = None,
        api_hash: Optional[str] = None,
        session_name_or_string: Optional[str] = None,
        batch_processor: Optional[MicroBatchProcessor] = None,
    ):
        self.api_id = api_id or settings.TELEGRAM_API_ID
        self.api_hash = api_hash or settings.TELEGRAM_API_HASH
        
        session_input = session_name_or_string or settings.TELEGRAM_SESSION_STRING or settings.TELEGRAM_SESSION_NAME
        if settings.TELEGRAM_SESSION_STRING:
            self.session = StringSession(settings.TELEGRAM_SESSION_STRING)
        else:
            self.session = session_input

        self.client: Optional[TelegramClient] = None
        self.batch_processor = batch_processor or MicroBatchProcessor()
        self._is_running = False
        self._monitored_chat_ids: Dict[int, Dict[str, Any]] = {}  # chat_id -> metadata
        self._linked_discussion_to_channel: Dict[int, int] = {}  # discussion_group_id -> broadcast_channel_id

    async def start(self) -> None:
        """Start the Telethon client and register event handlers."""
        if not self.api_id or not self.api_hash:
            logger.warning("Telegram API ID or Hash not configured. MTProto collector not started.")
            return

        logger.info("Starting Telegram MTProto Userbot Client...")
        self.client = TelegramClient(self.session, self.api_id, self.api_hash)
        await self.client.connect()

        if not await self.client.is_user_authorized():
            logger.warning(
                "Telegram Userbot is not authorized! Run `python scripts/init_telegram_session.py` to authenticate."
            )
            return

        me = await self.client.get_me()
        logger.info("Telegram Userbot authorized successfully", user_id=me.id, username=me.username)

        # Preload active targets from DB
        await self.refresh_monitored_chats()

        # Attach real-time event listener
        self.client.add_event_handler(self._on_new_message, events.NewMessage())
        self._is_running = True
        logger.info("Telegram real-time listener active", monitored_chats=list(self._monitored_chat_ids.keys()))

    async def stop(self) -> None:
        """Disconnect the Telethon client."""
        self._is_running = False
        if self.client and self.client.is_connected():
            await self.client.disconnect()
            logger.info("Telegram MTProto Userbot disconnected.")

    async def refresh_monitored_chats(self) -> None:
        """Reload active monitored Telegram targets and resolve linked discussion groups."""
        async with async_session_factory() as session:
            repo = TargetRepository(session)
            targets = await repo.get_active_targets(platform="telegram")

        chat_map = {}
        for t in targets:
            try:
                # Target external_id can be numeric string or username
                raw_id = t.external_id
                target_entity = None
                if raw_id.startswith("@") or not raw_id.lstrip("-").isdigit():
                    target_entity = await self.client.get_entity(raw_id)
                    chat_id = target_entity.id
                else:
                    chat_id = int(raw_id)
                    target_entity = await self.client.get_entity(chat_id)

                full_chat_id = chat_id
                # Supergroups/channels in Telethon usually have -100 prefix or peer id
                peer_id = getattr(target_entity, "id", chat_id)
                if isinstance(target_entity, types.Channel):
                    full_chat_id = int(f"-100{peer_id}") if not str(peer_id).startswith("-100") else peer_id

                chat_map[full_chat_id] = {
                    "target_id": t.id,
                    "title": getattr(target_entity, "title", t.title_or_name),
                    "username": getattr(target_entity, "username", None),
                    "is_broadcast": getattr(target_entity, "broadcast", False),
                }

                # If it's a broadcast channel with comments, discover its linked discussion chat!
                if getattr(target_entity, "broadcast", False):
                    try:
                        full = await self.client(functions.channels.GetFullChannelRequest(channel=target_entity))
                        linked_id = full.full_chat.linked_chat_id
                        if linked_id:
                            full_linked_id = int(f"-100{linked_id}") if not str(linked_id).startswith("-100") else linked_id
                            self._linked_discussion_to_channel[full_linked_id] = full_chat_id
                            chat_map[full_linked_id] = {
                                "target_id": t.id,
                                "title": f"Comments on {chat_map[full_chat_id]['title']}",
                                "username": None,
                                "is_broadcast": False,
                                "parent_channel_id": full_chat_id,
                                "parent_channel_username": chat_map[full_chat_id]["username"],
                            }
                            logger.info(
                                "Discovered linked discussion group for channel",
                                channel=chat_map[full_chat_id]["title"],
                                linked_chat_id=full_linked_id,
                            )
                    except Exception as disc_err:
                        logger.warning("Could not resolve linked discussion group", error=str(disc_err))

            except Exception as e:
                logger.error("Failed to resolve monitored chat entity", target_id=t.id, external_id=t.external_id, error=str(e))

        self._monitored_chat_ids = chat_map

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        """Handle incoming real-time Telegram messages without admin privileges."""
        try:
            message = event.message
            if not message or not message.text:
                return  # Skip media-only or service messages

            chat = await event.get_chat()
            if not chat:
                return

            # Check if this chat is in our monitored set (or is a linked discussion group)
            chat_id = chat.id
            full_chat_id = int(f"-100{chat_id}") if not str(chat_id).startswith("-") else chat_id

            meta = self._monitored_chat_ids.get(full_chat_id) or self._monitored_chat_ids.get(chat_id)
            if not meta:
                return  # Not a monitored target

            # Do not process messages sent by our own userbot account
            sender = await event.get_sender()
            me = await self.client.get_me()
            if sender and sender.id == me.id:
                return

            sender_id = str(sender.id) if sender else "unknown"
            sender_username = getattr(sender, "username", None)
            sender_name = None
            if sender:
                sender_name = f"{getattr(sender, 'first_name', '')} {getattr(sender, 'last_name', '')}".strip() or sender_username

            chat_title = meta.get("title") or getattr(chat, "title", "Telegram Chat")
            chat_username = meta.get("username") or getattr(chat, "username", None)

            # Determine post URL and comment deep link
            parent_channel_id = meta.get("parent_channel_id")
            parent_channel_user = meta.get("parent_channel_username")
            channel_post_id = None
            if parent_channel_id and message.reply_to:
                channel_post_id = message.reply_to.reply_to_top_id or message.reply_to.reply_to_msg_id

            comment_url = build_telegram_message_url(
                chat_username=chat_username,
                chat_id=chat_id,
                message_id=message.id,
                channel_post_id=channel_post_id,
                channel_username=parent_channel_user,
            )

            post_url = None
            if channel_post_id:
                post_url = build_telegram_message_url(
                    chat_username=parent_channel_user,
                    chat_id=parent_channel_id,
                    message_id=channel_post_id,
                )

            external_comment_id = f"tg_{chat_id}_{message.id}"

            # Ingest into DB
            async with async_session_factory() as session:
                author_repo = AuthorRepository(session)
                comment_repo = CommentRepository(session)

                author = await author_repo.upsert_author(
                    platform="telegram",
                    external_user_id=sender_id,
                    username=sender_username,
                    display_name=sender_name,
                    profile_url=f"https://t.me/{sender_username}" if sender_username else None,
                )

                created_comment = await comment_repo.insert_single(
                    platform="telegram",
                    external_comment_id=external_comment_id,
                    content_text=message.text,
                    target_id=meta.get("target_id"),
                    author_id=author.id,
                    parent_external_id=str(channel_post_id) if channel_post_id else None,
                    source_name=chat_title,
                    post_url=post_url,
                    comment_url=comment_url,
                    published_at=message.date.replace(tzinfo=timezone.utc) if message.date else datetime.now(timezone.utc),
                )
                await session.commit()

                # If inserted successfully, process through LLM micro-batcher
                if created_comment:
                    await self.batch_processor.process_batch(session)

        except Exception as e:
            logger.error("Error processing incoming Telegram message", error=str(e), exc_info=True)

    async def fetch_comments(
        self,
        target: MonitoredTarget,
        cursor: Optional[str] = None,
        limit: int = 100,
    ) -> AsyncIterator[NormalizedComment]:
        """Fetch historical comments for a group or channel discussion thread."""
        if not self.client or not self.client.is_connected():
            return

        try:
            entity = await self.client.get_entity(int(target.external_id) if target.external_id.lstrip("-").isdigit() else target.external_id)
            chat_to_iter = entity
            if getattr(entity, "broadcast", False):
                try:
                    full = await self.client(functions.channels.GetFullChannelRequest(channel=entity))
                    linked_id = full.full_chat.linked_chat_id
                    if linked_id:
                        full_linked_id = int(f"-100{linked_id}") if not str(linked_id).startswith("-100") else linked_id
                        chat_to_iter = await self.client.get_entity(full_linked_id)
                except Exception:
                    pass

            min_id = int(cursor) if cursor and cursor.isdigit() else 0

            count = 0
            async for message in self.client.iter_messages(chat_to_iter, limit=limit, min_id=min_id):
                if not message or not message.text:
                    continue

                sender = await message.get_sender()
                sender_id = str(sender.id) if sender else "unknown"
                sender_username = getattr(sender, "username", None)
                sender_name = f"{getattr(sender, 'first_name', '')} {getattr(sender, 'last_name', '')}".strip() if sender else None

                url = build_telegram_message_url(
                    chat_username=getattr(chat_to_iter, "username", None),
                    chat_id=getattr(chat_to_iter, "id", 0),
                    message_id=message.id,
                )

                yield NormalizedComment(
                    platform="telegram",
                    external_comment_id=f"tg_{chat_to_iter.id}_{message.id}",
                    target_external_id=str(entity.id),
                    source_name=getattr(entity, "title", target.title_or_name),
                    comment_url=url,
                    author_external_id=sender_id,
                    author_username=sender_username,
                    author_display_name=sender_name,
                    content_text=message.text,
                    published_at=message.date.replace(tzinfo=timezone.utc) if message.date else datetime.now(timezone.utc),
                )
                count += 1
        except Exception as err:
            logger.error("Failed to fetch historical comments from Telegram target", target_id=target.id, error=str(err))

    async def lookup_user_history(
        self,
        username_or_id: Union[str, int],
        filter_mode: str = "all",  # "all" or "negative"
        max_messages_per_chat: int = 500,
    ) -> Tuple[PlatformAuthor, List[Dict[str, Any]], int]:
        """Execute deep scan across all mutual groups & monitored chats for a specific user.

        Returns:
            Tuple of (PlatformAuthor, list_of_message_dicts, total_scanned_count)
        """
        if not self.client or not self.client.is_connected():
            raise RuntimeError("Telegram MTProto client is not connected.")

        # 1. Resolve user entity
        target_user = None
        clean_input = str(username_or_id).strip()

        if clean_input.startswith("@"):
            clean_input = clean_input[1:]

        try:
            if clean_input.isdigit():
                target_user = await self.client.get_entity(types.PeerUser(int(clean_input)))
            else:
                target_user = await self.client.get_entity(clean_input)
        except Exception as resolve_err:
            logger.warning("Could not resolve entity via MTProto; falling back to DB author record", input=clean_input, error=str(resolve_err))

        numeric_id = str(target_user.id) if target_user else clean_input
        user_handle = getattr(target_user, "username", clean_input if not clean_input.isdigit() else None)
        user_name = f"{getattr(target_user, 'first_name', '')} {getattr(target_user, 'last_name', '')}".strip() if target_user else None

        # Upsert author record
        async with async_session_factory() as session:
            author_repo = AuthorRepository(session)
            author = await author_repo.upsert_author(
                platform="telegram",
                external_user_id=numeric_id,
                username=user_handle,
                display_name=user_name,
                profile_url=f"https://t.me/{user_handle}" if user_handle else None,
            )
            await session.commit()

        # 2. Discover mutual groups using MTProto GetCommonChatsRequest
        chats_to_search: List[Any] = []
        if target_user:
            try:
                res = await self.client(
                    functions.messages.GetCommonChatsRequest(
                        user_id=target_user,
                        max_id=0,
                        limit=100,
                    )
                )
                chats_to_search.extend(res.chats)
                logger.info("Discovered mutual groups via MTProto", count=len(res.chats), user_id=numeric_id)
            except Exception as comm_err:
                logger.warning("Could not retrieve common chats from MTProto", error=str(comm_err))

        # Add all monitored targets to search list to catch channel discussions
        for chat_id, meta in self._monitored_chat_ids.items():
            try:
                ent = await self.client.get_entity(chat_id)
                if ent not in chats_to_search:
                    chats_to_search.append(ent)
            except Exception:
                pass

        total_scanned = 0
        newly_indexed_ids: List[str] = []

        # 3. Server-side search inside each mutual/monitored chat
        for chat in chats_to_search:
            try:
                chat_title = getattr(chat, "title", "Mutual Group")
                chat_user = getattr(chat, "username", None)
                chat_peer_id = getattr(chat, "id", 0)

                # MTProto server-side filtered search by from_user
                async for msg in self.client.iter_messages(chat, from_user=target_user or int(numeric_id), limit=max_messages_per_chat):
                    total_scanned += 1
                    if not msg or not msg.text:
                        continue

                    ext_comment_id = f"tg_{chat_peer_id}_{msg.id}"
                    comment_url = build_telegram_message_url(chat_user, chat_peer_id, msg.id)

                    async with async_session_factory() as session:
                        c_repo = CommentRepository(session)
                        inserted = await c_repo.insert_single(
                            platform="telegram",
                            external_comment_id=ext_comment_id,
                            content_text=msg.text,
                            author_id=author.id,
                            source_name=chat_title,
                            comment_url=comment_url,
                            published_at=msg.date.replace(tzinfo=timezone.utc) if msg.date else datetime.now(timezone.utc),
                        )
                        if inserted:
                            newly_indexed_ids.append(inserted.id)
                        await session.commit()

                # Safe rate-limiting delay between chats to avoid FloodWait
                await asyncio.sleep(0.8)

            except FloodWaitError as fwe:
                logger.warning("Telegram FloodWait encountered during deep scan", wait_seconds=fwe.seconds)
                if fwe.seconds < 60:
                    await asyncio.sleep(fwe.seconds + 1)
                else:
                    break
            except Exception as search_err:
                logger.debug("Skipped chat search", chat=getattr(chat, "title", "Unknown"), error=str(search_err))

        # 4. If filter_mode == "negative", run newly indexed messages through DeepSeek
        if filter_mode == "negative" and newly_indexed_ids:
            async with async_session_factory() as session:
                await self.batch_processor.process_batch(session)

        # 5. Fetch all matching comments from database
        async with async_session_factory() as session:
            c_repo = CommentRepository(session)
            results = await c_repo.get_user_comments(
                platform="telegram",
                external_user_id=numeric_id,
                only_negative=(filter_mode == "negative"),
                limit=100,
            )

            result_dicts = [
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
                for c in results
            ]

        return author, result_dicts, total_scanned
