"""Facebook Comet GraphQL Comment Collector with unfiltered comment ordering."""

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


class FacebookCollector(BaseCollector):
    platform = "facebook"

    def __init__(
        self,
        pool_manager: Optional[SessionPoolManager] = None,
        doc_id: Optional[str] = None,
    ):
        self.pool_manager = pool_manager
        self.doc_id = doc_id or settings.FB_COMET_DOC_ID

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
        """Paginate comments on a Facebook post using Comet GraphQL."""
        post_id = target.external_id
        if not self.pool_manager:
            logger.warning("No pool manager configured for Facebook collector; skipping")
            return

        session_tuple = await self.pool_manager.acquire_session("facebook")
        if not session_tuple:
            logger.warning("No active Facebook burner session found.")
            return

        session_obj, cookies, proxy_url = session_tuple

        headers = {
            "user-agent": session_obj.user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "content-type": "application/x-www-form-urlencoded",
        }

        total_yielded = 0
        current_cursor = cursor
        graphql_url = "https://www.facebook.com/api/graphql/"

        async with httpx.AsyncClient(cookies=cookies, proxy=proxy_url, headers=headers, timeout=25.0) as client:
            while total_yielded < limit:
                variables = {
                    "feedbackTargetID": post_id,
                    "commentOrderingMode": "RANKED_UNFILTERED",
                    "count": 50,
                    "cursor": current_cursor,
                }
                form_data = {
                    "doc_id": self.doc_id,
                    "variables": json.dumps(variables),
                    "fb_dtsg": cookies.get("fb_dtsg", ""),
                }

                try:
                    res = await client.post(graphql_url, data=form_data)
                    if res.status_code == 429:
                        await self.pool_manager.report_rate_limit(session_obj.id, cooldown_minutes=30)
                        break
                    if res.status_code != 200:
                        logger.error("Facebook Comet GraphQL returned status", status=res.status_code)
                        break

                    data = res.json()
                    feedback = data.get("data", {}).get("feedback", {})
                    comment_conn = feedback.get("comments", {})
                    edges = comment_conn.get("edges", [])
                    if not edges:
                        break

                    for edge in edges:
                        node = edge.get("node", {})
                        c_id = node.get("legacy_fbid") or node.get("id")
                        body = node.get("body", {}).get("text", "")
                        author = node.get("author", {})
                        author_name = author.get("name", "Facebook User")
                        author_id = author.get("id", "unknown")
                        created_time = node.get("created_time")
                        pub_dt = datetime.fromtimestamp(created_time, tz=timezone.utc) if created_time else datetime.now(timezone.utc)

                        comment_url = f"https://www.facebook.com/{post_id}?comment_id={c_id}"

                        yield NormalizedComment(
                            platform="facebook",
                            external_comment_id=f"fb_{c_id}",
                            parent_external_id=post_id,
                            target_external_id=post_id,
                            source_name=target.title_or_name or "Facebook Post",
                            post_url=target.url or f"https://www.facebook.com/{post_id}",
                            comment_url=comment_url,
                            author_external_id=author_id,
                            author_username=author_name,
                            author_display_name=author_name,
                            content_text=body,
                            published_at=pub_dt,
                        )
                        total_yielded += 1

                    page_info = comment_conn.get("page_info", {})
                    has_next = page_info.get("has_next_page", False)
                    end_cursor = page_info.get("end_cursor")

                    if not has_next or not end_cursor or end_cursor == current_cursor:
                        break
                    current_cursor = end_cursor

                except Exception as e:
                    logger.error("Facebook scraping failed", error=str(e))
                    break
