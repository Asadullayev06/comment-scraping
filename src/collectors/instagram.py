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
