"""X (Twitter) Collector supporting both Official API v2 (Bearer Token) and GraphQL TweetDetail (Burner Cookies)."""

import json
from typing import AsyncIterator, Optional, Dict, Any, List
from datetime import datetime, timezone
import urllib.parse
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
        bearer_token: Optional[str] = None,
    ):
        self.pool_manager = pool_manager
        self.query_id = query_id or settings.X_TWEET_DETAIL_QUERY_ID
        self.bearer_token = bearer_token if bearer_token is not None else settings.X_BEARER_TOKEN

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def _fetch_api_v2_replies(
        self,
        tweet_id: str,
        cursor: Optional[str] = None,
        limit: int = 500,
    ) -> AsyncIterator[NormalizedComment]:
        """Fetch replies to a post using X Official API v2 recent search (conversation_id:<tweet_id>)."""
        raw_token = urllib.parse.unquote(self.bearer_token or "").strip()
        if raw_token.lower().startswith("bearer "):
            raw_token = raw_token[7:].strip()

        headers = {
            "Authorization": f"Bearer {raw_token}",
            "User-Agent": "OperativesMonitor/2.0",
        }
        next_token = cursor
        total_yielded = 0

        async with httpx.AsyncClient(headers=headers, timeout=25.0) as client:
            while total_yielded < limit:
                params: Dict[str, Any] = {
                    "query": f"conversation_id:{tweet_id}",
                    "max_results": min(100, max(10, limit - total_yielded)),
                    "tweet.fields": "id,text,author_id,created_at,conversation_id,in_reply_to_user_id",
                    "expansions": "author_id",
                    "user.fields": "id,name,username",
                }
                if next_token:
                    params["next_token"] = next_token

                resp = None
                for base_host in ("https://api.x.com", "https://api.twitter.com"):
                    try:
                        resp = await client.get(f"{base_host}/2/tweets/search/recent", params=params)
                        if resp.status_code != 404:
                            break
                    except Exception as req_err:
                        logger.warning("X API v2 request error", host=base_host, error=str(req_err))

                if resp is None or resp.status_code != 200:
                    logger.warning(
                        "X API v2 search returned non-200",
                        status=resp.status_code if resp else None,
                        body=resp.text[:200] if resp else None,
                    )
                    break

                payload = resp.json()
                tweets = payload.get("data") or []
                if not tweets:
                    break

                users_list = (payload.get("includes") or {}).get("users") or []
                user_map: Dict[str, Dict[str, str]] = {
                    str(u.get("id")): {
                        "username": u.get("username") or "unknown",
                        "name": u.get("name") or u.get("username") or "User",
                    }
                    for u in users_list
                    if u.get("id")
                }

                for tw in tweets:
                    reply_id = str(tw.get("id") or "")
                    if not reply_id or reply_id == str(tweet_id):
                        continue
                    author_id = str(tw.get("author_id") or "unknown")
                    u_info = user_map.get(author_id, {"username": None, "name": None})
                    username = u_info.get("username")
                    display_name = u_info.get("name")
                    text = tw.get("text") or ""
                    created_str = tw.get("created_at")
                    pub_dt = datetime.now(timezone.utc)
                    if created_str:
                        try:
                            pub_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                        except Exception:
                            pass

                    yield NormalizedComment(
                        platform="x",
                        external_comment_id=f"x_{reply_id}",
                        parent_external_id=str(tweet_id),
                        target_external_id=str(tweet_id),
                        source_name=f"X Post @{username}" if username else f"X Post {tweet_id}",
                        post_url=f"https://x.com/i/status/{tweet_id}",
                        comment_url=f"https://x.com/{username}/status/{reply_id}" if username else f"https://x.com/i/status/{reply_id}",
                        author_external_id=author_id,
                        author_username=username,
                        author_display_name=display_name,
                        content_text=text,
                        published_at=pub_dt,
                    )
                    total_yielded += 1

                meta = payload.get("meta") or {}
                next_token = meta.get("next_token")
                if not next_token:
                    break

    def _extract_tweet_result(self, tweet_res: Dict[str, Any], focal_tweet_id: str) -> Optional[NormalizedComment]:
        """Parse a single GraphQL tweet_results object into a NormalizedComment."""
        if not isinstance(tweet_res, dict):
            return None
        if tweet_res.get("__typename") == "TweetWithVisibilityResults" and "tweet" in tweet_res:
            tweet_res = tweet_res.get("tweet") or {}

        legacy = tweet_res.get("legacy") or {}
        reply_id = str(legacy.get("id_str") or tweet_res.get("rest_id") or "")
        if not reply_id or reply_id == str(focal_tweet_id):
            return None

        user_result = ((tweet_res.get("core") or {}).get("user_results") or {}).get("result") or {}
        user_legacy = user_result.get("legacy") or {}
        user_core = user_result.get("core") or {}
        username = user_legacy.get("screen_name") or user_core.get("screen_name")
        display_name = user_legacy.get("name") or user_core.get("name")
        text = legacy.get("full_text") or ""
        if not text:
            return None

        return NormalizedComment(
            platform="x",
            external_comment_id=f"x_{reply_id}",
            parent_external_id=str(focal_tweet_id),
            target_external_id=str(focal_tweet_id),
            source_name=f"X Post @{username}" if username else f"X Post {focal_tweet_id}",
            post_url=f"https://x.com/i/status/{focal_tweet_id}",
            comment_url=f"https://x.com/{username}/status/{reply_id}" if username else f"https://x.com/i/status/{reply_id}",
            author_external_id=str(user_result.get("rest_id") or username or "unknown"),
            author_username=username,
            author_display_name=display_name,
            content_text=text,
            published_at=datetime.now(timezone.utc),
        )

    async def fetch_comments(
        self,
        target: MonitoredTarget,
        cursor: Optional[str] = None,
        limit: int = 5000,
    ) -> AsyncIterator[NormalizedComment]:
        """Paginate replies to an X post using API v2 Bearer Token or GraphQL Burner Session."""
        tweet_id = target.external_id
        total_yielded = 0

        # Engine 1: Official X API v2 Bearer Token (if configured)
        if self.bearer_token:
            async for comment in self._fetch_api_v2_replies(tweet_id=tweet_id, cursor=cursor, limit=limit):
                yield comment
                total_yielded += 1
            if total_yielded > 0:
                return

        # Engine 2: GraphQL TweetDetail with Burner Session Cookies (auth_token + ct0)
        if not self.pool_manager:
            logger.warning("No pool manager configured for X scraper; skipping")
            return

        session_tuple = await self.pool_manager.acquire_session("x")
        if not session_tuple:
            logger.warning("No active X burner session or X_BEARER_TOKEN found.")
            return

        session_obj, cookies, proxy_url = session_tuple
        if isinstance(cookies, str):
            # Support raw cookie string or single auth_token
            parsed_cookies: Dict[str, str] = {}
            for part in cookies.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    parsed_cookies[k.strip()] = v.strip()
            if not parsed_cookies:
                parsed_cookies["auth_token"] = cookies.strip()
            cookies = parsed_cookies

        headers = {
            "authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
            "x-csrf-token": cookies.get("ct0", ""),
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
            "user-agent": session_obj.user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "content-type": "application/json",
            "Referer": f"https://x.com/i/status/{tweet_id}",
        }

        current_cursor = cursor
        async with httpx.AsyncClient(cookies=cookies, proxy=proxy_url, headers=headers, timeout=25.0) as client:
            while total_yielded < limit:
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
                            content = entry.get("content") or {}

                            if entry_id.startswith("cursor-bottom-"):
                                next_cursor = (content.get("itemContent") or {}).get("value") or content.get("value")

                            # Direct itemContent
                            item_content = content.get("itemContent") or {}
                            tweet_res = (item_content.get("tweet_results") or {}).get("result")
                            if tweet_res:
                                norm = self._extract_tweet_result(tweet_res, tweet_id)
                                if norm:
                                    yield norm
                                    total_yielded += 1

                            # Threaded module items (conversationthreads)
                            for mod_item in content.get("items") or []:
                                mod_ic = (mod_item.get("item") or {}).get("itemContent") or {}
                                mod_tr = (mod_ic.get("tweet_results") or {}).get("result")
                                if mod_tr:
                                    norm = self._extract_tweet_result(mod_tr, tweet_id)
                                    if norm:
                                        yield norm
                                        total_yielded += 1

                    if not next_cursor or next_cursor == current_cursor:
                        break
                    current_cursor = next_cursor

                except Exception as e:
                    logger.error("X scraper iteration failed", error=str(e))
                    break
