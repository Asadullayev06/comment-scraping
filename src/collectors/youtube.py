"""YouTube Comment & Channel Collector using official Data API v3 with modern InnerTube fallback (no API key required)."""

import re
import json
import html
import os
import glob
import tempfile
import asyncio
import xml.etree.ElementTree as ET
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

YT_ANDROID_UA = "com.google.android.youtube/20.10.38 (Linux; U; Android 14) gzip"


def format_seconds_to_timestamp(seconds: float) -> str:
    """Format seconds into MM:SS or HH:MM:SS string."""
    total_sec = max(0, int(seconds))
    hrs, rem = divmod(total_sec, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def parse_iso8601_duration(iso_str: Optional[str]) -> int:
    """Parse ISO-8601 duration string (e.g. 'PT1H12M34S') into total seconds."""
    if not iso_str:
        return 0
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", str(iso_str).strip(), flags=re.IGNORECASE)
    if not m:
        return 0
    hrs = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    secs = int(m.group(3) or 0)
    return hrs * 3600 + mins * 60 + secs


def extract_chapters_from_description(description: Optional[str]) -> List[Dict[str, Any]]:
    """Extract timestamped chapter markers from a YouTube video description."""
    if not description:
        return []
    chapters: List[Dict[str, Any]] = []
    for line in description.splitlines():
        line_clean = line.strip()
        m = re.match(r"^(?:[\-\*\•]\s*)?(\d{1,2}:\d{2}(?::\d{2})?)\s*[\-–—:]?\s*(.+)$", line_clean)
        if m:
            ts_str = m.group(1)
            title = m.group(2).strip()
            parts = [int(p) for p in ts_str.split(":")]
            if len(parts) == 3:
                sec = parts[0] * 3600 + parts[1] * 60 + parts[2]
            else:
                sec = parts[0] * 60 + parts[1]
            if title:
                chapters.append(
                    {
                        "timestamp": format_seconds_to_timestamp(sec),
                        "start_seconds": sec,
                        "title": title[:120],
                    }
                )
    return chapters


def parse_youtube_timedtext(raw_content: str) -> List[Dict[str, Any]]:
    """Parse YouTube timedtext response (XML format 3, XML format 1, or JSON3) into timestamped segments."""
    if not raw_content or not raw_content.strip():
        return []

    text_stripped = raw_content.strip()
    segments: List[Dict[str, Any]] = []

    # 1. Try JSON3 format if response starts with '{'
    if text_stripped.startswith("{"):
        try:
            jdata = json.loads(text_stripped)
            for ev in jdata.get("events") or []:
                segs = ev.get("segs") or []
                raw_txt = "".join(s.get("utf8", "") for s in segs if isinstance(s, dict))
                clean_txt = " ".join(html.unescape(raw_txt).split()).strip()
                if not clean_txt:
                    continue
                t_ms = int(ev.get("tStartMs") or 0)
                d_ms = int(ev.get("dDurationMs") or 0)
                start_sec = round(t_ms / 1000.0, 1)
                dur_sec = round(d_ms / 1000.0, 1)
                if segments and segments[-1]["text"] == clean_txt:
                    continue
                segments.append(
                    {
                        "timestamp": format_seconds_to_timestamp(start_sec),
                        "start": start_sec,
                        "duration": dur_sec,
                        "text": clean_txt,
                    }
                )
            return segments
        except Exception:
            pass

    # 1.5. Try WebVTT format if response starts with 'WEBVTT'
    if text_stripped.startswith("WEBVTT"):
        lines = text_stripped.splitlines()
        idx = 0
        while idx < len(lines):
            line = lines[idx].strip()
            m_vtt = re.match(
                r"^(\d{1,2}:)?(\d{2}):(\d{2})[\.,](\d{3})\s*-->\s*(\d{1,2}:)?(\d{2}):(\d{2})[\.,](\d{3})",
                line,
            )
            if m_vtt:
                h1 = int((m_vtt.group(1) or "0:").rstrip(":"))
                m1 = int(m_vtt.group(2))
                s1 = int(m_vtt.group(3))
                ms1 = int(m_vtt.group(4))
                h2 = int((m_vtt.group(5) or "0:").rstrip(":"))
                m2 = int(m_vtt.group(6))
                s2 = int(m_vtt.group(7))
                ms2 = int(m_vtt.group(8))
                start_sec = round(h1 * 3600 + m1 * 60 + s1 + ms1 / 1000.0, 1)
                end_sec = round(h2 * 3600 + m2 * 60 + s2 + ms2 / 1000.0, 1)
                dur_sec = round(max(0.0, end_sec - start_sec), 1)
                idx += 1
                cue_lines = []
                while idx < len(lines) and lines[idx].strip():
                    cleaned_cue = re.sub(r"<[^>]+>", "", lines[idx]).strip()
                    if cleaned_cue:
                        cue_lines.append(cleaned_cue)
                    idx += 1
                clean_txt = " ".join(html.unescape(" ".join(cue_lines)).split()).strip()
                if clean_txt and (not segments or segments[-1]["text"] != clean_txt):
                    segments.append(
                        {
                            "timestamp": format_seconds_to_timestamp(start_sec),
                            "start": start_sec,
                            "duration": dur_sec,
                            "text": clean_txt,
                        }
                    )
            else:
                idx += 1
        return segments

    # 2. Try XML format (<timedtext format="3"><p t="..." d="..."> or <transcript><text start="..." dur="...">)
    try:
        root = ET.fromstring(text_stripped)
        p_nodes = root.findall(".//p")
        if p_nodes:
            for p in p_nodes:
                raw_txt = "".join(p.itertext())
                clean_txt = " ".join(html.unescape(raw_txt).split()).strip()
                if not clean_txt:
                    continue
                t_ms = int(p.attrib.get("t") or 0)
                d_ms = int(p.attrib.get("d") or 0)
                start_sec = round(t_ms / 1000.0, 1)
                dur_sec = round(d_ms / 1000.0, 1)
                if segments and segments[-1]["text"] == clean_txt:
                    continue
                segments.append(
                    {
                        "timestamp": format_seconds_to_timestamp(start_sec),
                        "start": start_sec,
                        "duration": dur_sec,
                        "text": clean_txt,
                    }
                )
        else:
            for t_el in root.findall(".//text"):
                raw_txt = "".join(t_el.itertext())
                clean_txt = " ".join(html.unescape(raw_txt).split()).strip()
                if not clean_txt:
                    continue
                start_sec = round(float(t_el.attrib.get("start") or 0.0), 1)
                dur_sec = round(float(t_el.attrib.get("dur") or 0.0), 1)
                if segments and segments[-1]["text"] == clean_txt:
                    continue
                segments.append(
                    {
                        "timestamp": format_seconds_to_timestamp(start_sec),
                        "start": start_sec,
                        "duration": dur_sec,
                        "text": clean_txt,
                    }
                )
    except Exception as e:
        logger.warning("Failed to parse YouTube timedtext XML", error=str(e))

    return segments


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

    def __init__(self, api_key: Optional[str] = None, pool_manager: Optional[Any] = None):
        self.api_key = api_key if api_key is not None else settings.YOUTUBE_API_KEY
        self.pool_manager = pool_manager
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

    async def _fetch_transcript_via_ytdlp(
        self,
        video_id: str,
        langs: List[str],
        cookies: Dict[str, str],
        proxy_url: Optional[str],
    ) -> Tuple[List[Dict[str, Any]], Optional[str], Optional[str], bool, List[Dict[str, Any]], Optional[str]]:
        """Run yt-dlp (--write-subs --write-auto-subs --sub-format json3 --skip-download) to extract transcript."""
        def _sync_extract() -> Tuple[List[Dict[str, Any]], Optional[str], Optional[str], bool, List[Dict[str, Any]], Optional[str]]:
            try:
                import yt_dlp
            except ImportError:
                return [], None, None, False, [], "yt-dlp not installed"

            with tempfile.TemporaryDirectory() as tmpdir:
                cookie_file = None
                if cookies:
                    cookie_file = os.path.join(tmpdir, "cookies.txt")
                    with open(cookie_file, "w", encoding="utf-8") as cf:
                        cf.write("# Netscape HTTP Cookie File\n")
                        for k, v in cookies.items():
                            if v:
                                cf.write(f".youtube.com\tTRUE\t/\tTRUE\t2147483647\t{k}\t{v}\n")

                # Order of language preferences plus fallback wildcard
                sub_langs = list(dict.fromkeys(langs + ["en.*", "ru.*", "uz.*", "all"]))
                ydl_opts: Dict[str, Any] = {
                    "skip_download": True,
                    "writesubtitles": True,
                    "writeautomaticsub": True,
                    "subtitleslangs": sub_langs[:6],
                    "subtitlesformat": "json3/srv3/ttml/vtt/best",
                    "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
                    "quiet": True,
                    "no_warnings": True,
                    "nocheckcertificate": True,
                    "extractor_args": {
                        "youtube": {
                            "player_client": ["ios", "android_vr", "tv", "web_embedded", "mweb", "web"],
                        }
                    },
                }
                if cookie_file:
                    ydl_opts["cookiefile"] = cookie_file
                if proxy_url:
                    ydl_opts["proxy"] = proxy_url

                avail_langs: List[Dict[str, Any]] = []
                last_err: Optional[str] = None

                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                        if not isinstance(info, dict):
                            return [], None, None, False, [], "yt-dlp returned empty info"

                        manual_subs = info.get("subtitles") or {}
                        auto_subs = info.get("automatic_captions") or {}

                        for l_code, tracks in manual_subs.items():
                            if tracks:
                                avail_langs.append({"code": l_code, "name": l_code, "is_auto": False})
                        for l_code, tracks in auto_subs.items():
                            if tracks and not any(x["code"] == l_code and not x["is_auto"] for x in avail_langs):
                                avail_langs.append({"code": l_code, "name": f"{l_code} (auto-generated)", "is_auto": True})

                        # Pick best language track from manual_subs or auto_subs
                        chosen_lang = None
                        chosen_is_auto = False
                        chosen_tracks = None

                        for pref in langs:
                            for l_code, t_list in manual_subs.items():
                                if (l_code.lower() == pref or l_code.lower().startswith(f"{pref}-")) and t_list:
                                    chosen_lang = l_code
                                    chosen_is_auto = False
                                    chosen_tracks = t_list
                                    break
                            if chosen_tracks:
                                break
                            for l_code, t_list in auto_subs.items():
                                if (l_code.lower() == pref or l_code.lower().startswith(f"{pref}-")) and t_list:
                                    chosen_lang = l_code
                                    chosen_is_auto = True
                                    chosen_tracks = t_list
                                    break
                            if chosen_tracks:
                                break

                        if not chosen_tracks:
                            for l_code, t_list in manual_subs.items():
                                if t_list:
                                    chosen_lang = l_code
                                    chosen_is_auto = False
                                    chosen_tracks = t_list
                                    break
                        if not chosen_tracks:
                            for l_code, t_list in auto_subs.items():
                                if t_list and l_code != "live_chat":
                                    chosen_lang = l_code
                                    chosen_is_auto = True
                                    chosen_tracks = t_list
                                    break

                        if chosen_tracks and chosen_lang:
                            # Prefer json3 or srv3 or vtt track URL
                            ext_priority = {"json3": 0, "srv3": 1, "srv2": 2, "srv1": 3, "vtt": 4, "ttml": 5}
                            sorted_fmt = sorted(
                                [t for t in chosen_tracks if isinstance(t, dict) and t.get("url")],
                                key=lambda x: ext_priority.get(str(x.get("ext") or "").lower(), 99),
                            )
                            for fmt in sorted_fmt[:3]:
                                track_url = fmt["url"]
                                try:
                                    with ydl.urlopen(track_url) as resp_stream:
                                        raw_bytes = resp_stream.read()
                                        raw_str = raw_bytes.decode("utf-8", errors="replace")
                                        parsed_segs = parse_youtube_timedtext(raw_str)
                                        if parsed_segs:
                                            lang_label = f"{chosen_lang} (auto-generated)" if chosen_is_auto else chosen_lang
                                            return parsed_segs, chosen_lang, lang_label, chosen_is_auto, avail_langs, None
                                except Exception as dl_err:
                                    last_err = str(dl_err)
                except Exception as y_err:
                    last_err = str(y_err)

                return [], None, None, False, avail_langs, last_err

        return await asyncio.to_thread(_sync_extract)

    async def _fetch_transcript_via_playwright(
        self,
        video_id: str,
        langs: List[str],
        cookies: Dict[str, str],
        proxy_url: Optional[str],
    ) -> Tuple[List[Dict[str, Any]], Optional[str], Optional[str], bool, List[Dict[str, Any]], Optional[str]]:
        """Headless Chromium fallback: loads the YouTube watch page in a real browser context and fetches captionTracks."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return [], None, None, False, [], "playwright not installed"

        pw_cookies = []
        for name, val in cookies.items():
            if val:
                pw_cookies.append(
                    {
                        "name": str(name),
                        "value": str(val),
                        "domain": ".youtube.com",
                        "path": "/",
                        "secure": True,
                        "sameSite": "Lax",
                    }
                )
        pw_cookies.append(
            {
                "name": "CONSENT",
                "value": "YES+cb.20240101-17-p0.en+FX+999",
                "domain": ".youtube.com",
                "path": "/",
                "secure": True,
                "sameSite": "Lax",
            }
        )

        proxy_cfg = {"server": proxy_url} if proxy_url else None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    proxy=proxy_cfg,
                    args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
                )
                try:
                    context = await browser.new_context(
                        user_agent=YT_WEB_HEADERS["User-Agent"],
                        locale="en-US",
                    )
                    await context.add_cookies(pw_cookies)
                    page = await context.new_page()

                    intercepted_timedtext: List[str] = []

                    async def on_response(resp):
                        if "/api/timedtext" in resp.url and resp.status == 200:
                            try:
                                txt = await resp.text()
                                if txt and txt.strip():
                                    intercepted_timedtext.append(txt)
                            except Exception:
                                pass

                    page.on("response", on_response)
                    await page.goto(
                        f"https://www.youtube.com/watch?v={video_id}",
                        wait_until="domcontentloaded",
                        timeout=25000,
                    )
                    await page.wait_for_timeout(2000)

                    pr_info = await page.evaluate(
                        """() => {
                            const pr = window.ytInitialPlayerResponse || {};
                            const ps = pr.playabilityStatus || {};
                            const tracks = (((pr.captions || {}).playerCaptionsTracklistRenderer || {}).captionTracks) || [];
                            return {
                                status: ps.status || null,
                                reason: ps.reason || null,
                                tracks: tracks
                            };
                        }"""
                    )

                    play_reason = pr_info.get("reason")
                    tracks = pr_info.get("tracks") or []
                    avail_langs: List[Dict[str, Any]] = []
                    for tr in tracks:
                        l_code = str(tr.get("languageCode") or "")
                        name_obj = tr.get("name") or {}
                        l_name = (
                            name_obj.get("simpleText")
                            or "".join(r.get("text", "") for r in (name_obj.get("runs") or []))
                            or l_code
                        )
                        avail_langs.append({"code": l_code, "name": l_name, "is_auto": tr.get("kind") == "asr"})

                    if tracks:
                        def _rank(t: Dict[str, Any]) -> Tuple[int, int]:
                            code = str(t.get("languageCode") or "").lower()
                            base_c = code.split("-")[0]
                            l_idx = 999
                            for idx, pref in enumerate(langs):
                                if code == pref or base_c == pref:
                                    l_idx = idx
                                    break
                            return (l_idx, 1 if t.get("kind") == "asr" else 0)

                        best = sorted(tracks, key=_rank)[0]
                        burl = best.get("baseUrl")
                        s_lang = best.get("languageCode")
                        name_obj = best.get("name") or {}
                        s_name = (
                            name_obj.get("simpleText")
                            or "".join(r.get("text", "") for r in (name_obj.get("runs") or []))
                            or s_lang
                        )
                        s_auto = best.get("kind") == "asr"
                        if burl:
                            raw_sub = await page.evaluate(
                                """async (url) => {
                                    try {
                                        const r = await fetch(url + '&fmt=json3', {credentials: 'include'});
                                        const t = await r.text();
                                        if (t && t.trim().length > 0) return t;
                                        const r2 = await fetch(url, {credentials: 'include'});
                                        return await r2.text();
                                    } catch (e) {
                                        return '';
                                    }
                                }""",
                                burl,
                            )
                            segs = parse_youtube_timedtext(raw_sub or "")
                            if segs:
                                return segs, s_lang, s_name, s_auto, avail_langs, None

                    # Click subtitle button in player if available to trigger network timedtext
                    if not intercepted_timedtext:
                        try:
                            sub_btn = await page.query_selector("button.ytp-subtitles-button")
                            if sub_btn:
                                await sub_btn.click()
                                await page.wait_for_timeout(1500)
                        except Exception:
                            pass

                    for raw_tt in intercepted_timedtext:
                        segs = parse_youtube_timedtext(raw_tt)
                        if segs:
                            return segs, "auto", "Auto-detected", True, avail_langs, None

                    return [], None, None, False, avail_langs, play_reason
                finally:
                    await browser.close()
        except Exception as pw_err:
            return [], None, None, False, [], str(pw_err)

    async def fetch_video_details_and_transcript(
        self,
        video_id: str,
        preferred_langs: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Fetch comprehensive YouTube video metadata, chapters, and timestamped transcript via multi-engine waterfall."""
        vid = str(video_id).strip()
        if not vid:
            raise ScraperError("invalid video id", "Missing YouTube video ID", status_code=400)

        langs = [l.strip().lower() for l in (preferred_langs or ["uz", "ru", "en", "tr", "az", "kk"]) if l.strip()]

        cookies: Dict[str, str] = {}
        proxy_url: Optional[str] = None
        if self.pool_manager:
            try:
                session_tuple = await self.pool_manager.acquire_session("youtube")
                if session_tuple:
                    _, s_cookies, s_proxy = session_tuple
                    if isinstance(s_cookies, dict):
                        cookies = s_cookies
                    proxy_url = s_proxy
            except Exception as e:
                logger.debug("Could not acquire YouTube session from pool", error=str(e))

        video_meta: Dict[str, Any] = {
            "video_id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "title": None,
            "channel_name": None,
            "channel_id": None,
            "published_at": None,
            "duration_seconds": 0,
            "duration_formatted": "00:00",
            "view_count": 0,
            "like_count": 0,
            "comment_count": 0,
            "description": "",
            "tags": [],
            "category": None,
            "chapters": [],
        }

        caption_tracks: List[Dict[str, Any]] = []
        found_video = False
        playability_status: Optional[str] = None
        playability_reason: Optional[str] = None
        visitor_data: Optional[str] = None
        extraction_engine: Optional[str] = None

        # 1. Enrich metadata first via official YouTube Data API v3 if API key is configured
        if self.api_key:
            try:
                v_resp = await self.http_client.get(
                    "https://www.googleapis.com/youtube/v3/videos",
                    params={
                        "key": self.api_key,
                        "part": "snippet,contentDetails,statistics",
                        "id": vid,
                    },
                )
                if v_resp.status_code == 200:
                    items = (v_resp.json() or {}).get("items") or []
                    if items:
                        found_video = True
                        item = items[0]
                        snip = item.get("snippet") or {}
                        stats = item.get("statistics") or {}
                        cdet = item.get("contentDetails") or {}

                        video_meta["title"] = snip.get("title") or video_meta["title"]
                        video_meta["channel_name"] = snip.get("channelTitle") or video_meta["channel_name"]
                        video_meta["channel_id"] = snip.get("channelId") or video_meta["channel_id"]
                        video_meta["published_at"] = snip.get("publishedAt") or video_meta["published_at"]
                        if snip.get("description"):
                            video_meta["description"] = snip["description"]
                        if snip.get("tags"):
                            video_meta["tags"] = snip["tags"]

                        dur_iso = parse_iso8601_duration(cdet.get("duration"))
                        if dur_iso > 0:
                            video_meta["duration_seconds"] = dur_iso
                            video_meta["duration_formatted"] = format_seconds_to_timestamp(dur_iso)

                        video_meta["view_count"] = int(stats.get("viewCount") or video_meta["view_count"] or 0)
                        video_meta["like_count"] = int(stats.get("likeCount") or video_meta["like_count"] or 0)
                        video_meta["comment_count"] = int(stats.get("commentCount") or video_meta["comment_count"] or 0)
            except Exception as e:
                logger.warning("YouTube Data API v3 video metadata lookup failed", video_id=vid, error=str(e))

        # 2. Fetch watch page to extract visitorData + initial player response
        try:
            watch_resp = await self.http_client.get(f"https://www.youtube.com/watch?v={vid}")
            if watch_resp.status_code == 200:
                html_text = watch_resp.text
                m_vis = re.search(r'"visitorData"\s*:\s*"([^"]+)"', html_text)
                if m_vis:
                    visitor_data = m_vis.group(1)

                m_pr = re.search(r"var ytInitialPlayerResponse\s*=\s*(\{.*?\});", html_text)
                if m_pr:
                    pr_data = json.loads(m_pr.group(1))
                    ps_obj = pr_data.get("playabilityStatus") or {}
                    play_st = ps_obj.get("status")
                    play_rsn = ps_obj.get("reason")
                    if play_st:
                        playability_status = play_st
                    if play_rsn:
                        playability_reason = play_rsn

                    vd = pr_data.get("videoDetails") or {}
                    if vd and vd.get("videoId") == vid and play_st != "ERROR":
                        found_video = True
                        video_meta["title"] = video_meta["title"] or vd.get("title")
                        video_meta["channel_name"] = video_meta["channel_name"] or vd.get("author")
                        video_meta["channel_id"] = video_meta["channel_id"] or vd.get("channelId")
                        if not video_meta["duration_seconds"]:
                            d_sec = int(vd.get("lengthSeconds") or 0)
                            video_meta["duration_seconds"] = d_sec
                            video_meta["duration_formatted"] = format_seconds_to_timestamp(d_sec)
                        if not video_meta["view_count"]:
                            video_meta["view_count"] = int(vd.get("viewCount") or 0)
                        if not video_meta["description"]:
                            video_meta["description"] = vd.get("shortDescription") or ""
                        if not video_meta["tags"]:
                            video_meta["tags"] = vd.get("keywords") or []

                    web_tracks = (
                        (pr_data.get("captions") or {})
                        .get("playerCaptionsTracklistRenderer", {})
                        .get("captionTracks")
                        or []
                    )
                    if web_tracks:
                        caption_tracks = web_tracks
                        extraction_engine = "watch_html"
        except Exception as e:
            logger.warning("YouTube watch HTML fetch failed", video_id=vid, error=str(e))

        # 3. InnerTube Multi-Client Player Waterfall (using isolated cookie-free client + visitorData so ANDROID/IOS never 400s)
        innertube_clients = [
            (
                "ANDROID",
                {
                    "clientName": "ANDROID",
                    "clientVersion": "20.10.38",
                    "androidSdkVersion": 34,
                    "hl": "en",
                    "gl": "US",
                },
                YT_ANDROID_UA,
            ),
            (
                "IOS",
                {
                    "clientName": "IOS",
                    "clientVersion": "19.45.4",
                    "deviceMake": "Apple",
                    "deviceModel": "iPhone16,2",
                    "osName": "iPhone",
                    "osVersion": "18.1.0.22B83",
                    "hl": "en",
                    "gl": "US",
                },
                "com.google.ios.youtube/19.45.4 (iPhone16,2; U; CPU iOS 18_1_0 like Mac OS X;)",
            ),
            (
                "ANDROID_VR",
                {
                    "clientName": "ANDROID_VR",
                    "clientVersion": "1.60.19",
                    "androidSdkVersion": 32,
                    "hl": "en",
                    "gl": "US",
                },
                "com.google.android.apps.youtube.vr.oculus/1.60.19 (Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip",
            ),
        ]

        transport = httpx.AsyncHTTPTransport(proxy=proxy_url) if proxy_url else None
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, transport=transport) as clean_client:
            for c_label, c_ctx, c_ua in innertube_clients:
                if caption_tracks:
                    break
                try:
                    client_payload = dict(c_ctx)
                    if visitor_data:
                        client_payload["visitorData"] = visitor_data
                    req_headers = {
                        "User-Agent": c_ua,
                        "Content-Type": "application/json",
                        "Accept-Language": "en-US,en;q=0.9",
                    }
                    if visitor_data:
                        req_headers["X-Goog-Visitor-Id"] = visitor_data

                    player_resp = await clean_client.post(
                        "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
                        json={
                            "context": {"client": client_payload},
                            "videoId": vid,
                            "contentCheckOk": True,
                            "racyCheckOk": True,
                        },
                        headers=req_headers,
                    )
                    if player_resp.status_code == 200:
                        pdata = player_resp.json() or {}
                        ps_obj = pdata.get("playabilityStatus") or {}
                        play_st = ps_obj.get("status")
                        play_rsn = ps_obj.get("reason")
                        if play_st and (not playability_status or play_st == "OK"):
                            playability_status = play_st
                            if play_st == "OK":
                                playability_reason = None
                        if play_rsn and playability_status != "OK":
                            playability_reason = play_rsn

                        v_details = pdata.get("videoDetails") or {}
                        if v_details and v_details.get("videoId") == vid and play_st != "ERROR":
                            found_video = True
                            video_meta["title"] = video_meta["title"] or v_details.get("title")
                            video_meta["channel_name"] = video_meta["channel_name"] or v_details.get("author")
                            video_meta["channel_id"] = video_meta["channel_id"] or v_details.get("channelId")
                            if not video_meta["duration_seconds"]:
                                dur_sec = int(v_details.get("lengthSeconds") or 0)
                                video_meta["duration_seconds"] = dur_sec
                                video_meta["duration_formatted"] = format_seconds_to_timestamp(dur_sec)
                            if not video_meta["view_count"]:
                                video_meta["view_count"] = int(v_details.get("viewCount") or 0)
                            if not video_meta["description"]:
                                video_meta["description"] = v_details.get("shortDescription") or ""
                            if not video_meta["tags"]:
                                video_meta["tags"] = v_details.get("keywords") or []

                        micro = (pdata.get("microformat") or {}).get("playerMicroformatRenderer") or {}
                        if micro:
                            video_meta["published_at"] = (
                                video_meta["published_at"] or micro.get("publishDate") or micro.get("uploadDate")
                            )
                            video_meta["category"] = video_meta["category"] or micro.get("category")

                        tracks = (
                            (pdata.get("captions") or {})
                            .get("playerCaptionsTracklistRenderer", {})
                            .get("captionTracks")
                            or []
                        )
                        if tracks:
                            caption_tracks = tracks
                            extraction_engine = f"innertube_{c_label.lower()}"
                            break
                except Exception as e:
                    logger.debug("InnerTube client attempt failed", client=c_label, video_id=vid, error=str(e))

            if not found_video:
                raise ScraperError("video not found", f"YouTube video '{vid}' not found or unavailable", status_code=404)

            video_meta["chapters"] = extract_chapters_from_description(video_meta.get("description"))

            # 4. Select best caption track from InnerTube if available
            available_languages: List[Dict[str, Any]] = []
            for tr in caption_tracks:
                l_code = str(tr.get("languageCode") or "")
                name_obj = tr.get("name") or {}
                l_name = (
                    name_obj.get("simpleText")
                    or "".join(r.get("text", "") for r in (name_obj.get("runs") or []))
                    or l_code
                )
                is_auto = tr.get("kind") == "asr"
                available_languages.append({"code": l_code, "name": l_name, "is_auto": is_auto})

            segments: List[Dict[str, Any]] = []
            selected_lang: Optional[str] = None
            selected_lang_name: Optional[str] = None
            selected_is_auto = False

            if caption_tracks:
                def _track_rank(t: Dict[str, Any]) -> Tuple[int, int]:
                    code = str(t.get("languageCode") or "").lower()
                    base_code = code.split("-")[0]
                    lang_idx = 999
                    for idx, pref in enumerate(langs):
                        if code == pref or base_code == pref:
                            lang_idx = idx
                            break
                    is_asr = 1 if t.get("kind") == "asr" else 0
                    return (lang_idx, is_asr)

                selected_track = sorted(caption_tracks, key=_track_rank)[0]
                if selected_track and selected_track.get("baseUrl"):
                    base_url = selected_track["baseUrl"]
                    selected_lang = selected_track.get("languageCode")
                    name_obj = selected_track.get("name") or {}
                    selected_lang_name = (
                        name_obj.get("simpleText")
                        or "".join(r.get("text", "") for r in (name_obj.get("runs") or []))
                        or selected_lang
                    )
                    selected_is_auto = selected_track.get("kind") == "asr"

                    dl_headers = {"User-Agent": YT_ANDROID_UA}
                    if visitor_data:
                        dl_headers["X-Goog-Visitor-Id"] = visitor_data
                    try:
                        t_resp = await clean_client.get(base_url, headers=dl_headers)
                        if t_resp.status_code == 200 and t_resp.text.strip():
                            segments = parse_youtube_timedtext(t_resp.text)
                        if not segments:
                            t_resp_j = await clean_client.get(f"{base_url}&fmt=json3", headers=dl_headers)
                            if t_resp_j.status_code == 200 and t_resp_j.text.strip():
                                segments = parse_youtube_timedtext(t_resp_j.text)
                    except Exception as e:
                        logger.warning("Failed to download YouTube caption track", video_id=vid, error=str(e))

        # 5. Fallback Engine A: yt-dlp (--write-subs --write-auto-subs --sub-format json3 --skip-download)
        if not segments:
            ytdlp_segs, ytdlp_lang, ytdlp_name, ytdlp_auto, ytdlp_avail, ytdlp_err = await self._fetch_transcript_via_ytdlp(
                video_id=vid,
                langs=langs,
                cookies=cookies,
                proxy_url=proxy_url,
            )
            if ytdlp_avail and not available_languages:
                available_languages = ytdlp_avail
            if ytdlp_segs:
                segments = ytdlp_segs
                selected_lang = ytdlp_lang
                selected_lang_name = ytdlp_name
                selected_is_auto = ytdlp_auto
                extraction_engine = "yt_dlp"
                playability_status = "OK"
                playability_reason = None
            elif ytdlp_err and not playability_reason and ("bot" in ytdlp_err.lower() or "sign in" in ytdlp_err.lower()):
                playability_status = playability_status or "LOGIN_REQUIRED"
                playability_reason = ytdlp_err

        # 6. Fallback Engine B: Playwright headless Chromium (executes BotGuard JS / poToken in real browser)
        if not segments:
            pw_segs, pw_lang, pw_name, pw_auto, pw_avail, pw_rsn = await self._fetch_transcript_via_playwright(
                video_id=vid,
                langs=langs,
                cookies=cookies,
                proxy_url=proxy_url,
            )
            if pw_avail and not available_languages:
                available_languages = pw_avail
            if pw_segs:
                segments = pw_segs
                selected_lang = pw_lang
                selected_lang_name = pw_name
                selected_is_auto = pw_auto
                extraction_engine = "playwright"
                playability_status = "OK"
                playability_reason = None
            elif pw_rsn and not playability_reason:
                playability_reason = pw_rsn

        # Group segments into ~20-second blocks where each block's header [MM:SS] matches an exact segment timestamp
        grouped_lines: List[str] = []
        bucket_start: Optional[float] = None
        bucket_ts_str: Optional[str] = None
        bucket_texts: List[str] = []
        for seg in segments:
            s_time = float(seg.get("start") or 0.0)
            s_ts = seg.get("timestamp") or format_seconds_to_timestamp(s_time)
            s_txt = seg.get("text") or ""
            if not s_txt:
                continue
            if bucket_start is None:
                bucket_start = s_time
                bucket_ts_str = s_ts
                bucket_texts = [s_txt]
            elif s_time - bucket_start >= 20.0:
                grouped_lines.append(f"[{bucket_ts_str}] {' '.join(bucket_texts)}")
                bucket_start = s_time
                bucket_ts_str = s_ts
                bucket_texts = [s_txt]
            else:
                bucket_texts.append(s_txt)
        if bucket_start is not None and bucket_ts_str and bucket_texts:
            grouped_lines.append(f"[{bucket_ts_str}] {' '.join(bucket_texts)}")

        full_transcript_text = "\n".join(grouped_lines)

        unavailable_reason: Optional[str] = None
        if not segments:
            if playability_status in ("LOGIN_REQUIRED", "UNPLAYABLE") or (
                playability_reason and ("bot" in playability_reason.lower() or "sign in" in playability_reason.lower())
            ):
                unavailable_reason = (
                    f"YouTube bot-check blocked anonymous transcript extraction "
                    f"(playabilityStatus={playability_status or 'LOGIN_REQUIRED'}: "
                    f"{playability_reason or 'Sign in to confirm you are not a bot'}). "
                    f"Add a YouTube session or proxy via 'ops sessions --add youtube <account> <cookies_json>'."
                )
            else:
                unavailable_reason = "Video is playable but has no manual or auto-generated caption tracks."

        return {
            "video": video_meta,
            "transcript": {
                "available": len(segments) > 0,
                "extraction_engine": extraction_engine if segments else None,
                "playability_status": "OK" if segments else (playability_status or "OK"),
                "playability_reason": None if segments else playability_reason,
                "unavailable_reason": unavailable_reason,
                "language": selected_lang,
                "language_name": selected_lang_name,
                "is_auto_generated": selected_is_auto,
                "available_languages": available_languages,
                "segment_count": len(segments),
                "full_text": full_transcript_text,
                "segments": segments,
            },
        }
