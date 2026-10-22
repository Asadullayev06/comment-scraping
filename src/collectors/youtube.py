"""YouTube Comment Collector using official Data API v3 with InnerTube fallback."""

import re
import json
from typing import AsyncIterator, Optional, Dict, Any
from datetime import datetime, timezone
import httpx
import structlog
from src.config import settings
from src.collectors.base import BaseCollector
from src.schemas.comment import NormalizedComment
from src.db.models import MonitoredTarget

logger = structlog.get_logger(__name__)


def build_youtube_comment_url(video_id: str, comment_id: str) -> str:
    """Generate direct deep link to a specific YouTube comment."""
    return f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}"


class YouTubeCollector(BaseCollector):
    platform = "youtube"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.YOUTUBE_API_KEY
        self.http_client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        await self.http_client.aclose()

    async def fetch_comments(
        self,
        target: MonitoredTarget,
        cursor: Optional[str] = None,
        limit: int = 1000,
    ) -> AsyncIterator[NormalizedComment]:
        """Fetch comments using API v3 if available, or fall back to InnerTube."""
        video_id = target.external_id

        if self.api_key:
            try:
                async for c in self._fetch_via_api_v3(video_id, target, cursor, limit):
                    yield c
                return
            except Exception as e:
                logger.warning("YouTube API v3 failed or quota exceeded; switching to InnerTube fallback", error=str(e))

        async for c in self._fetch_via_innertube(video_id, target, cursor, limit):
            yield c

    async def _fetch_via_api_v3(
        self,
        video_id: str,
        target: MonitoredTarget,
        cursor: Optional[str],
        limit: int,
    ) -> AsyncIterator[NormalizedComment]:
        """Paginate comments using official YouTube Data API v3 commentThreads."""
        url = "https://www.googleapis.com/youtube/v3/commentThreads"
        page_token = cursor
        total_yielded = 0

        while total_yielded < limit:
            params = {
                "key": self.api_key,
                "part": "snippet,replies",
                "videoId": video_id,
                "maxResults": min(100, limit - total_yielded),
                "order": "time",
            }
            if page_token:
                params["pageToken"] = page_token

            resp = await self.http_client.get(url, params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"YouTube API v3 error {resp.status_code}: {resp.text}")

            data = resp.json()
            items = data.get("items", [])
            if not items:
                break

            for item in items:
                snippet = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
                comment_id = item.get("id")
                author_channel = snippet.get("authorChannelId", {}).get("value", "unknown")
                author_name = snippet.get("authorDisplayName")
                text = snippet.get("textDisplay", "")
                published_str = snippet.get("publishedAt")

                dt = None
                if published_str:
                    try:
                        dt = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
                    except Exception:
                        dt = datetime.now(timezone.utc)

                yield NormalizedComment(
                    platform="youtube",
                    external_comment_id=f"yt_{comment_id}",
                    parent_external_id=video_id,
                    target_external_id=video_id,
                    source_name=target.title_or_name or f"YouTube Video {video_id}",
                    post_url=f"https://www.youtube.com/watch?v={video_id}",
                    comment_url=build_youtube_comment_url(video_id, comment_id),
                    author_external_id=author_channel,
                    author_username=author_name,
                    author_display_name=author_name,
                    content_text=text,
                    published_at=dt,
                )
                total_yielded += 1

                # Include replies if present
                replies = item.get("replies", {}).get("comments", [])
                for reply in replies:
                    r_snip = reply.get("snippet", {})
                    r_id = reply.get("id")
                    r_author = r_snip.get("authorChannelId", {}).get("value", "unknown")
                    r_name = r_snip.get("authorDisplayName")
                    r_text = r_snip.get("textDisplay", "")
                    r_pub = r_snip.get("publishedAt")
                    r_dt = None
                    if r_pub:
                        try:
                            r_dt = datetime.fromisoformat(r_pub.replace("Z", "+00:00"))
                        except Exception:
                            r_dt = datetime.now(timezone.utc)

                    yield NormalizedComment(
                        platform="youtube",
                        external_comment_id=f"yt_{r_id}",
                        parent_external_id=comment_id,
                        target_external_id=video_id,
                        source_name=target.title_or_name or f"YouTube Video {video_id}",
                        post_url=f"https://www.youtube.com/watch?v={video_id}",
                        comment_url=build_youtube_comment_url(video_id, r_id),
                        author_external_id=r_author,
                        author_username=r_name,
                        author_display_name=r_name,
                        content_text=r_text,
                        published_at=r_dt,
                    )
                    total_yielded += 1

            page_token = data.get("nextPageToken")
            if not page_token:
                break

    async def _fetch_via_innertube(
        self,
        video_id: str,
        target: MonitoredTarget,
        cursor: Optional[str],
        limit: int,
    ) -> AsyncIterator[NormalizedComment]:
        """InnerTube continuation fallback when no API key is available."""
        # Lightweight innerTube initial fetch
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        resp = await self.http_client.get(watch_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        html_text = resp.text

        # Extract continuation token
        key_match = re.search(r'"INNERTUBE_API_KEY":"([A-Za-z0-9_-]+)"', html_text)
        token_match = re.search(r'"continuationCommand":{"token":"([A-Za-z0-9_-]+)"', html_text)

        if not key_match or not token_match:
            logger.warning("Could not extract InnerTube token from YouTube page", video_id=video_id)
            return

        innertube_key = key_match.group(1)
        continuation_token = token_match.group(1)

        api_url = f"https://www.youtube.com/youtubei/v1/next?key={innertube_key}"
        total_yielded = 0

        while continuation_token and total_yielded < limit:
            payload = {
                "context": {
                    "client": {
                        "clientName": "WEB",
                        "clientVersion": "2.20240501.00.00",
                    }
                },
                "continuation": continuation_token,
            }
            res = await self.http_client.post(api_url, json=payload)
            if res.status_code != 200:
                break

            data = res.json()
            endpoints = data.get("onResponseReceivedEndpoints", [])
            next_token = None

            for ep in endpoints:
                actions = ep.get("appendContinuationItemsAction", {}).get("continuationItems", [])
                for action in actions:
                    if "commentThreadRenderer" in action:
                        renderer = action["commentThreadRenderer"]["comment"]["commentRenderer"]
                        c_id = renderer.get("commentId")
                        author_name = renderer.get("authorText", {}).get("simpleText", "Unknown")
                        text_runs = renderer.get("contentText", {}).get("runs", [])
                        text = "".join(r.get("text", "") for r in text_runs)

                        yield NormalizedComment(
                            platform="youtube",
                            external_comment_id=f"yt_{c_id}",
                            parent_external_id=video_id,
                            target_external_id=video_id,
                            source_name=target.title_or_name or f"YouTube Video {video_id}",
                            post_url=watch_url,
                            comment_url=build_youtube_comment_url(video_id, c_id),
                            author_external_id=renderer.get("authorEndpoint", {}).get("browseEndpoint", {}).get("browseId", author_name),
                            author_username=author_name,
                            author_display_name=author_name,
                            content_text=text,
                            published_at=datetime.now(timezone.utc),
                        )
                        total_yielded += 1

                    elif "continuationItemRenderer" in action:
                        cont_endpoint = action["continuationItemRenderer"].get("continuationEndpoint", {})
                        next_token = cont_endpoint.get("continuationCommand", {}).get("token")

            continuation_token = next_token
