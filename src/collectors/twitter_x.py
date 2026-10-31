"""X (Twitter) Collector supporting both Official API v2 (Bearer Token) and GraphQL TweetDetail (Burner Cookies + twikit + Playwright fallback)."""

import json
import re
from email.utils import parsedate_to_datetime
from typing import AsyncIterator, Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone
import urllib.parse
import httpx
import structlog
from src.config import settings
from src.collectors.base import BaseCollector, ScraperError
from src.schemas.comment import NormalizedComment
from src.db.models import MonitoredTarget
from src.collectors.pool_manager import SessionPoolManager

logger = structlog.get_logger(__name__)

# Complete GraphQL feature flags required by X's TweetDetail endpoint (matched with twikit 2.3.x)
X_GRAPHQL_FEATURES: Dict[str, Any] = {
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_home_pinned_timelines_enabled": True,
    "blue_business_profile_image_shape_enabled": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "graphql_timeline_v2_bookmark_timeline": True,
    "hidden_profile_likes_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "interactive_text_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_richtext_consumption_enabled": True,
    "profile_foundations_tweet_stats_enabled": True,
    "profile_foundations_tweet_stats_tweet_frequency": True,
    "responsive_web_birdwatch_note_limit_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_media_download_video_enabled": False,
    "responsive_web_text_conversations_enabled": False,
    "responsive_web_twitter_article_data_v2_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": False,
    "responsive_web_twitter_blue_verified_badge_is_enabled": True,
    "rweb_lists_timeline_redesign_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "spaces_2022_h2_clipping": True,
    "spaces_2022_h2_spaces_communities": True,
    "standardized_nudges_misinfo": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "tweet_with_visibility_results_prefer_gql_media_interstitial_enabled": False,
    "tweetypie_unmention_optimization_enabled": True,
    "verified_phone_label_enabled": False,
    "vibe_api_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "articles_preview_enabled": True,
    "rweb_video_timestamps_enabled": True,
}

X_PUBLIC_WEB_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# Candidate TweetDetail GraphQL query IDs (twikit current + common rotations)
X_TWEET_DETAIL_QUERY_IDS = [
    "U0HTv-bAWTBYylwEMT7x5A",
    "_8aYOgEDz35BrBcBal1-_w",
    "QuBlQ6SxNAQCt6-kBiCXCQ",
    "V94IR8nUKvgjwjd8uKnogg",
    "nBS-WpgA6ZG0CyNHD517JQ",
]

X_USER_BY_SCREEN_NAME_QUERY_IDS = [
    "NimuplG1OB7Fd2btCLdBOw",
    "xmU6X_CKVnQ5lSrCbAmJsg",
    "qW5u-DAuXpMEG0zA1F7UGQ",
    "sLVLhk0bGj3MVFEKTdax1w",
]

X_USER_TWEETS_QUERY_IDS = [
    "QWF3SzpHmykQHsQMixG0cg",
    "E3opETHurmVJflFsUBVuUQ",
    "V1ze5q3ijDS1VeLwLY0m7g",
    "HuTx74BxAnezK1gWvYY7zg",
]

X_USER_FEATURES: Dict[str, Any] = {
    "hidden_profile_likes_enabled": True,
    "hidden_profile_subscriptions_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "subscriptions_feature_can_gift_premium": True,
}


def _parse_x_created_at(raw_dt: Optional[str]) -> datetime:
    """Parse either ISO-8601 ('2026-03-01T12:00:00Z') or Twitter Ruby date ('Wed Oct 10 20:19:24 +0000 2018')."""
    if not raw_dt:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
    except Exception:
        pass
    try:
        return parsedate_to_datetime(raw_dt)
    except Exception:
        return datetime.now(timezone.utc)


def _patch_twikit_transaction() -> None:
    """Ensure twikit's ClientTransaction.init never crashes if X updates its ondemand.s script layout."""
    try:
        from twikit.x_client_transaction import ClientTransaction

        if getattr(ClientTransaction, "_ops_patched", False):
            return

        orig_get_indices = ClientTransaction.get_indices

        async def _safe_get_indices(self, home_page_response, session, headers):
            try:
                return await orig_get_indices(self, home_page_response, session, headers)
            except Exception:
                # Fallback to standard key byte indices so ClientTransaction can still generate transaction IDs
                return 2, [12, 14, 7]

        ClientTransaction.get_indices = _safe_get_indices
        ClientTransaction._ops_patched = True
    except Exception:
        pass


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
        self.last_focal_tweet_meta: Optional[Dict[str, Any]] = None

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def _fetch_api_v2_replies(
        self,
        tweet_id: str,
        target: MonitoredTarget,
        cursor: Optional[str] = None,
        limit: int = 500,
    ) -> AsyncIterator[NormalizedComment]:
        """Fetch focal tweet + replies using X Official API v2 Bearer Token."""
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
            # Step 1: Verify the focal tweet exists and fetch its text + author
            focal_params = {
                "tweet.fields": "id,text,author_id,created_at,public_metrics,conversation_id",
                "expansions": "author_id",
                "user.fields": "id,name,username",
            }
            focal_resp = None
            for base_host in ("https://api.x.com", "https://api.twitter.com"):
                try:
                    focal_resp = await client.get(f"{base_host}/2/tweets/{tweet_id}", params=focal_params)
                    if focal_resp.status_code != 404:
                        break
                except Exception as req_err:
                    logger.warning("X API v2 focal tweet check error", host=base_host, error=str(req_err))

            if focal_resp is not None:
                if focal_resp.status_code in (401, 403):
                    raise ScraperError("x session rejected", f"X API v2 Bearer Token rejected (HTTP {focal_resp.status_code})", status_code=401)
                if focal_resp.status_code == 429:
                    raise ScraperError("rate limited", "X API v2 rate limit exceeded (HTTP 429)", status_code=429)
                if focal_resp.status_code == 200:
                    f_json = focal_resp.json()
                    if "errors" in f_json and not f_json.get("data"):
                        raise ScraperError("tweet not found", f"Tweet {tweet_id} not found or deleted", status_code=404)
                    f_data = f_json.get("data") or {}
                    f_users = ((f_json.get("includes") or {}).get("users") or [])
                    f_author = f_users[0] if f_users else {}
                    f_username = f_author.get("username") or "unknown"
                    f_metrics = f_data.get("public_metrics") or {}
                    self.last_focal_tweet_meta = {
                        "id": str(f_data.get("id") or tweet_id),
                        "author": f"@{f_username}",
                        "author_name": f_author.get("name"),
                        "text": f_data.get("text") or "",
                        "published_at": f_data.get("created_at"),
                        "like_count": f_metrics.get("like_count", 0),
                        "reply_count": f_metrics.get("reply_count", 0),
                        "retweet_count": f_metrics.get("retweet_count", 0),
                    }
                    if f_data.get("text"):
                        target.title_or_name = f"X @{f_username}: {f_data['text'][:80]}"

            # Step 2: Fetch replies via recent search
            while total_yielded < limit:
                params: Dict[str, Any] = {
                    "query": f"conversation_id:{tweet_id}",
                    "max_results": min(100, max(10, limit - total_yielded)),
                    "tweet.fields": "id,text,author_id,created_at,conversation_id,in_reply_to_user_id,public_metrics",
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

                if resp is None:
                    raise ScraperError("x upstream error", "Failed to connect to X API v2", status_code=502)
                if resp.status_code in (401, 403):
                    raise ScraperError("x session rejected", f"X API v2 rejected credentials (HTTP {resp.status_code})", status_code=401)
                if resp.status_code == 429:
                    raise ScraperError("rate limited", "X API v2 rate limit exceeded", status_code=429)
                if resp.status_code != 200:
                    raise ScraperError("x api error", f"X API v2 returned HTTP {resp.status_code}", status_code=502)

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
                    pub_dt = _parse_x_created_at(tw.get("created_at"))
                    metrics = tw.get("public_metrics") or {}

                    yield NormalizedComment(
                        platform="x",
                        external_comment_id=f"x_{reply_id}",
                        parent_external_id=str(tweet_id),
                        target_external_id=str(tweet_id),
                        source_name=target.title_or_name or (f"X Post @{username}" if username else f"X Post {tweet_id}"),
                        post_url=f"https://x.com/i/status/{tweet_id}",
                        comment_url=f"https://x.com/{username}/status/{reply_id}" if username else f"https://x.com/i/status/{reply_id}",
                        author_external_id=author_id,
                        author_username=username,
                        author_display_name=display_name,
                        content_text=text,
                        published_at=pub_dt,
                        child_comment_count=int(metrics.get("reply_count") or 0),
                    )
                    total_yielded += 1

                meta = payload.get("meta") or {}
                next_token = meta.get("next_token")
                if not next_token:
                    break

    def _unwrap_tweet_result(self, tweet_res: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Unwrap TweetWithVisibilityResults or check for TweetTombstone / TweetUnavailable."""
        if not isinstance(tweet_res, dict):
            return None
        typename = tweet_res.get("__typename")
        if typename == "TweetWithVisibilityResults" and "tweet" in tweet_res:
            return tweet_res.get("tweet")
        if typename in ("TweetTombstone", "TweetUnavailable"):
            return tweet_res
        return tweet_res

    def _extract_focal_meta(self, tweet_res: Dict[str, Any], focal_tweet_id: str) -> Optional[Dict[str, Any]]:
        """Extract focal tweet metadata (text, author, likes, replies, timestamp) from GraphQL result."""
        unwrapped = self._unwrap_tweet_result(tweet_res)
        if not unwrapped or unwrapped.get("__typename") in ("TweetTombstone", "TweetUnavailable"):
            return None

        legacy = unwrapped.get("legacy") or {}
        rest_id = str(legacy.get("id_str") or unwrapped.get("rest_id") or "")
        if rest_id != str(focal_tweet_id):
            return None

        user_result = ((unwrapped.get("core") or {}).get("user_results") or {}).get("result") or {}
        user_legacy = user_result.get("legacy") or {}
        user_core = user_result.get("core") or {}
        username = user_legacy.get("screen_name") or user_core.get("screen_name") or "unknown"
        display_name = user_legacy.get("name") or user_core.get("name") or username

        # Support NoteTweet (long-form tweet text)
        note_text = (
            (((unwrapped.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}).get("text")
        )
        full_text = note_text or legacy.get("full_text") or ""
        pub_dt = _parse_x_created_at(legacy.get("created_at"))

        return {
            "id": rest_id,
            "author": f"@{username}",
            "author_name": display_name,
            "author_id": str(user_result.get("rest_id") or username),
            "text": full_text,
            "published_at": pub_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "like_count": int(legacy.get("favorite_count") or 0),
            "reply_count": int(legacy.get("reply_count") or 0),
            "retweet_count": int(legacy.get("retweet_count") or 0),
        }

    def _extract_tweet_result(
        self,
        tweet_res: Dict[str, Any],
        focal_tweet_id: str,
        source_title: Optional[str] = None,
    ) -> Optional[NormalizedComment]:
        """Parse a single GraphQL tweet_results object into a NormalizedComment."""
        unwrapped = self._unwrap_tweet_result(tweet_res)
        if not unwrapped or unwrapped.get("__typename") in ("TweetTombstone", "TweetUnavailable"):
            return None

        legacy = unwrapped.get("legacy") or {}
        reply_id = str(legacy.get("id_str") or unwrapped.get("rest_id") or "")
        if not reply_id or reply_id == str(focal_tweet_id):
            return None

        user_result = ((unwrapped.get("core") or {}).get("user_results") or {}).get("result") or {}
        user_legacy = user_result.get("legacy") or {}
        user_core = user_result.get("core") or {}
        username = user_legacy.get("screen_name") or user_core.get("screen_name")
        display_name = user_legacy.get("name") or user_core.get("name")

        note_text = (
            (((unwrapped.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}).get("text")
        )
        text = note_text or legacy.get("full_text") or ""
        if not text:
            return None

        # Strip leading @mention chains if the reply still has actual body text, or keep full text
        pub_dt = _parse_x_created_at(legacy.get("created_at"))
        reply_count = int(legacy.get("reply_count") or 0)

        return NormalizedComment(
            platform="x",
            external_comment_id=f"x_{reply_id}",
            parent_external_id=str(legacy.get("in_reply_to_status_id_str") or focal_tweet_id),
            target_external_id=str(focal_tweet_id),
            source_name=source_title or (f"X Post @{username}" if username else f"X Post {focal_tweet_id}"),
            post_url=f"https://x.com/i/status/{focal_tweet_id}",
            comment_url=f"https://x.com/{username}/status/{reply_id}" if username else f"https://x.com/i/status/{reply_id}",
            author_external_id=str(user_result.get("rest_id") or username or "unknown"),
            author_username=username,
            author_display_name=display_name,
            content_text=text,
            published_at=pub_dt,
            child_comment_count=reply_count,
        )

    def _parse_tweet_detail_payload(
        self,
        data: Dict[str, Any],
        focal_tweet_id: str,
        target: MonitoredTarget,
    ) -> Tuple[bool, List[NormalizedComment], Optional[str]]:
        """Parse a TweetDetail GraphQL JSON payload.

        Returns:
            Tuple of (focal_tweet_found, list_of_replies, next_bottom_cursor)
        """
        errors = data.get("errors") or []
        threaded = (data.get("data") or {}).get("threaded_conversation_with_injections_v2") or {}
        instructions = threaded.get("instructions") or []

        # Also check tweetResult fallback structure
        single_res = ((data.get("data") or {}).get("tweetResult") or {}).get("result")
        focal_found = False
        focal_unavailable = False

        if single_res:
            if single_res.get("__typename") in ("TweetTombstone", "TweetUnavailable"):
                focal_unavailable = True
            else:
                meta = self._extract_focal_meta(single_res, focal_tweet_id)
                if meta:
                    self.last_focal_tweet_meta = meta
                    focal_found = True

        replies: List[NormalizedComment] = []
        next_cursor: Optional[str] = None
        seen_ids = set()

        for inst in instructions:
            entries = inst.get("entries") or []
            if inst.get("entry"):
                entries = [inst["entry"]]
            for entry in entries:
                entry_id = str(entry.get("entryId") or "")
                content = entry.get("content") or {}

                if entry_id.startswith("cursor-bottom-") or (
                    content.get("cursorType") in ("Bottom", "ShowMoreThreads", "ShowMoreThreadsPrompt")
                ):
                    val = (content.get("itemContent") or {}).get("value") or content.get("value")
                    if val:
                        next_cursor = val

                # Direct itemContent (could be focal tweet or single tweet)
                item_content = content.get("itemContent") or {}
                tweet_res = (item_content.get("tweet_results") or {}).get("result")
                if tweet_res:
                    if entry_id == f"tweet-{focal_tweet_id}" or str(tweet_res.get("rest_id")) == str(focal_tweet_id):
                        if tweet_res.get("__typename") in ("TweetTombstone", "TweetUnavailable"):
                            focal_unavailable = True
                        else:
                            meta = self._extract_focal_meta(tweet_res, focal_tweet_id)
                            if meta:
                                self.last_focal_tweet_meta = meta
                                focal_found = True
                                if meta.get("text"):
                                    target.title_or_name = f"X {meta['author']}: {meta['text'][:80]}"
                    else:
                        norm = self._extract_tweet_result(tweet_res, focal_tweet_id, source_title=target.title_or_name)
                        if norm and norm.external_comment_id not in seen_ids:
                            seen_ids.add(norm.external_comment_id)
                            replies.append(norm)

                # Threaded module items (conversationthread-*)
                for mod_item in content.get("items") or []:
                    mod_ic = (mod_item.get("item") or {}).get("itemContent") or {}
                    if mod_ic.get("itemType") == "TimelineTimelineCursor" or mod_ic.get("__typename") == "TimelineTimelineCursor":
                        continue
                    mod_tr = (mod_ic.get("tweet_results") or {}).get("result")
                    if mod_tr:
                        if str(mod_tr.get("rest_id")) == str(focal_tweet_id):
                            meta = self._extract_focal_meta(mod_tr, focal_tweet_id)
                            if meta:
                                self.last_focal_tweet_meta = meta
                                focal_found = True
                                if meta.get("text"):
                                    target.title_or_name = f"X {meta['author']}: {meta['text'][:80]}"
                        else:
                            norm = self._extract_tweet_result(mod_tr, focal_tweet_id, source_title=target.title_or_name)
                            if norm and norm.external_comment_id not in seen_ids:
                                seen_ids.add(norm.external_comment_id)
                                replies.append(norm)

        if focal_unavailable and not focal_found:
            raise ScraperError("tweet not found", f"Tweet {focal_tweet_id} is unavailable or deleted", status_code=404)

        # Check explicit GraphQL errors if focal tweet wasn't found
        if not focal_found and not replies:
            for err in errors:
                code = err.get("code")
                msg = (err.get("message") or "").lower()
                if code in (32, 64, 135, 215, 326, 353) or "authenticate" in msg or "csrf" in msg or "token" in msg:
                    raise ScraperError("x session rejected", err.get("message") or "X session rejected", status_code=401)
                if code == 88 or "rate limit" in msg:
                    raise ScraperError("rate limited", err.get("message") or "X rate limit exceeded", status_code=429)
                if code in (34, 144, 179, 421) or "no status found" in msg or "not found" in msg:
                    raise ScraperError("tweet not found", f"Tweet {focal_tweet_id} not found", status_code=404)

        return focal_found, replies, next_cursor

    async def _refresh_ct0_cookie(
        self,
        cookies: Dict[str, str],
        user_agent: str,
        proxy_url: Optional[str] = None,
    ) -> Dict[str, str]:
        """Ensure ct0 CSRF cookie is present and fresh by making a lightweight authenticated request to X."""
        updated = dict(cookies)
        headers = {
            "authorization": X_PUBLIC_WEB_BEARER,
            "user-agent": user_agent,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
            "Referer": "https://x.com/home",
        }
        if updated.get("ct0"):
            headers["x-csrf-token"] = updated["ct0"]

        try:
            async with httpx.AsyncClient(
                cookies=updated,
                proxy=proxy_url,
                headers=headers,
                timeout=15.0,
                follow_redirects=True,
            ) as client:
                # Hit x.com/manifest.json or settings endpoint to let X issue/rotate ct0 cookie
                resp = await client.get("https://x.com/i/api/1.1/account/settings.json")
                for k, v in client.cookies.items():
                    updated[k] = v
                for k, v in resp.cookies.items():
                    updated[k] = v
                if resp.status_code in (401, 403):
                    body_text = resp.text.lower()
                    # Code 32 = Could not authenticate you (invalid/expired auth_token)
                    if '"code":32' in body_text or '"code": 32' in body_text or "could not authenticate" in body_text:
                        raise ScraperError("x session rejected", "X auth_token cookie is invalid or expired (code 32)", status_code=401)
        except ScraperError:
            raise
        except Exception as e:
            logger.debug("ct0 pre-flight refresh check skipped", error=str(e))

        return updated

    async def _fetch_via_twikit(
        self,
        tweet_id: str,
        target: MonitoredTarget,
        cookies: Dict[str, str],
        user_agent: str,
        proxy_url: Optional[str],
        cursor: Optional[str],
        limit: int,
    ) -> Tuple[bool, List[NormalizedComment], Optional[Dict[str, str]]]:
        """Attempt fetching tweet + replies via twikit Client (handles ct0 & transaction headers)."""
        _patch_twikit_transaction()
        from twikit import Client as TwikitClient
        from twikit.errors import Unauthorized, Forbidden, NotFound, TooManyRequests, AccountLocked, AccountSuspended

        tw_client = TwikitClient(language="en-US", proxy=proxy_url, user_agent=user_agent)
        tw_client.set_cookies(cookies)

        collected: List[NormalizedComment] = []
        focal_found = False

        try:
            current_cursor = cursor
            pages = 0
            max_pages = max(1, (limit // 20) + 1)

            while len(collected) < limit and pages < max_pages:
                pages += 1
                res_tuple = await tw_client.gql.tweet_detail(tweet_id, cursor=current_cursor)
                data = res_tuple[0] if isinstance(res_tuple, tuple) else res_tuple
                if not isinstance(data, dict):
                    break

                page_focal, page_replies, next_cursor = self._parse_tweet_detail_payload(data, tweet_id, target)
                if page_focal:
                    focal_found = True
                for r in page_replies:
                    if len(collected) < limit:
                        collected.append(r)

                if not next_cursor or next_cursor == current_cursor:
                    break
                current_cursor = next_cursor

            new_cookies = tw_client.get_cookies()
            return focal_found, collected, new_cookies

        except (Unauthorized, Forbidden, AccountLocked, AccountSuspended) as auth_err:
            raise ScraperError("x session rejected", f"X session rejected: {auth_err}", status_code=401)
        except TooManyRequests as rl_err:
            raise ScraperError("rate limited", f"X rate limited: {rl_err}", status_code=429)
        except NotFound as nf_err:
            raise ScraperError("tweet not found", f"Tweet {tweet_id} not found: {nf_err}", status_code=404)

    async def _fetch_via_playwright(
        self,
        tweet_id: str,
        target: MonitoredTarget,
        cookies: Dict[str, str],
        user_agent: str,
        proxy_url: Optional[str],
        limit: int,
    ) -> Tuple[bool, List[NormalizedComment]]:
        """Fallback: Use headless Chromium with injected session cookies to intercept X's TweetDetail GraphQL JSON."""
        from playwright.async_api import async_playwright

        intercepted_payloads: List[Dict[str, Any]] = []
        auth_failed = False
        rate_limited = False

        pw_cookies = []
        for name, val in cookies.items():
            if not val:
                continue
            for domain in (".x.com", ".twitter.com"):
                pw_cookies.append(
                    {
                        "name": str(name),
                        "value": str(val),
                        "domain": domain,
                        "path": "/",
                        "secure": True,
                        "httpOnly": name == "auth_token",
                        "sameSite": "Lax",
                    }
                )

        proxy_cfg = {"server": proxy_url} if proxy_url else None

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, proxy=proxy_cfg)
            try:
                context = await browser.new_context(user_agent=user_agent)
                await context.add_cookies(pw_cookies)
                page = await context.new_page()

                async def handle_response(response):
                    nonlocal auth_failed, rate_limited
                    if "TweetDetail" in response.url or "TweetResultByRestId" in response.url:
                        if response.status in (401, 403):
                            auth_failed = True
                        elif response.status == 429:
                            rate_limited = True
                        elif response.status == 200:
                            try:
                                body_json = await response.json()
                                intercepted_payloads.append(body_json)
                            except Exception:
                                pass

                page.on("response", handle_response)
                await page.goto(f"https://x.com/i/status/{tweet_id}", wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(3500)

                if "/login" in page.url or "/i/flow/login" in page.url:
                    raise ScraperError("x session rejected", "X redirected to login flow (session cookies expired)", status_code=401)

                page_text = await page.content()
                if "Hmm...this page doesn’t exist" in page_text or "Hmm...this page doesn't exist" in page_text:
                    raise ScraperError("tweet not found", f"Tweet {tweet_id} does not exist", status_code=404)
            finally:
                await browser.close()

        if rate_limited and not intercepted_payloads:
            raise ScraperError("rate limited", "X rate limit exceeded (HTTP 429)", status_code=429)
        if auth_failed and not intercepted_payloads:
            raise ScraperError("x session rejected", "X session rejected (HTTP 401/403)", status_code=401)

        focal_found = False
        collected: List[NormalizedComment] = []
        seen_ids = set()

        for payload in intercepted_payloads:
            p_focal, p_replies, _ = self._parse_tweet_detail_payload(payload, tweet_id, target)
            if p_focal:
                focal_found = True
            for r in p_replies:
                if r.external_comment_id not in seen_ids and len(collected) < limit:
                    seen_ids.add(r.external_comment_id)
                    collected.append(r)

        return focal_found, collected

    async def fetch_comments(
        self,
        target: MonitoredTarget,
        cursor: Optional[str] = None,
        limit: int = 5000,
    ) -> AsyncIterator[NormalizedComment]:
        """Paginate replies to an X post using Burner Session Cookies (twikit / GraphQL / Playwright) or API v2."""
        tweet_id = str(target.external_id).strip()
        self.last_focal_tweet_meta = None
        total_yielded = 0

        # Engine 1: Burner Session Cookies (Preferred when registered in scraper_sessions)
        session_tuple = None
        if self.pool_manager:
            session_tuple = await self.pool_manager.acquire_session("x")

        if session_tuple:
            session_obj, raw_cookies, proxy_url = session_tuple
            cookies: Dict[str, str] = {}
            if isinstance(raw_cookies, dict):
                cookies = {str(k): str(v) for k, v in raw_cookies.items() if v is not None}
            elif isinstance(raw_cookies, str):
                for part in raw_cookies.split(";"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        cookies[k.strip()] = v.strip()
                if not cookies and raw_cookies.strip():
                    cookies["auth_token"] = raw_cookies.strip()

            user_agent = (
                session_obj.user_agent
                or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )

            try:
                # 1a. Refresh/obtain ct0 cookie if missing or stale
                cookies = await self._refresh_ct0_cookie(cookies, user_agent=user_agent, proxy_url=proxy_url)
                if self.pool_manager and cookies.get("ct0"):
                    await self.pool_manager.update_session_cookies(session_obj.id, cookies)

                # 1b. Try twikit Client first
                focal_found = False
                replies: List[NormalizedComment] = []
                try:
                    focal_found, replies, rotated_cookies = await self._fetch_via_twikit(
                        tweet_id=tweet_id,
                        target=target,
                        cookies=cookies,
                        user_agent=user_agent,
                        proxy_url=proxy_url,
                        cursor=cursor,
                        limit=limit,
                    )
                    if rotated_cookies and self.pool_manager:
                        cookies.update({str(k): str(v) for k, v in rotated_cookies.items() if v})
                        await self.pool_manager.update_session_cookies(session_obj.id, cookies)
                except ScraperError:
                    raise
                except Exception as tw_err:
                    logger.warning("twikit fetch failed; trying direct GraphQL with full FEATURES", error=str(tw_err))

                # 1c. Direct GraphQL with full X_GRAPHQL_FEATURES & candidate query IDs
                if not focal_found and not replies:
                    headers = {
                        "authorization": X_PUBLIC_WEB_BEARER,
                        "x-csrf-token": cookies.get("ct0", ""),
                        "x-twitter-active-user": "yes",
                        "x-twitter-auth-type": "OAuth2Session",
                        "x-twitter-client-language": "en",
                        "user-agent": user_agent,
                        "content-type": "application/json",
                        "Referer": f"https://x.com/i/status/{tweet_id}",
                    }
                    candidate_qids = [self.query_id] + [q for q in X_TWEET_DETAIL_QUERY_IDS if q != self.query_id]
                    current_cursor = cursor
                    last_status = None

                    async with httpx.AsyncClient(cookies=cookies, proxy=proxy_url, headers=headers, timeout=25.0) as client:
                        for qid in candidate_qids:
                            url = f"https://x.com/i/api/graphql/{qid}/TweetDetail"
                            page_loop = 0
                            while len(replies) < limit and page_loop < max(1, (limit // 20) + 1):
                                page_loop += 1
                                variables: Dict[str, Any] = {
                                    "focalTweetId": tweet_id,
                                    "with_rux_injections": False,
                                    "includePromotedContent": True,
                                    "withCommunity": True,
                                    "withQuickPromoteEligibilityTweetFields": True,
                                    "withBirdwatchNotes": True,
                                    "withVoice": True,
                                    "withV2Timeline": True,
                                }
                                if current_cursor:
                                    variables["cursor"] = current_cursor

                                params = {
                                    "variables": json.dumps(variables),
                                    "features": json.dumps(X_GRAPHQL_FEATURES),
                                    "fieldToggles": json.dumps({"withAuxiliaryUserLabels": False}),
                                }
                                resp = await client.get(url, params=params)
                                last_status = resp.status_code

                                # Update ct0 if X rotated it
                                if "ct0" in resp.cookies:
                                    cookies["ct0"] = resp.cookies["ct0"]
                                    client.headers["x-csrf-token"] = cookies["ct0"]

                                if resp.status_code == 429:
                                    raise ScraperError("rate limited", "X rate limit exceeded (HTTP 429)", status_code=429)
                                if resp.status_code in (401, 403):
                                    raise ScraperError(
                                        "x session rejected",
                                        f"X GraphQL rejected session (HTTP {resp.status_code})",
                                        status_code=401,
                                    )
                                if resp.status_code in (400, 404):
                                    # Stale query_id or missing transaction header — try next query_id
                                    break
                                if resp.status_code != 200:
                                    break

                                data = resp.json()
                                p_focal, p_replies, next_cursor = self._parse_tweet_detail_payload(data, tweet_id, target)
                                if p_focal:
                                    focal_found = True
                                replies.extend(p_replies)
                                if not next_cursor or next_cursor == current_cursor:
                                    break
                                current_cursor = next_cursor

                            if focal_found or replies:
                                break

                    # 1d. If direct HTTP got blocked (e.g. 404/400 due to missing browser X-Client-Transaction-Id), use Playwright fallback
                    if not focal_found and not replies and last_status in (400, 404, 500, None):
                        try:
                            focal_found, replies = await self._fetch_via_playwright(
                                tweet_id=tweet_id,
                                target=target,
                                cookies=cookies,
                                user_agent=user_agent,
                                proxy_url=proxy_url,
                                limit=limit,
                            )
                        except ScraperError:
                            raise
                        except Exception as pw_err:
                            logger.warning("Playwright X fallback failed", error=str(pw_err))

                if not focal_found and not replies:
                    raise ScraperError("tweet not found", f"Tweet {tweet_id} not found or unavailable", status_code=404)

                for comment in replies[:limit]:
                    yield comment
                    total_yielded += 1
                return

            except ScraperError as s_err:
                if s_err.error_code == "x session rejected" and self.pool_manager:
                    await self.pool_manager.report_session_error(session_obj.id)
                elif s_err.error_code == "rate limited" and self.pool_manager:
                    await self.pool_manager.report_rate_limit(session_obj.id, cooldown_minutes=15)
                raise

        # Engine 2: Official X API v2 Bearer Token (if no active burner session or as configured)
        if self.bearer_token:
            async for comment in self._fetch_api_v2_replies(
                tweet_id=tweet_id,
                target=target,
                cursor=cursor,
                limit=limit,
            ):
                yield comment
                total_yielded += 1
            return

        raise ScraperError("no x session configured", "No active X burner session or X_BEARER_TOKEN configured", status_code=409)

    def _parse_user_by_screen_name_payload(self, data: Dict[str, Any], clean_username: str) -> Tuple[Optional[str], Dict[str, Any]]:
        """Safely extract user rest_id and profile metadata from UserByScreenName JSON without KeyError."""
        user_res = ((data.get("data") or {}).get("user") or {}).get("result") or {}
        if not user_res:
            return None, {}
        if user_res.get("__typename") == "UserUnavailable":
            raise ScraperError("profile not found", f"X user @{clean_username} is unavailable or suspended", status_code=404)

        legacy = user_res.get("legacy") or {}
        core = user_res.get("core") or {}
        rest_id = str(user_res.get("rest_id") or legacy.get("id_str") or "")
        if not rest_id:
            return None, {}

        screen_name = legacy.get("screen_name") or core.get("screen_name") or clean_username
        full_name = legacy.get("name") or core.get("name") or screen_name
        bio = legacy.get("description") or ((user_res.get("profile_bio") or {}).get("description"))

        meta = {
            "platform": "x",
            "user_id": rest_id,
            "username": f"@{screen_name}",
            "url": f"https://x.com/{screen_name}",
            "full_name": full_name,
            "bio": bio,
            "followers": int(legacy.get("followers_count") or 0),
            "following": int(legacy.get("friends_count") or 0),
            "total_posts": int(legacy.get("statuses_count") or 0),
            "is_verified": bool(user_res.get("is_blue_verified") or legacy.get("verified")),
        }
        return rest_id, meta

    def _parse_user_tweets_payload(
        self,
        data: Dict[str, Any],
        clean_username: str,
        count: int,
    ) -> List[Dict[str, Any]]:
        """Safely extract user timeline tweets from UserTweets GraphQL JSON."""
        user_res = ((data.get("data") or {}).get("user") or {}).get("result") or {}
        tl_v2 = (user_res.get("timeline_v2") or user_res.get("timeline") or {}).get("timeline") or {}
        instructions = tl_v2.get("instructions") or []

        posts: List[Dict[str, Any]] = []
        seen_ids = set()

        for inst in instructions:
            entries = inst.get("entries") or []
            if inst.get("entry"):
                entries = [inst["entry"]]
            for entry in entries:
                entry_id = str(entry.get("entryId") or "")
                content = entry.get("content") or {}

                candidate_results = []
                ic = content.get("itemContent") or {}
                if (ic.get("tweet_results") or {}).get("result"):
                    candidate_results.append(ic["tweet_results"]["result"])

                for mod_item in content.get("items") or []:
                    mod_ic = (mod_item.get("item") or {}).get("itemContent") or {}
                    if (mod_ic.get("tweet_results") or {}).get("result"):
                        candidate_results.append(mod_ic["tweet_results"]["result"])

                for raw_tr in candidate_results:
                    tr = self._unwrap_tweet_result(raw_tr)
                    if not tr or tr.get("__typename") in ("TweetTombstone", "TweetUnavailable"):
                        continue
                    leg = tr.get("legacy") or {}
                    # If this entry is a pure retweet and we haven't reached the end, skip or unwrap
                    if leg.get("retweeted_status_result") and entry_id.startswith("tweet-"):
                        continue
                    tw_id = str(leg.get("id_str") or tr.get("rest_id") or "")
                    if not tw_id or tw_id in seen_ids:
                        continue
                    seen_ids.add(tw_id)

                    note_text = (
                        (((tr.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}).get("text")
                    )
                    full_text = note_text or leg.get("full_text") or ""
                    pub_dt = _parse_x_created_at(leg.get("created_at"))

                    posts.append(
                        {
                            "post_index": len(posts) + 1,
                            "shortcode": tw_id,
                            "tweet_id": tw_id,
                            "url": f"https://x.com/{clean_username}/status/{tw_id}",
                            "caption": full_text[:200],
                            "comment_count": int(leg.get("reply_count") or 0),
                            "like_count": int(leg.get("favorite_count") or 0),
                            "retweet_count": int(leg.get("retweet_count") or 0),
                            "published_at": pub_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    )
                    if len(posts) >= count:
                        return posts
        return posts

    async def fetch_profile_info_and_posts(
        self,
        username: str,
        count: int = 5,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Fetch an X user's profile metadata and their latest N posts (tweets).

        Returns:
            Tuple of (profile_meta_dict, list_of_recent_posts)
        """
        clean_username = username.strip().lstrip("@").split("/")[0].split("?")[0]
        profile_meta: Dict[str, Any] = {
            "platform": "x",
            "username": f"@{clean_username}",
            "url": f"https://x.com/{clean_username}",
            "full_name": clean_username,
            "bio": None,
            "followers": None,
            "following": None,
            "total_posts": None,
            "is_verified": False,
        }
        posts: List[Dict[str, Any]] = []

        # Engine 1: Burner Session Cookies via twikit + Direct GraphQL + Search fallback + Playwright
        session_tuple = None
        if self.pool_manager:
            session_tuple = await self.pool_manager.acquire_session("x")

        if session_tuple:
            session_obj, raw_cookies, proxy_url = session_tuple
            cookies: Dict[str, str] = {}
            if isinstance(raw_cookies, dict):
                cookies = {str(k): str(v) for k, v in raw_cookies.items() if v is not None}
            elif isinstance(raw_cookies, str):
                for part in raw_cookies.split(";"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        cookies[k.strip()] = v.strip()
                if not cookies and raw_cookies.strip():
                    cookies["auth_token"] = raw_cookies.strip()

            user_agent = (
                session_obj.user_agent
                or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )

            try:
                cookies = await self._refresh_ct0_cookie(cookies, user_agent=user_agent, proxy_url=proxy_url)
                if self.pool_manager and cookies.get("ct0"):
                    await self.pool_manager.update_session_cookies(session_obj.id, cookies)

                user_id: Optional[str] = None

                # 1a. Try twikit Client (using both high-level get_user_by_screen_name / get_user_tweets and raw gql fallback)
                try:
                    _patch_twikit_transaction()
                    from twikit import Client as TwikitClient
                    from twikit.errors import (
                        Unauthorized,
                        Forbidden,
                        NotFound,
                        UserNotFound,
                        UserUnavailable,
                        TooManyRequests,
                        AccountLocked,
                        AccountSuspended,
                    )

                    tw_client = TwikitClient(language="en-US", proxy=proxy_url, user_agent=user_agent)
                    tw_client.set_cookies(cookies)

                    # First try raw gql.user_by_screen_name so X schema changes never raise KeyError inside twikit.User.__init__
                    try:
                        u_raw_resp, _ = await tw_client.gql.user_by_screen_name(clean_username)
                        if isinstance(u_raw_resp, dict):
                            uid, parsed_meta = self._parse_user_by_screen_name_payload(u_raw_resp, clean_username)
                            if uid:
                                user_id = uid
                                profile_meta.update(parsed_meta)
                    except (Unauthorized, Forbidden, AccountLocked, AccountSuspended, TooManyRequests, NotFound, UserNotFound, UserUnavailable):
                        raise
                    except Exception as raw_u_err:
                        logger.debug("twikit raw gql.user_by_screen_name fallback", error=str(raw_u_err))

                    # Also try high-level tw_client.get_user_by_screen_name if user_id is still None
                    if not user_id:
                        user_obj = await tw_client.get_user_by_screen_name(clean_username)
                        if user_obj:
                            user_id = str(user_obj.id)
                            profile_meta.update(
                                {
                                    "user_id": user_id,
                                    "username": f"@{user_obj.screen_name or clean_username}",
                                    "url": f"https://x.com/{user_obj.screen_name or clean_username}",
                                    "full_name": user_obj.name or clean_username,
                                    "bio": user_obj.description or None,
                                    "followers": int(getattr(user_obj, "followers_count", 0) or 0),
                                    "following": int(getattr(user_obj, "following_count", 0) or 0),
                                    "total_posts": int(getattr(user_obj, "statuses_count", 0) or 0),
                                    "is_verified": bool(getattr(user_obj, "is_blue_verified", False) or getattr(user_obj, "verified", False)),
                                }
                            )

                    if user_id:
                        # Try raw gql.user_tweets first (immune to twikit index errors on empty/pinned timelines)
                        try:
                            tw_raw_resp, _ = await tw_client.gql.user_tweets(user_id, max(20, count * 3), None)
                            if isinstance(tw_raw_resp, dict):
                                posts = self._parse_user_tweets_payload(tw_raw_resp, clean_username, count=count)
                        except Exception as raw_t_err:
                            logger.debug("twikit raw gql.user_tweets fallback", error=str(raw_t_err))

                        # Also try high-level tw_client.get_user_tweets(user_id, 'Tweets') if posts still empty
                        if not posts:
                            user_tweets = await tw_client.get_user_tweets(user_id, "Tweets", count=max(20, count * 2))
                            for tw in user_tweets or []:
                                tw_text = getattr(tw, "full_text", None) or getattr(tw, "text", "") or ""
                                if tw_text.startswith("RT @") and len(posts) >= count:
                                    continue
                                tw_id = str(getattr(tw, "id", "") or "")
                                if not tw_id:
                                    continue
                                pub_dt = _parse_x_created_at(getattr(tw, "created_at", None))
                                posts.append(
                                    {
                                        "post_index": len(posts) + 1,
                                        "shortcode": tw_id,
                                        "tweet_id": tw_id,
                                        "url": f"https://x.com/{clean_username}/status/{tw_id}",
                                        "caption": tw_text[:200],
                                        "comment_count": int(getattr(tw, "reply_count", 0) or 0),
                                        "like_count": int(getattr(tw, "favorite_count", 0) or 0),
                                        "retweet_count": int(getattr(tw, "retweet_count", 0) or 0),
                                        "published_at": pub_dt.strftime("%Y-%m-%d %H:%M:%S"),
                                    }
                                )
                                if len(posts) >= count:
                                    break

                    # If UserTweets endpoint failed or returned 0, try twikit search_tweet("from:<username>")
                    if not posts:
                        try:
                            search_res = await tw_client.search_tweet(f"from:{clean_username} -filter:retweets", "Latest", count=max(10, count * 2))
                            for tw in search_res or []:
                                tw_id = str(getattr(tw, "id", "") or "")
                                if not tw_id:
                                    continue
                                if not user_id and getattr(tw, "user", None):
                                    u = tw.user
                                    user_id = str(getattr(u, "id", "") or "")
                                    profile_meta.update(
                                        {
                                            "username": f"@{getattr(u, 'screen_name', None) or clean_username}",
                                            "full_name": getattr(u, "name", None) or clean_username,
                                            "bio": getattr(u, "description", None),
                                            "followers": int(getattr(u, "followers_count", 0) or 0),
                                            "following": int(getattr(u, "following_count", 0) or 0),
                                            "total_posts": int(getattr(u, "statuses_count", 0) or 0),
                                        }
                                    )
                                tw_text = getattr(tw, "full_text", None) or getattr(tw, "text", "") or ""
                                pub_dt = _parse_x_created_at(getattr(tw, "created_at", None))
                                posts.append(
                                    {
                                        "post_index": len(posts) + 1,
                                        "shortcode": tw_id,
                                        "tweet_id": tw_id,
                                        "url": f"https://x.com/{clean_username}/status/{tw_id}",
                                        "caption": tw_text[:200],
                                        "comment_count": int(getattr(tw, "reply_count", 0) or 0),
                                        "like_count": int(getattr(tw, "favorite_count", 0) or 0),
                                        "retweet_count": int(getattr(tw, "retweet_count", 0) or 0),
                                        "published_at": pub_dt.strftime("%Y-%m-%d %H:%M:%S"),
                                    }
                                )
                                if len(posts) >= count:
                                    break
                        except Exception as search_err:
                            logger.debug("twikit search_tweet fallback failed", error=str(search_err))

                    rotated = tw_client.get_cookies()
                    if rotated and self.pool_manager:
                        cookies.update({str(k): str(v) for k, v in rotated.items() if v})
                        await self.pool_manager.update_session_cookies(session_obj.id, cookies)

                except (Unauthorized, Forbidden, AccountLocked, AccountSuspended) as auth_err:
                    raise ScraperError("x session rejected", f"X session rejected: {auth_err}", status_code=401)
                except TooManyRequests as rl_err:
                    raise ScraperError("rate limited", f"X rate limited: {rl_err}", status_code=429)
                except (NotFound, UserNotFound, UserUnavailable) as nf_err:
                    raise ScraperError("profile not found", f"X user @{clean_username} not found: {nf_err}", status_code=404)
                except ScraperError:
                    raise
                except Exception as tw_prof_err:
                    logger.warning("twikit profile fetch failed; trying direct HTTP GraphQL", username=clean_username, error=str(tw_prof_err))

                # 1b. Direct HTTP GraphQL fallback (same httpx pattern that succeeded for TweetDetail!)
                if not posts:
                    headers = {
                        "authorization": X_PUBLIC_WEB_BEARER,
                        "x-csrf-token": cookies.get("ct0", ""),
                        "x-twitter-active-user": "yes",
                        "x-twitter-auth-type": "OAuth2Session",
                        "x-twitter-client-language": "en",
                        "user-agent": user_agent,
                        "content-type": "application/json",
                        "Referer": f"https://x.com/{clean_username}",
                    }
                    async with httpx.AsyncClient(cookies=cookies, proxy=proxy_url, headers=headers, timeout=25.0) as client:
                        # Step 1: Resolve user_id & profile metadata via UserByScreenName or 1.1 users/show.json
                        if not user_id:
                            for u_qid in X_USER_BY_SCREEN_NAME_QUERY_IDS:
                                u_url = f"https://x.com/i/api/graphql/{u_qid}/UserByScreenName"
                                u_params = {
                                    "variables": json.dumps({"screen_name": clean_username, "withSafetyModeUserFields": True}),
                                    "features": json.dumps(X_USER_FEATURES),
                                    "fieldToggles": json.dumps({"withAuxiliaryUserLabels": False}),
                                }
                                u_resp = await client.get(u_url, params=u_params)
                                if u_resp.status_code == 429:
                                    raise ScraperError("rate limited", "X rate limit exceeded (HTTP 429)", status_code=429)
                                if u_resp.status_code in (401, 403):
                                    raise ScraperError("x session rejected", f"X rejected session (HTTP {u_resp.status_code})", status_code=401)
                                if u_resp.status_code == 200:
                                    uid, p_meta = self._parse_user_by_screen_name_payload(u_resp.json(), clean_username)
                                    if uid:
                                        user_id = uid
                                        profile_meta.update(p_meta)
                                        break

                        # Fallback to REST v1.1 users/show.json if GraphQL UserByScreenName query_id rotated
                        if not user_id:
                            v1_resp = await client.get(
                                "https://x.com/i/api/1.1/users/show.json",
                                params={"screen_name": clean_username},
                            )
                            if v1_resp.status_code == 200:
                                v1_u = v1_resp.json()
                                user_id = str(v1_u.get("id_str") or v1_u.get("id") or "")
                                if user_id:
                                    profile_meta.update(
                                        {
                                            "user_id": user_id,
                                            "username": f"@{v1_u.get('screen_name') or clean_username}",
                                            "url": f"https://x.com/{v1_u.get('screen_name') or clean_username}",
                                            "full_name": v1_u.get("name") or clean_username,
                                            "bio": v1_u.get("description"),
                                            "followers": int(v1_u.get("followers_count") or 0),
                                            "following": int(v1_u.get("friends_count") or 0),
                                            "total_posts": int(v1_u.get("statuses_count") or 0),
                                            "is_verified": bool(v1_u.get("verified")),
                                        }
                                    )

                        # Step 2: Fetch user's tweets via UserTweets GraphQL
                        if user_id:
                            for t_qid in X_USER_TWEETS_QUERY_IDS:
                                t_url = f"https://x.com/i/api/graphql/{t_qid}/UserTweets"
                                t_variables = {
                                    "userId": user_id,
                                    "count": max(20, count * 3),
                                    "includePromotedContent": True,
                                    "withQuickPromoteEligibilityTweetFields": True,
                                    "withVoice": True,
                                    "withV2Timeline": True,
                                }
                                t_params = {
                                    "variables": json.dumps(t_variables),
                                    "features": json.dumps(X_GRAPHQL_FEATURES),
                                    "fieldToggles": json.dumps({"withArticlePlainText": False}),
                                }
                                t_resp = await client.get(t_url, params=t_params)
                                if t_resp.status_code == 429:
                                    raise ScraperError("rate limited", "X rate limit exceeded (HTTP 429)", status_code=429)
                                if t_resp.status_code in (401, 403):
                                    raise ScraperError("x session rejected", f"X rejected session (HTTP {t_resp.status_code})", status_code=401)
                                if t_resp.status_code == 200:
                                    posts = self._parse_user_tweets_payload(t_resp.json(), clean_username, count=count)
                                    if posts:
                                        break

                # 1c. Playwright fallback for X profile timeline (UserByScreenName + UserTweets)
                if not posts:
                    try:
                        from playwright.async_api import async_playwright

                        user_payloads: List[Dict[str, Any]] = []
                        tweets_payloads: List[Dict[str, Any]] = []
                        pw_cookies = []
                        for name, val in cookies.items():
                            if not val:
                                continue
                            for domain in (".x.com", ".twitter.com"):
                                pw_cookies.append(
                                    {
                                        "name": str(name),
                                        "value": str(val),
                                        "domain": domain,
                                        "path": "/",
                                        "secure": True,
                                        "httpOnly": name == "auth_token",
                                        "sameSite": "Lax",
                                    }
                                )
                        proxy_cfg = {"server": proxy_url} if proxy_url else None
                        async with async_playwright() as p:
                            browser = await p.chromium.launch(headless=True, proxy=proxy_cfg)
                            try:
                                context = await browser.new_context(user_agent=user_agent)
                                await context.add_cookies(pw_cookies)
                                page = await context.new_page()

                                async def handle_pw_resp(response):
                                    if "UserByScreenName" in response.url and response.status == 200:
                                        try:
                                            user_payloads.append(await response.json())
                                        except Exception:
                                            pass
                                    elif ("UserTweets" in response.url or "UserTweetsAndReplies" in response.url) and response.status == 200:
                                        try:
                                            tweets_payloads.append(await response.json())
                                        except Exception:
                                            pass

                                page.on("response", handle_pw_resp)
                                await page.goto(f"https://x.com/{clean_username}", wait_until="domcontentloaded", timeout=25000)
                                await page.wait_for_timeout(3500)

                                if "/login" in page.url or "/i/flow/login" in page.url:
                                    raise ScraperError("x session rejected", "X redirected to login flow (session cookies expired)", status_code=401)
                            finally:
                                await browser.close()

                        for u_pay in user_payloads:
                            _, p_meta = self._parse_user_by_screen_name_payload(u_pay, clean_username)
                            if p_meta:
                                profile_meta.update(p_meta)

                        for t_pay in tweets_payloads:
                            parsed_p = self._parse_user_tweets_payload(t_pay, clean_username, count=count)
                            if parsed_p:
                                posts.extend(parsed_p)
                                if len(posts) >= count:
                                    break
                    except ScraperError:
                        raise
                    except Exception as pw_prof_err:
                        logger.warning("Playwright X profile fallback failed", username=clean_username, error=str(pw_prof_err))

                if posts or profile_meta.get("followers") is not None:
                    return profile_meta, posts[:count]

            except ScraperError as s_err:
                if s_err.error_code == "x session rejected" and self.pool_manager:
                    await self.pool_manager.report_session_error(session_obj.id)
                elif s_err.error_code == "rate limited" and self.pool_manager:
                    await self.pool_manager.report_rate_limit(session_obj.id, cooldown_minutes=15)
                raise

        # Engine 2: Official X API v2 Bearer Token
        if self.bearer_token:
            raw_token = urllib.parse.unquote(self.bearer_token or "").strip()
            if raw_token.lower().startswith("bearer "):
                raw_token = raw_token[7:].strip()
            headers = {"Authorization": f"Bearer {raw_token}", "User-Agent": "OperativesMonitor/2.0"}
            async with httpx.AsyncClient(headers=headers, timeout=20.0) as client:
                u_resp = await client.get(
                    f"https://api.x.com/2/users/by/username/{clean_username}",
                    params={"user.fields": "id,name,username,description,public_metrics,verified"},
                )
                if u_resp.status_code in (401, 403):
                    raise ScraperError("x session rejected", "X API v2 Bearer Token rejected", status_code=401)
                if u_resp.status_code == 429:
                    raise ScraperError("rate limited", "X API v2 rate limit exceeded", status_code=429)
                if u_resp.status_code == 200:
                    u_data = (u_resp.json() or {}).get("data")
                    if not u_data:
                        raise ScraperError("profile not found", f"X user @{clean_username} not found", status_code=404)
                    uid = u_data["id"]
                    p_metrics = u_data.get("public_metrics") or {}
                    profile_meta.update(
                        {
                            "username": f"@{u_data.get('username') or clean_username}",
                            "url": f"https://x.com/{u_data.get('username') or clean_username}",
                            "full_name": u_data.get("name") or clean_username,
                            "bio": u_data.get("description"),
                            "followers": p_metrics.get("followers_count", 0),
                            "following": p_metrics.get("following_count", 0),
                            "total_posts": p_metrics.get("tweet_count", 0),
                            "is_verified": bool(u_data.get("verified")),
                        }
                    )
                    t_resp = await client.get(
                        f"https://api.x.com/2/users/{uid}/tweets",
                        params={
                            "max_results": min(100, max(5, count)),
                            "tweet.fields": "id,text,created_at,public_metrics",
                            "exclude": "retweets,replies",
                        },
                    )
                    if t_resp.status_code == 200:
                        for tw in (t_resp.json() or {}).get("data") or []:
                            tw_id = str(tw.get("id") or "")
                            if not tw_id:
                                continue
                            tm = tw.get("public_metrics") or {}
                            pub_dt = _parse_x_created_at(tw.get("created_at"))
                            posts.append(
                                {
                                    "post_index": len(posts) + 1,
                                    "shortcode": tw_id,
                                    "tweet_id": tw_id,
                                    "url": f"https://x.com/{clean_username}/status/{tw_id}",
                                    "caption": (tw.get("text") or "")[:200],
                                    "comment_count": tm.get("reply_count", 0),
                                    "like_count": tm.get("like_count", 0),
                                    "retweet_count": tm.get("retweet_count", 0),
                                    "published_at": pub_dt.strftime("%Y-%m-%d %H:%M:%S"),
                                }
                            )
                            if len(posts) >= count:
                                break
            return profile_meta, posts[:count]

        return profile_meta, posts

    async def fetch_profile_recent_posts(self, username: str, count: int = 5) -> List[Dict[str, Any]]:
        """Return only the recent posts list for an X profile."""
        _, posts = await self.fetch_profile_info_and_posts(username, count=count)
        return posts

