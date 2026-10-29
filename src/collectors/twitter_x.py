"""X (Twitter) GraphQL TweetDetail Collector for cursor-paginated replies."""

import json
from typing import AsyncIterator, Optional, Dict, Any
from datetime import datetime, timezone
import httpx
import structlog
from src.config import settings
from src.collectors.base import BaseCollector
from src.schemas.comment import NormalizedComment
from src.db.models import MonitoredTarget
from src.collectors.pool_manager import SessionPoolManager

logger = structlog.get_logger(__name__)


class TwitterXCollector(BaseCollector):
    platform = "x"

    def __init__(
        self,
        pool_manager: Optional[SessionPoolManager] = None,
        query_id: Optional[str] = None,
    ):
        self.pool_manager = pool_manager
        self.query_id = query_id or settings.X_TWEET_DETAIL_QUERY_ID

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def fetch_comments(
        self,
        target: MonitoredTarget,
        cursor: Optional[str] = None,
        limit: int = 5000,
    ) -> AsyncIterator[NormalizedComment]:
        """Paginate replies to a tweet using X GraphQL TweetDetail endpoint."""
        tweet_id = target.external_id
        current_cursor = cursor
        total_yielded = 0

        # Acquire healthy burner session
        if not self.pool_manager:
            logger.warning("No pool manager configured for X scraper; skipping")
            return

        session_tuple = await self.pool_manager.acquire_session("x")
        if not session_tuple:
            logger.warning("No active X burner session found.")
            return

        session_obj, cookies, proxy_url = session_tuple

        headers = {
            "authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
            "x-csrf-token": cookies.get("ct0", ""),
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "user-agent": session_obj.user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "content-type": "application/json",
            "Referer": f"https://x.com/i/status/{tweet_id}",
        }

        async with httpx.AsyncClient(cookies=cookies, proxy=proxy_url, headers=headers, timeout=25.0) as client:
            while total_yielded < limit:
                # Query TweetDetail GraphQL
                variables = {
                    "focalTweetId": tweet_id,
                    "with_rux_injections": False,
                    "includePromotedContent": False,
                    "withCommunity": False,
                    "withQuickPromoteEligibilityTweetFields": False,
                    "withBirdwatchNotes": False,
                    "withVoice": False,
                    "withV2Timeline": True,
                }
                if current_cursor:
                    variables["cursor"] = current_cursor

                params = {
                    "variables": json.dumps(variables),
                    "features": json.dumps({"responsive_web_graphql_timeline_navigation_enabled": True}),
                }

                url = f"https://x.com/i/api/graphql/{self.query_id}/TweetDetail"
                try:
                    resp = await client.get(url, params=params)
                    if resp.status_code == 429:
                        await self.pool_manager.report_rate_limit(session_obj.id, cooldown_minutes=15)
                        break
                    if resp.status_code != 200:
                        logger.error("X GraphQL request returned status", status=resp.status_code)
                        break

                    data = resp.json()
                    instructions = data.get("data", {}).get("threaded_conversation_with_injections_v2", {}).get("instructions", [])
                    next_cursor = None

                    for inst in instructions:
                        entries = inst.get("entries", [])
                        for entry in entries:
                            entry_id = entry.get("entryId", "")

                            # Extract cursor
                            if entry_id.startswith("cursor-bottom-"):
                                next_cursor = entry.get("content", {}).get("itemContent", {}).get("value")

                            # Extract tweet/reply
                            item_content = entry.get("content", {}).get("itemContent", {})
                            tweet_res = item_content.get("tweet_results", {}).get("result", {})
                            legacy = tweet_res.get("legacy", {})

                            reply_id = legacy.get("id_str")
                            if reply_id and reply_id != tweet_id:
                                user_result = tweet_res.get("core", {}).get("user_results", {}).get("result", {})
                                user_legacy = user_result.get("legacy", {})
                                username = user_legacy.get("screen_name")
                                text = legacy.get("full_text", "")

                                yield NormalizedComment(
                                    platform="x",
                                    external_comment_id=f"x_{reply_id}",
                                    parent_external_id=tweet_id,
                                    target_external_id=tweet_id,
                                    source_name=f"X Post @{username}",
                                    post_url=f"https://x.com/i/status/{tweet_id}",
                                    comment_url=f"https://x.com/{username}/status/{reply_id}" if username else f"https://x.com/i/status/{reply_id}",
                                    author_external_id=user_result.get("rest_id", username or "unknown"),
                                    author_username=username,
                                    author_display_name=user_legacy.get("name"),
                                    content_text=text,
                                    published_at=datetime.now(timezone.utc),
                                )
                                total_yielded += 1

                    if not next_cursor or next_cursor == current_cursor:
                        break
                    current_cursor = next_cursor

                except Exception as e:
                    logger.error("X scraper iteration failed", error=str(e))
                    break
