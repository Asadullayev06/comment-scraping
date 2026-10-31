"""YouTube Comment & Channel Collector using official Data API v3 with modern InnerTube fallback (no API key required)."""

import re
import json
from typing import AsyncIterator, Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone
import httpx
import structlog
from src.config import settings
from src.collectors.base import BaseCollector, ScraperError
from src.schemas.comment import NormalizedComment
from src.db.models import MonitoredTarget

logger = structlog.get_logger(__name__)

YT_WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def build_youtube_comment_url(video_id: str, comment_id: str) -> str:
    """Generate direct deep link to a specific YouTube comment."""
    return f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}"


def _parse_compact_number(raw: Optional[str]) -> int:
    """Parse compact YouTube count strings like '46.6M subscribers', '1.6K videos', '4.8M', '986' into int."""
    if not raw:
        return 0
    cleaned = str(raw).upper().replace(",", "").strip()
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KMB])?", cleaned)
    if not m:
        return 0
    val = float(m.group(1))
    mult = m.group(2)
    if mult == "K":
        val *= 1_000
    elif mult == "M":
        val *= 1_000_000
    elif mult == "B":
        val *= 1_000_000_000
    return int(val)


def _find_continuation_tokens(obj: Any, out: List[str]) -> None:
    """Recursively collect continuationCommand tokens from an InnerTube JSON payload."""
    if isinstance(obj, dict):
        if "continuationCommand" in obj and isinstance(obj["continuationCommand"], dict):
            tok = obj["continuationCommand"].get("token")
            if tok and tok not in out:
                out.append(tok)
        if "nextContinuationData" in obj and isinstance(obj["nextContinuationData"], dict):
            tok = obj["nextContinuationData"].get("continuation")
            if tok and tok not in out:
                out.append(tok)
        for v in obj.values():
            _find_continuation_tokens(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _find_continuation_tokens(item, out)


class YouTubeCollector(BaseCollector):
    platform = "youtube"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key if api_key is not None else settings.YOUTUBE_API_KEY
        self.http_client = httpx.AsyncClient(timeout=25.0, follow_redirects=True, headers=YT_WEB_HEADERS)
        self.last_focal_tweet_meta: Optional[Dict[str, Any]] = None

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
        """Fetch comments using API v3 if available, or fall back to modern InnerTube."""
        video_id = str(target.external_id).strip()
        self.last_focal_tweet_meta = None
        total_yielded = 0

        if self.api_key:
            try:
                async for c in self._fetch_via_api_v3(video_id, target, cursor, limit):
                    yield c
                    total_yielded += 1
                if total_yielded > 0:
                    return
            except ScraperError:
                raise
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
        # Optional video metadata lookup
        v_resp = await self.http_client.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"key": self.api_key, "part": "snippet,statistics", "id": video_id},
        )
        if v_resp.status_code == 200:
            v_items = (v_resp.json() or {}).get("items") or []
            if not v_items:
                raise ScraperError("video not found", f"YouTube video {video_id} not found", status_code=404)
            v_snip = v_items[0].get("snippet") or {}
            v_stats = v_items[0].get("statistics") or {}
            ch_title = v_snip.get("channelTitle") or "YouTube"
            v_title = v_snip.get("title") or video_id
            target.title_or_name = f"YouTube {ch_title}: {v_title[:80]}"
            self.last_focal_tweet_meta = {
                "id": video_id,
                "author": ch_title,
                "text": v_title,
                "published_at": v_snip.get("publishedAt"),
                "like_count": int(v_stats.get("likeCount") or 0),
                "reply_count": int(v_stats.get("commentCount") or 0),
                "view_count": int(v_stats.get("viewCount") or 0),
            }

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
            if resp.status_code == 404:
                raise ScraperError("video not found", f"YouTube video {video_id} not found", status_code=404)
            if resp.status_code != 200:
                raise RuntimeError(f"YouTube API v3 error {resp.status_code}: {resp.text}")

            data = resp.json()
            items = data.get("items", [])
            if not items:
                break

            for item in items:
                top_snip = item.get("snippet", {})
                snippet = top_snip.get("topLevelComment", {}).get("snippet", {})
                comment_id = item.get("id")
                author_channel = snippet.get("authorChannelId", {}).get("value", "unknown")
                author_name = (snippet.get("authorDisplayName") or "User").lstrip("@")
                text = snippet.get("textOriginal") or snippet.get("textDisplay") or ""
                published_str = snippet.get("publishedAt")
                reply_count = int(top_snip.get("totalReplyCount") or 0)

                dt = datetime.now(timezone.utc)
                if published_str:
                    try:
                        dt = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
                    except Exception:
                        pass

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
                    child_comment_count=reply_count,
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
        """Modern InnerTube continuation parser supporting both commentEntityPayload (2025/2026) and legacy commentThreadRenderer."""
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        resp = await self.http_client.get(watch_url)
        if resp.status_code == 404:
            raise ScraperError("video not found", f"YouTube video {video_id} not found", status_code=404)

        html_text = resp.text
        if '"playabilityStatus":{"status":"ERROR"' in html_text or "Video unavailable" in html_text:
            raise ScraperError("video not found", f"YouTube video {video_id} is unavailable", status_code=404)

        # Extract video title & author from watch HTML if available
        title_m = re.search(r'"videoDetails":\{.*?"title":"([^"]+)"', html_text)
        author_m = re.search(r'"videoDetails":\{.*?"author":"([^"]+)"', html_text)
        views_m = re.search(r'"videoDetails":\{.*?"viewCount":"(\d+)"', html_text)
        if title_m:
            v_title = title_m.group(1)
            v_author = author_m.group(1) if author_m else "YouTube"
            target.title_or_name = f"YouTube {v_author}: {v_title[:80]}"
            self.last_focal_tweet_meta = {
                "id": video_id,
                "author": v_author,
                "text": v_title,
                "view_count": int(views_m.group(1)) if views_m else 0,
            }

        key_match = re.search(r'"INNERTUBE_API_KEY":"([A-Za-z0-9_-]+)"', html_text)
        tokens = re.findall(r'"continuationCommand":\{"token":"([^"]+)"', html_text)

        if not key_match or not tokens:
            return

        innertube_key = key_match.group(1)
        api_url = f"https://www.youtube.com/youtubei/v1/next?key={innertube_key}"
        total_yielded = 0
        seen_ids = set()

        # Find the initial comment section continuation token (usually tokens[0], or check first 3 tokens)
        queue_tokens = [cursor] if cursor else tokens[:4]
        visited_tokens = set()

        while queue_tokens and total_yielded < limit:
            continuation_token = queue_tokens.pop(0)
            if not continuation_token or continuation_token in visited_tokens:
                continue
            visited_tokens.add(continuation_token)

            payload = {
                "context": {
                    "client": {
                        "clientName": "WEB",
                        "clientVersion": "2.20240501.00.00",
                        "hl": "en",
                    }
                },
                "continuation": continuation_token,
            }
            res = await self.http_client.post(api_url, json=payload)
            if res.status_code != 200:
                continue

            data = res.json()
            page_comments: List[NormalizedComment] = []

            # Format 1 (Modern 2025/2026 YouTube): frameworkUpdates -> entityBatchUpdate -> mutations -> commentEntityPayload
            mutations = (
                (data.get("frameworkUpdates") or {})
                .get("entityBatchUpdate", {})
                .get("mutations", [])
            )
            for mut in mutations:
                cep = (mut.get("payload") or {}).get("commentEntityPayload")
                if not isinstance(cep, dict):
                    continue
                props = cep.get("properties") or {}
                c_id = str(props.get("commentId") or "")
                text = ((props.get("content") or {}).get("content") or "").strip()
                if not c_id or not text or c_id in seen_ids:
                    continue
                seen_ids.add(c_id)

                author_obj = cep.get("author") or {}
                channel_id = str(author_obj.get("channelId") or "unknown")
                display_name = (author_obj.get("displayName") or "User").strip()
                username = display_name.lstrip("@") if display_name.startswith("@") else display_name

                toolbar = cep.get("toolbar") or {}
                reply_count = _parse_compact_number(toolbar.get("replyCount"))

                page_comments.append(
                    NormalizedComment(
                        platform="youtube",
                        external_comment_id=f"yt_{c_id}",
                        parent_external_id=video_id,
                        target_external_id=video_id,
                        source_name=target.title_or_name or f"YouTube Video {video_id}",
                        post_url=watch_url,
                        comment_url=build_youtube_comment_url(video_id, c_id),
                        author_external_id=channel_id,
                        author_username=username,
                        author_display_name=display_name,
                        content_text=text,
                        published_at=datetime.now(timezone.utc),
                        child_comment_count=reply_count,
                    )
                )

            # Format 2 (Legacy YouTube): onResponseReceivedEndpoints -> commentThreadRenderer
            endpoints = data.get("onResponseReceivedEndpoints") or []
            for ep in endpoints:
                actions = (
                    (ep.get("appendContinuationItemsAction") or {}).get("continuationItems")
                    or (ep.get("reloadContinuationItemsCommand") or {}).get("continuationItems")
                    or []
                )
                for action in actions:
                    if "commentThreadRenderer" in action:
                        renderer = (action["commentThreadRenderer"].get("comment") or {}).get("commentRenderer") or {}
                        c_id = str(renderer.get("commentId") or "")
                        text_runs = (renderer.get("contentText") or {}).get("runs") or []
                        text = "".join(r.get("text", "") for r in text_runs).strip()
                        if not c_id or not text or c_id in seen_ids:
                            continue
                        seen_ids.add(c_id)
                        author_name = (renderer.get("authorText") or {}).get("simpleText", "User")
                        browse_id = (
                            ((renderer.get("authorEndpoint") or {}).get("browseEndpoint") or {}).get("browseId")
                            or author_name
                        )
                        page_comments.append(
                            NormalizedComment(
                                platform="youtube",
                                external_comment_id=f"yt_{c_id}",
                                parent_external_id=video_id,
                                target_external_id=video_id,
                                source_name=target.title_or_name or f"YouTube Video {video_id}",
                                post_url=watch_url,
                                comment_url=build_youtube_comment_url(video_id, c_id),
                                author_external_id=browse_id,
                                author_username=author_name.lstrip("@"),
                                author_display_name=author_name,
                                content_text=text,
                                published_at=datetime.now(timezone.utc),
                            )
                        )

            for norm in page_comments:
                if total_yielded >= limit:
                    break
                yield norm
                total_yielded += 1

            # If this token yielded comments, discover next page continuation token from onResponseReceivedEndpoints
            if page_comments and total_yielded < limit:
                next_tokens: List[str] = []
                _find_continuation_tokens(endpoints, next_tokens)
                for nt in next_tokens:
                    if nt not in visited_tokens:
                        queue_tokens.insert(0, nt)

    async def fetch_profile_info_and_posts(
        self,
        username: str,
        count: int = 5,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Fetch a YouTube channel's metadata and latest N videos using InnerTube (or Data API v3)."""
        raw_clean = username.strip().rstrip("/")
        if "youtube.com/" in raw_clean:
            path_part = raw_clean.split("youtube.com/")[-1].split("?")[0]
            path_part = re.sub(r"/(videos|shorts|streams|featured|community|playlists)$", "", path_part)
        else:
            path_part = raw_clean if raw_clean.startswith("@") or raw_clean.startswith("UC") else f"@{raw_clean}"

        if path_part.startswith("channel/") or path_part.startswith("c/") or path_part.startswith("user/"):
            channel_url = f"https://www.youtube.com/{path_part}"
            handle_display = path_part.split("/")[-1]
        elif path_part.startswith("UC") and len(path_part) >= 20:
            channel_url = f"https://www.youtube.com/channel/{path_part}"
            handle_display = path_part
        else:
            handle = path_part if path_part.startswith("@") else f"@{path_part}"
            channel_url = f"https://www.youtube.com/{handle}"
            handle_display = handle

        videos_url = f"{channel_url}/videos"
        resp = await self.http_client.get(videos_url)
        if resp.status_code == 404:
            raise ScraperError("profile not found", f"YouTube channel '{handle_display}' not found", status_code=404)

        html = resp.text
        m_init = re.search(r"var ytInitialData = (\{.*?\});</script>", html)
        if not m_init:
            return (
                {
                    "platform": "youtube",
                    "username": handle_display,
                    "url": channel_url,
                    "full_name": handle_display,
                    "bio": None,
                    "followers": None,
                    "following": None,
                    "total_posts": 0,
                    "is_verified": False,
                },
                [],
            )

        raw_json = m_init.group(1)
        ch_data = json.loads(raw_json)
        ch_meta = (ch_data.get("metadata") or {}).get("channelMetadataRenderer") or {}

        sub_matches = re.findall(r'"([0-9.,]+(?:\s*[KMB])?\s*subscribers)"', raw_json, flags=re.IGNORECASE)
        vid_matches = re.findall(r'"([0-9.,]+(?:\s*[KMB])?\s*videos)"', raw_json, flags=re.IGNORECASE)

        profile_meta: Dict[str, Any] = {
            "platform": "youtube",
            "channel_id": ch_meta.get("externalId"),
            "username": handle_display,
            "url": ch_meta.get("channelUrl") or channel_url,
            "full_name": ch_meta.get("title") or handle_display,
            "bio": (ch_meta.get("description") or "")[:300] or None,
            "followers": _parse_compact_number(sub_matches[0]) if sub_matches else None,
            "following": None,
            "total_posts": _parse_compact_number(vid_matches[0]) if vid_matches else None,
            "is_verified": '"CHECK_CIRCLE_FILLED"' in raw_json or '"BADGE_STYLE_TYPE_VERIFIED"' in raw_json,
        }

        posts: List[Dict[str, Any]] = []
        seen_vids = set()

        def _extract_videos(obj: Any) -> None:
            if len(posts) >= count:
                return
            if isinstance(obj, dict):
                # Modern 2025/2026 lockupViewModel
                if "lockupViewModel" in obj and isinstance(obj["lockupViewModel"], dict):
                    lvm = obj["lockupViewModel"]
                    vid = str(lvm.get("contentId") or "")
                    if vid and len(vid) == 11 and vid not in seen_vids:
                        seen_vids.add(vid)
                        m_vm = (lvm.get("metadata") or {}).get("lockupMetadataViewModel") or {}
                        title = ((m_vm.get("title") or {}).get("content") or "").strip()
                        rows = ((m_vm.get("metadata") or {}).get("contentMetadataViewModel") or {}).get("metadataRows") or []
                        views_count = 0
                        pub_text = None
                        for r in rows:
                            for part in r.get("metadataParts") or []:
                                txt = ((part.get("text") or {}).get("content") or "").strip()
                                a11y = (part.get("accessibilityLabel") or "").lower()
                                if "view" in a11y or "view" in txt.lower():
                                    views_count = _parse_compact_number(txt)
                                elif "ago" in txt.lower() or "ago" in a11y:
                                    pub_text = txt
                        posts.append(
                            {
                                "post_index": len(posts) + 1,
                                "shortcode": vid,
                                "video_id": vid,
                                "url": f"https://www.youtube.com/watch?v={vid}",
                                "caption": title[:200],
                                "comment_count": 0,
                                "like_count": views_count,
                                "view_count": views_count,
                                "published_at": pub_text,
                            }
                        )
                        return

                # Classic videoRenderer
                if "videoRenderer" in obj and isinstance(obj["videoRenderer"], dict):
                    vr = obj["videoRenderer"]
                    vid = str(vr.get("videoId") or "")
                    if vid and len(vid) == 11 and vid not in seen_vids:
                        seen_vids.add(vid)
                        title_runs = (vr.get("title") or {}).get("runs") or []
                        title = "".join(r.get("text", "") for r in title_runs) or (vr.get("title") or {}).get("simpleText", "")
                        pub_text = (vr.get("publishedTimeText") or {}).get("simpleText")
                        views_text = (vr.get("viewCountText") or {}).get("simpleText")
                        views_count = _parse_compact_number(views_text)
                        posts.append(
                            {
                                "post_index": len(posts) + 1,
                                "shortcode": vid,
                                "video_id": vid,
                                "url": f"https://www.youtube.com/watch?v={vid}",
                                "caption": title[:200],
                                "comment_count": 0,
                                "like_count": views_count,
                                "view_count": views_count,
                                "published_at": pub_text,
                            }
                        )
                        return

                for v in obj.values():
                    _extract_videos(v)
            elif isinstance(obj, list):
                for item in obj:
                    _extract_videos(item)

        _extract_videos(ch_data)
        return profile_meta, posts[:count]

    async def fetch_profile_recent_posts(self, username: str, count: int = 5) -> List[Dict[str, Any]]:
        """Return only the recent videos list for a YouTube channel."""
        _, posts = await self.fetch_profile_info_and_posts(username, count=count)
        return posts
