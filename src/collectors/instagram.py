"""Instagram Comment Collector using mobile API (instagrapi) with single-account pinning and challenge resilience."""

import asyncio
import random
from typing import AsyncIterator, Optional, Dict, Any, List
from datetime import datetime, timezone
import structlog
from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired,
    LoginRequired,
    FeedbackRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
    ClientError,
)
from src.collectors.base import BaseCollector
from src.schemas.comment import NormalizedComment
from src.db.models import MonitoredTarget
from src.collectors.pool_manager import SessionPoolManager

logger = structlog.get_logger(__name__)


def build_instagram_comment_url(shortcode: str, comment_pk: str) -> str:
    """Generate direct deep link to an Instagram comment."""
    return f"https://www.instagram.com/p/{shortcode}/c/{comment_pk}/"


class InstagramCollector(BaseCollector):
    platform = "instagram"

    def __init__(self, pool_manager: Optional[SessionPoolManager] = None):
        self.pool_manager = pool_manager

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def _init_client(self, session_data: Any, proxy_url: Optional[str]) -> Client:
        """Initialize instagrapi Client with authenticated mobile settings and proxy."""
        cl = Client()
        if proxy_url:
            cl.set_proxy(proxy_url)

        if isinstance(session_data, dict):
            # Check if full instagrapi dump_settings or simple sessionid dict
            if "sessionid" in session_data and len(session_data) <= 3:
                cl.login_by_sessionid(session_data["sessionid"])
            else:
                cl.set_settings(session_data)
        elif isinstance(session_data, str):
            cl.login_by_sessionid(session_data)

        return cl

    async def fetch_comments(
        self,
        target: MonitoredTarget,
        cursor: Optional[str] = None,
        limit: int = 5000,
    ) -> AsyncIterator[NormalizedComment]:
        """Paginate comments on an Instagram post using a single pinned burner account.

        Paginates start to finish with human-like jitter, walks child replies,
        detects challenge/rate limits, and pauses the affected account gracefully.
        """
        shortcode = target.external_id
        if not self.pool_manager:
            logger.warning("No pool manager configured for Instagram collector; skipping")
            return

        # 1. Assign single account to paginate this post from start to finish
        session_tuple = await self.pool_manager.acquire_session("instagram")
        if not session_tuple:
            logger.warning("No healthy Instagram burner session available in pool.")
            return

        session_obj, session_data, proxy_url = session_tuple

        try:
            cl = await asyncio.to_thread(self._init_client, session_data, proxy_url)
        except Exception as init_err:
            logger.error("Failed to initialize instagrapi client with session", error=str(init_err))
            await self.pool_manager.report_challenge(session_obj.id, error_type="SessionInitFailed", cooldown_hours=2)
            return

        # Resolve media_id from shortcode or target external_id
        try:
            if shortcode.isdigit():
                media_id = str(shortcode)
            else:
                media_pk = await asyncio.to_thread(cl.media_pk_from_url, f"https://www.instagram.com/p/{shortcode}/")
                media_id = str(media_pk)
        except Exception as pk_err:
            logger.error("Could not resolve media_id for post", shortcode=shortcode, error=str(pk_err))
            media_id = shortcode

        total_yielded = 0
        min_id = cursor
        consecutive_known_ids = 0

        logger.info("Starting Instagram post comment extraction", shortcode=shortcode, media_id=media_id, burner=session_obj.account_identifier)

        while total_yielded < limit:
            try:
                # 2. Fetch chunk of comments using mobile API endpoint
                # media_comments_chunk returns (List[Comment], min_id)
                comments_chunk, next_min_id = await asyncio.to_thread(
                    cl.media_comments_chunk,
                    media_id,
                    max_amount=50,
                    min_id=min_id,
                )

                if not comments_chunk:
                    break

                for c in comments_chunk:
                    pk = str(c.pk)
                    user = getattr(c, "user", None)
                    username = getattr(user, "username", None)
                    full_name = getattr(user, "full_name", None)
                    user_pk = str(getattr(user, "pk", "unknown"))
                    text = getattr(c, "text", "")
                    created_at_utc = getattr(c, "created_at_utc", None)
                    if isinstance(created_at_utc, (int, float)):
                        pub_dt = datetime.fromtimestamp(created_at_utc, tz=timezone.utc)
                    else:
                        pub_dt = created_at_utc or datetime.now(timezone.utc)

                    child_count = getattr(c, "child_comment_count", 0) or 0

                    yield NormalizedComment(
                        platform="instagram",
                        external_comment_id=f"ig_{pk}",
                        parent_external_id=shortcode,
                        child_comment_count=child_count,
                        target_external_id=shortcode,
                        source_name=f"Instagram Post @{username}" if username else "Instagram Post",
                        post_url=f"https://www.instagram.com/p/{shortcode}/",
                        comment_url=build_instagram_comment_url(shortcode, pk),
                        author_external_id=user_pk,
                        author_username=username,
                        author_display_name=full_name,
                        content_text=text,
                        published_at=pub_dt,
                    )
                    total_yielded += 1

                if not next_min_id or next_min_id == min_id:
                    break
                min_id = next_min_id

                # 4. Human-like pagination pacing delay (2.0s - 4.0s)
                jitter = random.uniform(2.0, 4.0)
                await asyncio.sleep(jitter)

            # 5. Challenge & Rate-Limit Exception Handling
            except (ChallengeRequired, FeedbackRequired) as challenge_err:
                logger.error(
                    "Instagram account hit checkpoint challenge",
                    burner=session_obj.account_identifier,
                    error=str(challenge_err),
                )
                await self.pool_manager.report_challenge(
                    session_obj.id,
                    error_type="ChallengeRequired",
                    cooldown_hours=6,
                )
                break

            except (RateLimitError, PleaseWaitFewMinutes) as rate_err:
                logger.warning(
                    "Instagram rate-limit encountered; pausing account",
                    burner=session_obj.account_identifier,
                    error=str(rate_err),
                )
                await self.pool_manager.report_challenge(
                    session_obj.id,
                    error_type="PleaseWaitFewMinutes",
                    cooldown_hours=2,
                )
                break

            except LoginRequired as login_err:
                logger.error(
                    "Instagram session invalidated / login required",
                    burner=session_obj.account_identifier,
                    error=str(login_err),
                )
                await self.pool_manager.report_challenge(
                    session_obj.id,
                    error_type="LoginRequired",
                    cooldown_hours=12,
                )
                break

            except ClientError as client_err:
                logger.error("Instagram client error", error=str(client_err))
                break

            except Exception as e:
                logger.error("Unexpected error during Instagram pagination", error=str(e))
                break

        logger.info(
            "Finished Instagram comment extraction run",
            shortcode=shortcode,
            comments_extracted=total_yielded,
        )
