"""Instagram Comment Collector using mobile API (instagrapi) and Web REST API fallback with single-account pinning."""

import asyncio
import re
import random
from typing import AsyncIterator, Optional, Dict, Any, List
from datetime import datetime, timezone
import structlog
import httpx
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

    def _safe_init_sessionid(self, cl: Client, sessionid: str) -> None:
        """Safely inject sessionid and headers without crashing on GraphQL user lookups."""
        user_match = re.search(r"^(\d+)", sessionid)
        user_id = user_match.group(1) if user_match else "0"

        cl.settings["cookies"] = {"sessionid": sessionid, "ds_user_id": user_id}
        cl.init()
        cl.authorization_data = {
            "ds_user_id": user_id,
            "sessionid": sessionid,
            "should_use_header_over_cookies": True,
        }
        cl.private.cookies.set("sessionid", sessionid)
        cl.private.cookies.set("ds_user_id", user_id)
        cl.private.headers.update(cl.base_headers)
        if cl.authorization:
            cl.private.headers.update({"Authorization": cl.authorization})
        cl.username = "burner"

    def _init_client(self, session_data: Any, proxy_url: Optional[str]) -> Client:
        """Initialize instagrapi Client with authenticated mobile settings and proxy."""
        cl = Client()
        if proxy_url:
            cl.set_proxy(proxy_url)

        if isinstance(session_data, dict):
            if "sessionid" in session_data and len(session_data) <= 3:
                self._safe_init_sessionid(cl, session_data["sessionid"])
            else:
                cl.set_settings(session_data)
        elif isinstance(session_data, str):
            self._safe_init_sessionid(cl, session_data)

        return cl

    async def fetch_profile_recent_posts(
        self,
        username: str,
        count: int = 5,
    ) -> List[Dict[str, Any]]:
        """Fetch a user's latest N posts by handle/username, sorted newest-first (1 = most recent).

        Uses Instagram Web REST API (/api/v1/feed/user/<username>/username/ and web_profile_info)
        with fallback to instagrapi mobile API (user_medias_v1).
        """
        clean_username = username.strip().lstrip("@").split("?")[0].strip("/")
        if "instagram.com/" in clean_username:
            clean_username = clean_username.split("instagram.com/")[-1].split("/")[0].lstrip("@")

        if not clean_username:
            return []

        session_data: Any = {}
        proxy_url: Optional[str] = None
        if self.pool_manager:
            session_tuple = await self.pool_manager.acquire_session("instagram")
            if session_tuple:
                _, session_data, proxy_url = session_tuple

        session_val = session_data.get("sessionid") if isinstance(session_data, dict) else (str(session_data) if session_data else "")
        user_match = re.search(r"^(\d+)", session_val) if session_val else None
        user_id = user_match.group(1) if user_match else ""

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "x-ig-app-id": "936619743392459",
            "x-asbd-id": "129477",
            "x-requested-with": "XMLHttpRequest",
            "Referer": f"https://www.instagram.com/{clean_username}/",
        }
        cookies = {}
        if session_val:
            cookies["sessionid"] = session_val
        if user_id:
            cookies["ds_user_id"] = user_id

        # Fetch extra items so pinned older posts at the top of the grid don't displace actual newest posts
        fetch_amount = max(count + 4, 12)
        raw_posts: List[Dict[str, Any]] = []

        transport = httpx.AsyncHTTPTransport(proxy=proxy_url) if proxy_url else None
        async with httpx.AsyncClient(headers=headers, cookies=cookies, transport=transport, timeout=20.0) as client:
            # Engine 1: Web REST User Feed API
            try:
                feed_url = f"https://www.instagram.com/api/v1/feed/user/{clean_username}/username/?count={fetch_amount}"
                resp = await client.get(feed_url)
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("items", [])
                    for item in items:
                        shortcode = item.get("code")
                        if not shortcode:
                            continue
                        media_pk = str(item.get("pk") or item.get("id") or shortcode)
                        ts = int(item.get("taken_at") or 0)
                        pub_dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts > 0 else None
                        cap_obj = item.get("caption")
                        caption_text = cap_obj.get("text", "") if isinstance(cap_obj, dict) else ""
                        comment_count = int(item.get("comment_count") or 0)
                        like_count = int(item.get("like_count") or 0)
                        thumb_url = None
                        img_v2 = item.get("image_versions2", {})
                        candidates = img_v2.get("candidates", []) if isinstance(img_v2, dict) else []
                        if candidates and isinstance(candidates[0], dict):
                            thumb_url = candidates[0].get("url")

                        raw_posts.append(
                            {
                                "shortcode": shortcode,
                                "media_id": media_pk,
                                "url": f"https://www.instagram.com/p/{shortcode}/",
                                "thumbnail_url": thumb_url,
                                "caption": " ".join(caption_text.split())[:160],
                                "comment_count": comment_count,
                                "like_count": like_count,
                                "published_at": pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else None,
                                "timestamp": ts,
                            }
                        )
            except Exception as e1:
                logger.warning("Instagram Web Feed API failed; trying web_profile_info", username=clean_username, error=str(e1))

            # Engine 2: Web Profile Info API fallback
            if not raw_posts:
                try:
                    info_url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={clean_username}"
                    resp2 = await client.get(info_url)
                    if resp2.status_code == 200:
                        data2 = resp2.json()
                        edges = (
                            data2.get("data", {})
                            .get("user", {})
                            .get("edge_owner_to_timeline_media", {})
                            .get("edges", [])
                        )
                        for edge in edges:
                            node = edge.get("node", {})
                            shortcode = node.get("shortcode")
                            if not shortcode:
                                continue
                            media_pk = str(node.get("id") or shortcode)
                            ts = int(node.get("taken_at_timestamp") or 0)
                            pub_dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts > 0 else None
                            cap_edges = node.get("edge_media_to_caption", {}).get("edges", [])
                            caption_text = cap_edges[0].get("node", {}).get("text", "") if cap_edges else ""
                            comment_count = int(node.get("edge_media_to_comment", {}).get("count") or 0)
                            like_count = int(node.get("edge_liked_by", {}).get("count") or 0)
                            thumb_url = node.get("thumbnail_src") or node.get("display_url")

                            raw_posts.append(
                                {
                                    "shortcode": shortcode,
                                    "media_id": media_pk,
                                    "url": f"https://www.instagram.com/p/{shortcode}/",
                                    "thumbnail_url": thumb_url,
                                    "caption": " ".join(caption_text.split())[:160],
                                    "comment_count": comment_count,
                                    "like_count": like_count,
                                    "published_at": pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else None,
                                    "timestamp": ts,
                                }
                            )
                except Exception as e2:
                    logger.warning("Instagram web_profile_info fallback failed", username=clean_username, error=str(e2))

        # Engine 3: instagrapi mobile API fallback
        if not raw_posts and session_data:
            try:
                cl = await asyncio.to_thread(self._init_client, session_data, proxy_url)
                u_info = await asyncio.to_thread(cl.user_info_by_username_v1, clean_username)
                medias = await asyncio.to_thread(cl.user_medias_v1, int(u_info.pk), fetch_amount)
                for m in medias:
                    shortcode = getattr(m, "code", None)
                    if not shortcode:
                        continue
                    taken_at = getattr(m, "taken_at", None)
                    if isinstance(taken_at, datetime):
                        pub_dt = taken_at.astimezone(timezone.utc)
                        ts = int(pub_dt.timestamp())
                    else:
                        pub_dt = None
                        ts = 0
                    caption_text = getattr(m, "caption_text", "") or ""
                    thumb = getattr(m, "thumbnail_url", None)
                    raw_posts.append(
                        {
                            "shortcode": shortcode,
                            "media_id": str(getattr(m, "pk", shortcode)),
                            "url": f"https://www.instagram.com/p/{shortcode}/",
                            "thumbnail_url": str(thumb) if thumb else None,
                            "caption": " ".join(caption_text.split())[:160],
                            "comment_count": int(getattr(m, "comment_count", 0) or 0),
                            "like_count": int(getattr(m, "like_count", 0) or 0),
                            "published_at": pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else None,
                            "timestamp": ts,
                        }
                    )
            except Exception as e3:
                logger.warning("instagrapi mobile profile medias fallback failed", username=clean_username, error=str(e3))

        if not raw_posts:
            return []

        # Deduplicate by shortcode and sort strictly newest-first by timestamp so pinned old posts don't skew index 1
        deduped: Dict[str, Dict[str, Any]] = {}
        for p in raw_posts:
            sc = p["shortcode"]
            if sc not in deduped:
                deduped[sc] = p

        sorted_posts = sorted(deduped.values(), key=lambda x: x.get("timestamp", 0), reverse=True)[:count]
        for idx, post in enumerate(sorted_posts, start=1):
            post["post_index"] = idx
            post.pop("timestamp", None)

        return sorted_posts

    async def _fetch_web_comments(
        self,
        shortcode: str,
        media_id: str,
        session_data: Any,
        proxy_url: Optional[str] = None,
        min_id: Optional[str] = None,
        limit: int = 1000,
    ) -> AsyncIterator[NormalizedComment]:
        """Fallback engine: Scrape comments using Instagram's Web REST API with browser cookies."""
        session_val = session_data.get("sessionid") if isinstance(session_data, dict) else str(session_data)
        user_match = re.search(r"^(\d+)", session_val) if session_val else None
        user_id = user_match.group(1) if user_match else ""

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "x-ig-app-id": "936619743392459",
            "x-asbd-id": "129477",
            "x-requested-with": "XMLHttpRequest",
            "Referer": f"https://www.instagram.com/p/{shortcode}/",
        }
        cookies = {}
        if session_val:
            cookies["sessionid"] = session_val
        if user_id:
            cookies["ds_user_id"] = user_id

        transport = httpx.AsyncHTTPTransport(proxy=proxy_url) if proxy_url else None
        async with httpx.AsyncClient(headers=headers, cookies=cookies, transport=transport, timeout=20.0) as client:
            current_min_id = min_id
            total_yielded = 0

            while total_yielded < limit:
                url = f"https://www.instagram.com/api/v1/media/{media_id}/comments/?can_support_threading=true&permalink_enabled=false"
                if current_min_id:
                    url += f"&min_id={current_min_id}"

                try:
                    resp = await client.get(url)
                except Exception as net_err:
                    logger.warning("Network error calling Instagram web API", error=str(net_err))
                    break

                if resp.status_code in [302, 401]:
                    logger.warning("Instagram web API rejected session cookies (login required / challenged)")
                    break
                if resp.status_code != 200:
                    logger.warning("Instagram web API returned non-200", status=resp.status_code)
                    break

                try:
                    data = resp.json()
                except Exception:
                    break

                raw_comments = data.get("comments", [])
                if not raw_comments:
                    break

                for c in raw_comments:
                    pk = str(c.get("pk", ""))
                    user = c.get("user", {})
                    username = user.get("username")
                    full_name = user.get("full_name")
                    user_pk = str(user.get("pk", "unknown"))
                    text = c.get("text", "")
                    created_at_utc = c.get("created_at_utc")
                    if isinstance(created_at_utc, (int, float)):
                        pub_dt = datetime.fromtimestamp(created_at_utc, tz=timezone.utc)
                    else:
                        pub_dt = datetime.now(timezone.utc)

                    child_count = c.get("child_comment_count", 0) or 0

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

                next_min_id = data.get("next_min_id")
                has_more = data.get("has_more_headload_comments") or data.get("has_more_comments")
                if not next_min_id or not has_more or next_min_id == current_min_id:
                    break
                current_min_id = next_min_id
                await asyncio.sleep(random.uniform(2.0, 3.5))

    async def fetch_comments(
        self,
        target: MonitoredTarget,
        cursor: Optional[str] = None,
        limit: int = 5000,
    ) -> AsyncIterator[NormalizedComment]:
        """Paginate comments on an Instagram post using a single pinned burner account.

        Paginates start to finish with human-like jitter, detects challenges,
        and provides fallback to web REST API if mobile API encounters issues.
        """
        shortcode = target.external_id
        if not self.pool_manager:
            logger.warning("No pool manager configured for Instagram collector; skipping")
            return

        # 1. Assign single account to paginate this post from start to finish
        session_tuple = await self.pool_manager.acquire_session("instagram")
        if not session_tuple:
            logger.warning("No healthy Instagram burner session available in pool. Use /add_session instagram ...")
            return

        session_obj, session_data, proxy_url = session_tuple

        # Resolve media_id mathematically from shortcode without network overhead
        try:
            if shortcode.isdigit():
                media_id = str(shortcode)
            else:
                dummy_cl = Client()
                media_id = str(dummy_cl.media_pk_from_code(shortcode))
        except Exception:
            media_id = shortcode

        total_yielded = 0
        min_id = cursor

        logger.info("Starting Instagram post comment extraction", shortcode=shortcode, media_id=media_id, burner=session_obj.account_identifier)

        # Try Engine 1: instagrapi mobile API
        use_web_fallback = False
        try:
            cl = await asyncio.to_thread(self._init_client, session_data, proxy_url)
        except Exception as init_err:
            logger.warning("instagrapi client init failed; switching to web REST engine", error=str(init_err))
            use_web_fallback = True

        if not use_web_fallback:
            while total_yielded < limit:
                try:
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
                    await asyncio.sleep(random.uniform(2.0, 4.0))

                except (ChallengeRequired, FeedbackRequired) as challenge_err:
                    logger.error("Instagram account hit checkpoint challenge", burner=session_obj.account_identifier, error=str(challenge_err))
                    await self.pool_manager.report_challenge(session_obj.id, error_type="ChallengeRequired", cooldown_hours=6)
                    break

                except (RateLimitError, PleaseWaitFewMinutes) as rate_err:
                    logger.warning("Instagram rate-limit encountered; pausing account", burner=session_obj.account_identifier, error=str(rate_err))
                    await self.pool_manager.report_challenge(session_obj.id, error_type="PleaseWaitFewMinutes", cooldown_hours=2)
                    break

                except LoginRequired:
                    logger.warning("Instagram mobile API requested login; trying web REST fallback")
                    use_web_fallback = True
                    break

                except ClientError as client_err:
                    logger.warning("Instagram mobile API error; falling back to web REST engine", error=str(client_err))
                    use_web_fallback = True
                    break

                except Exception as e:
                    logger.warning("Unexpected instagrapi error; attempting web REST fallback", error=str(e))
                    use_web_fallback = True
                    break

        # Engine 2: Web REST API fallback if mobile API failed
        if use_web_fallback and total_yielded < limit:
            logger.info("Engaging Instagram Web REST API scraper engine", shortcode=shortcode)
            try:
                async for norm_comment in self._fetch_web_comments(
                    shortcode=shortcode,
                    media_id=media_id,
                    session_data=session_data,
                    proxy_url=proxy_url,
                    min_id=min_id,
                    limit=limit - total_yielded,
                ):
                    yield norm_comment
                    total_yielded += 1
            except Exception as web_err:
                logger.error("Web REST API engine encountered error", error=str(web_err))

        logger.info(
            "Finished Instagram comment extraction run",
            shortcode=shortcode,
            comments_extracted=total_yielded,
        )
