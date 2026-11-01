"""Facebook Collector supporting both Comet GraphQL, embedded Relay JSON extraction, and Playwright fallback."""

import json
import re
from typing import AsyncIterator, Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone
import httpx
import structlog
from src.config import settings
from src.collectors.base import BaseCollector, ScraperError
from src.schemas.comment import NormalizedComment
from src.db.models import MonitoredTarget
from src.collectors.pool_manager import SessionPoolManager

logger = structlog.get_logger(__name__)

# Standard browser headers for Facebook requests
FB_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
}


def parse_count_str(val: Any) -> Optional[int]:
    """Parse numeric count string with abbreviations (e.g., '119M', '45.2K', '1,200') to integer."""
    if isinstance(val, (int, float)):
        return int(val)
    if not isinstance(val, str):
        return None
    cleaned = val.strip().replace(",", "").replace(" ", "").upper()
    try:
        if cleaned.endswith("K"):
            return int(float(cleaned[:-1]) * 1_000)
        elif cleaned.endswith("M"):
            return int(float(cleaned[:-1]) * 1_000_000)
        elif cleaned.endswith("B"):
            return int(float(cleaned[:-1]) * 1_000_000_000)
        return int(float(cleaned))
    except (ValueError, TypeError):
        return None


def extract_feedback_id_and_tokens(html: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract feedbackTargetID, fb_dtsg, and jazoest from Facebook HTML."""
    feedback_id = None
    fb_dtsg = None
    jazoest = None

    # Feedback target ID patterns
    fbid_m = (
        re.search(r'"feedback_id":"([^"]+)"', html)
        or re.search(r'"feedbackTargetID":"([^"]+)"', html)
        or re.search(r'"feedback":\{"id":"([^"]+)"', html)
        or re.search(r'"top_level_post_id":"(\d+)"', html)
        or re.search(r'data-ft=\'[^\']*"top_level_post_id":"(\d+)"', html)
    )
    if fbid_m:
        feedback_id = fbid_m.group(1)

    # fb_dtsg patterns
    dtsg_m = (
        re.search(r'\["DTSGInitialData",\[\],\{"token":"([^"]+)"\}', html)
        or re.search(r'"DTSG_TOKEN":"([^"]+)"', html)
        or re.search(r'name="fb_dtsg" value="([^"]+)"', html)
        or re.search(r'"token":"(AQ[A-Za-z0-9_-]+:[A-Za-z0-9_-]+)"', html)
    )
    if dtsg_m:
        fb_dtsg = dtsg_m.group(1)

    # jazoest patterns
    jazo_m = (
        re.search(r'name="jazoest" value="(\d+)"', html)
        or re.search(r'"jazoest":"?(\d+)"?', html)
    )
    if jazo_m:
        jazoest = jazo_m.group(1)

    return feedback_id, fb_dtsg, jazoest


def parse_profile_meta_from_html(html: str, username: str) -> Dict[str, Any]:
    """Parse profile metadata (name, bio, followers, avatar) from Facebook page HTML."""
    clean_username = username.strip().lstrip("@").strip("/")
    profile_meta: Dict[str, Any] = {
        "platform": "facebook",
        "username": f"@{clean_username}" if clean_username else "",
        "url": f"https://www.facebook.com/{clean_username}/" if clean_username else "",
        "full_name": None,
        "bio": None,
        "followers": None,
        "following": None,
        "total_posts": None,
        "is_verified": False,
        "avatar_url": None,
    }

    # Extract OpenGraph / Meta tags
    title_m = re.search(r'<meta property="og:title" content="([^"]+)"', html, re.IGNORECASE)
    if not title_m:
        title_m = re.search(r'<title>([^<]+)</title>', html, re.IGNORECASE)
    if title_m:
        raw_title = title_m.group(1).replace("&#039;", "'").replace("&amp;", "&")
        # Remove suffixes like " - Home | Facebook" or " | Facebook"
        cleaned_title = re.sub(r"\s*[-|]\s*(?:Home\s*\|\s*)?Facebook.*$", "", raw_title, flags=re.IGNORECASE).strip()
        if cleaned_title:
            profile_meta["full_name"] = cleaned_title

    desc_m = re.search(r'<meta property="og:description" content="([^"]+)"', html, re.IGNORECASE)
    if desc_m:
        raw_desc = desc_m.group(1).replace("&#039;", "'").replace("&amp;", "&")
        profile_meta["bio"] = " ".join(raw_desc.split())[:250]

        # Check for follower counts inside description (e.g. "119M likes · 119M followers")
        fol_m = re.search(r"([\d.,]+[KMBkmb]?)\s+followers", raw_desc, re.IGNORECASE)
        if fol_m:
            profile_meta["followers"] = parse_count_str(fol_m.group(1))
        else:
            likes_m = re.search(r"([\d.,]+[KMBkmb]?)\s+likes", raw_desc, re.IGNORECASE)
            if likes_m:
                profile_meta["followers"] = parse_count_str(likes_m.group(1))

    img_m = re.search(r'<meta property="og:image" content="([^"]+)"', html, re.IGNORECASE)
    if img_m:
        profile_meta["avatar_url"] = img_m.group(1).replace("&amp;", "&")

    if re.search(r'"is_verified":\s*true', html, re.IGNORECASE) or re.search(r'"verified":\s*true', html, re.IGNORECASE):
        profile_meta["is_verified"] = True

    return profile_meta


def extract_comments_from_json(
    data: Any,
    post_id: str,
    source_name: str,
    post_url: str,
) -> List[NormalizedComment]:
    """Recursively search Relay / GraphQL JSON objects for Facebook comments."""
    results: List[NormalizedComment] = []
    seen_ids = set()

    def _walk(obj: Any):
        if isinstance(obj, dict):
            # Check if this node is a comment
            node_body = obj.get("body") or {}
            node_text = node_body.get("text", "") if isinstance(node_body, dict) else (str(node_body) if isinstance(node_body, str) else "")
            author_obj = obj.get("author") or {}
            c_id = obj.get("legacy_fbid") or obj.get("id")

            if c_id and (node_text or author_obj) and isinstance(author_obj, dict):
                c_id_str = str(c_id)
                if c_id_str not in seen_ids:
                    seen_ids.add(c_id_str)
                    author_name = author_obj.get("name") or "Facebook User"
                    author_id = str(author_obj.get("id") or "unknown")
                    author_url = author_obj.get("url") or (f"https://www.facebook.com/{author_id}" if author_id != "unknown" else None)
                    created_time = obj.get("created_time")
                    pub_dt = (
                        datetime.fromtimestamp(created_time, tz=timezone.utc)
                        if (created_time and isinstance(created_time, (int, float)))
                        else datetime.now(timezone.utc)
                    )
                    c_url = f"{post_url}?comment_id={c_id_str}"

                    results.append(
                        NormalizedComment(
                            platform="facebook",
                            external_comment_id=f"fb_{c_id_str}",
                            parent_external_id=post_id,
                            target_external_id=post_id,
                            source_name=source_name,
                            post_url=post_url,
                            comment_url=c_url,
                            author_external_id=author_id,
                            author_username=author_name,
                            author_display_name=author_name,
                            author_profile_url=author_url,
                            content_text=node_text,
                            published_at=pub_dt,
                        )
                    )

            # Recurse into dict values
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(data)
    return results


def extract_posts_from_json(data: Any, base_username: str = "") -> List[Dict[str, Any]]:
    """Recursively search Relay / GraphQL JSON objects for user timeline posts."""
    posts: List[Dict[str, Any]] = []
    seen_urls = set()

    def _walk(obj: Any):
        if isinstance(obj, dict):
            # Story or timeline unit candidate
            msg_obj = obj.get("message")
            story_url = obj.get("url") or obj.get("share_url") or obj.get("story_url")
            c_time = obj.get("creation_time") or obj.get("publish_time")
            fbid = obj.get("legacy_fbid") or obj.get("post_id") or obj.get("id")

            caption = ""
            if isinstance(msg_obj, dict):
                caption = msg_obj.get("text", "")
            elif isinstance(msg_obj, str):
                caption = msg_obj

            if (story_url or fbid) and (caption or c_time):
                p_url = story_url or (f"https://www.facebook.com/{fbid}" if fbid else "")
                if p_url and p_url not in seen_urls:
                    # Clean post id / shortcode
                    shortcode = str(fbid or "")
                    if not shortcode and p_url:
                        m = re.search(r"/posts/([A-Za-z0-9_.-]+)", p_url) or re.search(r"story_fbid=([A-Za-z0-9_.-]+)", p_url)
                        shortcode = m.group(1) if m else p_url.split("/")[-1]

                    feedback = obj.get("feedback") or {}
                    like_cnt = (
                        feedback.get("reaction_count", {}).get("count")
                        or feedback.get("reactors", {}).get("count")
                        or 0
                    )
                    comm_cnt = (
                        feedback.get("comments_count_summary", {}).get("total_count")
                        or feedback.get("comments", {}).get("total_count")
                        or 0
                    )

                    pub_dt = (
                        datetime.fromtimestamp(c_time, tz=timezone.utc)
                        if (c_time and isinstance(c_time, (int, float)))
                        else None
                    )

                    seen_urls.add(p_url)
                    posts.append(
                        {
                            "shortcode": shortcode,
                            "url": p_url,
                            "caption": " ".join(caption.split())[:160] if caption else "",
                            "comment_count": int(comm_cnt or 0),
                            "like_count": int(like_cnt or 0),
                            "published_at": pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else None,
                            "timestamp": int(c_time) if c_time else 0,
                        }
                    )

            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(data)
    # Sort posts newest first
    posts.sort(key=lambda p: p.get("timestamp") or 0, reverse=True)
    return posts


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

    async def _resolve_canonical_url(self, raw_url: str, client: httpx.AsyncClient) -> str:
        """Resolve short link redirects (fb.watch, share/p/...) to canonical post URL."""
        if any(marker in raw_url for marker in ("fb.watch", "/share/p/", "/share/v/", "/share/r/")):
            try:
                resp = await client.head(raw_url, follow_redirects=True, timeout=12.0)
                return str(resp.url)
            except Exception:
                try:
                    resp = await client.get(raw_url, follow_redirects=True, timeout=12.0)
                    return str(resp.url)
                except Exception:
                    pass
        return raw_url

    async def fetch_profile_info_and_posts(
        self,
        username_or_id: str,
        count: int = 5,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Fetch Facebook profile/page metadata and recent posts via HTTP and Playwright fallback."""
        clean_handle = username_or_id.strip().lstrip("@").split("?")[0].strip("/")
        if "facebook.com/" in clean_handle:
            clean_handle = clean_handle.split("facebook.com/")[-1].split("/")[0].lstrip("@")

        profile_url = f"https://www.facebook.com/{clean_handle}/"
        profile_meta = {
            "platform": "facebook",
            "username": f"@{clean_handle}",
            "url": profile_url,
            "full_name": clean_handle,
            "bio": None,
            "followers": None,
            "following": None,
            "total_posts": None,
            "is_verified": False,
        }

        cookies: Dict[str, str] = {}
        proxy_url: Optional[str] = None
        user_agent = FB_DEFAULT_HEADERS["User-Agent"]

        if self.pool_manager:
            session_tuple = await self.pool_manager.acquire_session("facebook")
            if session_tuple:
                session_obj, s_cookies, s_proxy = session_tuple
                if isinstance(s_cookies, dict):
                    cookies = s_cookies
                proxy_url = s_proxy
                if session_obj and session_obj.user_agent:
                    user_agent = session_obj.user_agent

        headers = dict(FB_DEFAULT_HEADERS)
        headers["User-Agent"] = user_agent

        recent_posts: List[Dict[str, Any]] = []

        # Engine 1: Direct HTTP GET
        try:
            transport = httpx.AsyncHTTPTransport(proxy=proxy_url) if proxy_url else None
            async with httpx.AsyncClient(headers=headers, cookies=cookies, transport=transport, timeout=20.0, follow_redirects=True) as client:
                resp = await client.get(profile_url)
                if resp.status_code == 200:
                    html_text = resp.text
                    meta = parse_profile_meta_from_html(html_text, clean_handle)
                    profile_meta.update({k: v for k, v in meta.items() if v is not None})

                    # Parse embedded scripts for posts
                    script_blocks = re.findall(r'<script[^>]*>(.*?)</script>', html_text, flags=re.DOTALL)
                    for block in script_blocks:
                        if "{" in block and ("timeline" in block or "story" in block or "feedback" in block):
                            try:
                                json_matches = re.findall(r'(\{(?:[^{}]|(?R))*\})', block)
                            except Exception:
                                json_matches = [block]
                            for jm in json_matches:
                                try:
                                    parsed = json.loads(jm)
                                    extracted = extract_posts_from_json(parsed, base_username=clean_handle)
                                    for p in extracted:
                                        if not any(x["shortcode"] == p["shortcode"] for x in recent_posts):
                                            recent_posts.append(p)
                                except Exception:
                                    continue
        except Exception as e:
            logger.warning("Facebook profile direct HTTP fetch failed", handle=clean_handle, error=str(e))

        # Engine 2: Playwright fallback if direct HTTP found no posts
        if not recent_posts:
            try:
                from playwright.async_api import async_playwright

                pw_cookies = []
                for name, val in cookies.items():
                    if val:
                        pw_cookies.append(
                            {
                                "name": str(name),
                                "value": str(val),
                                "domain": ".facebook.com",
                                "path": "/",
                                "secure": True,
                                "sameSite": "Lax",
                            }
                        )

                proxy_cfg = {"server": proxy_url} if proxy_url else None
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True, proxy=proxy_cfg)
                    try:
                        context = await browser.new_context(user_agent=user_agent)
                        if pw_cookies:
                            await context.add_cookies(pw_cookies)
                        page = await context.new_page()

                        intercepted_json: List[Dict[str, Any]] = []

                        async def handle_resp(response):
                            if "graphql" in response.url and response.status == 200:
                                try:
                                    intercepted_json.append(await response.json())
                                except Exception:
                                    pass

                        page.on("response", handle_resp)
                        await page.goto(profile_url, wait_until="domcontentloaded", timeout=25000)
                        await page.wait_for_timeout(3000)

                        # Extract any intercepted GraphQL timeline feeds
                        for j_data in intercepted_json:
                            pw_posts = extract_posts_from_json(j_data, base_username=clean_handle)
                            for post in pw_posts:
                                if not any(x["shortcode"] == post["shortcode"] for x in recent_posts):
                                    recent_posts.append(post)

                        # If still no posts, extract articles from DOM
                        if not recent_posts:
                            articles = await page.query_selector_all('div[role="feed"] div[role="article"], div[role="article"]')
                            for idx, art in enumerate(articles[:count]):
                                text_content = await art.inner_text()
                                if text_content:
                                    lines = [l.strip() for l in text_content.splitlines() if l.strip()]
                                    caption_snip = " ".join(lines[:3])[:160]
                                    post_link = profile_url
                                    link_el = await art.query_selector('a[href*="/posts/"], a[href*="story_fbid="], a[href*="/videos/"]')
                                    if link_el:
                                        href = await link_el.get_attribute("href")
                                        if href:
                                            post_link = href if href.startswith("http") else f"https://www.facebook.com{href}"

                                    recent_posts.append(
                                        {
                                            "shortcode": f"fb_post_{idx+1}",
                                            "url": post_link,
                                            "caption": caption_snip,
                                            "comment_count": 0,
                                            "like_count": 0,
                                            "published_at": None,
                                            "timestamp": int(datetime.now(timezone.utc).timestamp()),
                                        }
                                    )
                    finally:
                        await browser.close()
            except Exception as pw_err:
                logger.warning("Facebook profile Playwright fallback failed", handle=clean_handle, error=str(pw_err))

        # Add 1-based post_index
        for idx, p in enumerate(recent_posts):
            p["post_index"] = idx + 1

        return profile_meta, recent_posts[:count]

    async def fetch_profile_recent_posts(
        self,
        username_or_id: str,
        count: int = 5,
    ) -> List[Dict[str, Any]]:
        """Convenience helper to fetch only recent posts of a Facebook profile/page."""
        _, posts = await self.fetch_profile_info_and_posts(username_or_id, count=count)
        return posts

    async def _fetch_via_playwright(
        self,
        post_url: str,
        target: MonitoredTarget,
        cookies: Dict[str, str],
        user_agent: str,
        proxy_url: Optional[str],
        limit: int,
    ) -> List[NormalizedComment]:
        """Fallback: Use headless Chromium with session cookies to intercept Facebook comments."""
        from playwright.async_api import async_playwright

        comments: List[NormalizedComment] = []
        pw_cookies = []
        for name, val in cookies.items():
            if val:
                pw_cookies.append(
                    {
                        "name": str(name),
                        "value": str(val),
                        "domain": ".facebook.com",
                        "path": "/",
                        "secure": True,
                        "sameSite": "Lax",
                    }
                )

        proxy_cfg = {"server": proxy_url} if proxy_url else None
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, proxy=proxy_cfg)
            try:
                context = await browser.new_context(user_agent=user_agent)
                if pw_cookies:
                    await context.add_cookies(pw_cookies)
                page = await context.new_page()

                async def handle_response(response):
                    if "graphql" in response.url and response.status == 200:
                        try:
                            body_json = await response.json()
                            extracted = extract_comments_from_json(
                                body_json,
                                post_id=target.external_id,
                                source_name=target.title_or_name or "Facebook Post",
                                post_url=post_url,
                            )
                            for c in extracted:
                                if not any(x.external_comment_id == c.external_comment_id for x in comments):
                                    comments.append(c)
                        except Exception:
                            pass

                page.on("response", handle_response)
                await page.goto(post_url, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(3000)

                # Scroll down slightly to trigger lazy comment loading
                for _ in range(2):
                    if len(comments) >= limit:
                        break
                    await page.mouse.wheel(0, 800)
                    await page.wait_for_timeout(2000)

                # If GraphQL interception produced nothing, scrape comment articles from DOM
                if not comments:
                    comment_els = await page.query_selector_all('div[role="article"]')
                    for idx, el in enumerate(comment_els[:limit]):
                        raw_text = await el.inner_text()
                        if not raw_text:
                            continue
                        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
                        if not lines:
                            continue
                        author_name = lines[0] if len(lines) > 1 else "Facebook User"
                        content_body = " ".join(lines[1:]) if len(lines) > 1 else lines[0]

                        c_id = f"pw_{target.external_id}_{idx}"
                        comments.append(
                            NormalizedComment(
                                platform="facebook",
                                external_comment_id=c_id,
                                parent_external_id=target.external_id,
                                target_external_id=target.external_id,
                                source_name=target.title_or_name or "Facebook Post",
                                post_url=post_url,
                                comment_url=f"{post_url}?comment_id={c_id}",
                                author_external_id=author_name,
                                author_username=author_name,
                                author_display_name=author_name,
                                content_text=content_body,
                                published_at=datetime.now(timezone.utc),
                            )
                        )
            finally:
                await browser.close()

        return comments

    async def fetch_comments(
        self,
        target: MonitoredTarget,
        cursor: Optional[str] = None,
        limit: int = 5000,
    ) -> AsyncIterator[NormalizedComment]:
        """Paginate comments on a Facebook post using initial Relay JSON, Comet GraphQL, or Playwright fallback."""
        post_id = target.external_id
        post_url = target.url or f"https://www.facebook.com/{post_id}"

        cookies: Dict[str, str] = {}
        proxy_url: Optional[str] = None
        user_agent = FB_DEFAULT_HEADERS["User-Agent"]
        session_id: Optional[str] = None

        if self.pool_manager:
            session_tuple = await self.pool_manager.acquire_session("facebook")
            if session_tuple:
                session_obj, s_cookies, s_proxy = session_tuple
                if isinstance(s_cookies, dict):
                    cookies = s_cookies
                proxy_url = s_proxy
                session_id = session_obj.id if session_obj else None
                if session_obj and session_obj.user_agent:
                    user_agent = session_obj.user_agent

        headers = dict(FB_DEFAULT_HEADERS)
        headers["User-Agent"] = user_agent

        total_yielded = 0
        seen_comment_ids = set()

        transport = httpx.AsyncHTTPTransport(proxy=proxy_url) if proxy_url else None
        async with httpx.AsyncClient(headers=headers, cookies=cookies, transport=transport, timeout=25.0, follow_redirects=True) as client:
            canonical_url = await self._resolve_canonical_url(post_url, client)

            # Engine 1: Fetch post HTML to extract initial comments + feedbackTargetID + tokens
            feedback_target_id = None
            fb_dtsg = cookies.get("fb_dtsg", "")
            jazoest = None

            try:
                resp = await client.get(canonical_url)
                if resp.status_code == 429:
                    if session_id and self.pool_manager:
                        await self.pool_manager.report_rate_limit(session_id, cooldown_minutes=30)
                elif resp.status_code == 200:
                    html_content = resp.text
                    f_id, dtsg, jaz = extract_feedback_id_and_tokens(html_content)
                    if f_id:
                        feedback_target_id = f_id
                    if dtsg:
                        fb_dtsg = dtsg
                    if jaz:
                        jazoest = jaz

                    # Parse embedded JSON scripts for initial comment batch
                    script_blocks = re.findall(r'<script[^>]*>(.*?)</script>', html_content, flags=re.DOTALL)
                    for block in script_blocks:
                        if "{" in block and ("comments" in block or "feedback" in block):
                            try:
                                json_matches = re.findall(r'(\{(?:[^{}]|(?R))*\})', block)
                            except Exception:
                                json_matches = [block]
                            for jm in json_matches:
                                try:
                                    parsed = json.loads(jm)
                                    batch = extract_comments_from_json(
                                        parsed,
                                        post_id=post_id,
                                        source_name=target.title_or_name or "Facebook Post",
                                        post_url=canonical_url,
                                    )
                                    for comm in batch:
                                        if comm.external_comment_id not in seen_comment_ids:
                                            seen_comment_ids.add(comm.external_comment_id)
                                            yield comm
                                            total_yielded += 1
                                            if total_yielded >= limit:
                                                return
                                except Exception:
                                    continue
            except Exception as e:
                logger.warning("Facebook initial HTML comments fetch failed", post_id=post_id, error=str(e))

            # Engine 2: Paginate remaining comments via Comet GraphQL if feedbackTargetID is available
            effective_target_id = feedback_target_id or post_id
            if effective_target_id and total_yielded < limit:
                current_cursor = cursor
                graphql_url = "https://www.facebook.com/api/graphql/"
                gql_headers = {
                    "user-agent": user_agent,
                    "content-type": "application/x-www-form-urlencoded",
                }

                while total_yielded < limit:
                    variables = {
                        "feedbackTargetID": effective_target_id,
                        "commentOrderingMode": "RANKED_UNFILTERED",
                        "count": 50,
                        "cursor": current_cursor,
                    }
                    form_data = {
                        "doc_id": self.doc_id,
                        "variables": json.dumps(variables),
                        "fb_dtsg": fb_dtsg,
                    }
                    if jazoest:
                        form_data["jazoest"] = jazoest

                    try:
                        res = await client.post(graphql_url, data=form_data, headers=gql_headers)
                        if res.status_code == 429:
                            if session_id and self.pool_manager:
                                await self.pool_manager.report_rate_limit(session_id, cooldown_minutes=30)
                            break
                        if res.status_code != 200:
                            logger.debug("Facebook Comet GraphQL returned non-200", status=res.status_code)
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
                            if not c_id:
                                continue
                            c_id_str = str(c_id)
                            ext_id = f"fb_{c_id_str}"
                            if ext_id in seen_comment_ids:
                                continue

                            body = node.get("body", {}).get("text", "")
                            author = node.get("author", {})
                            author_name = author.get("name", "Facebook User")
                            author_id = str(author.get("id") or "unknown")
                            author_url = author.get("url")
                            created_time = node.get("created_time")
                            pub_dt = (
                                datetime.fromtimestamp(created_time, tz=timezone.utc)
                                if created_time
                                else datetime.now(timezone.utc)
                            )
                            c_url = f"{canonical_url}?comment_id={c_id_str}"

                            seen_comment_ids.add(ext_id)
                            yield NormalizedComment(
                                platform="facebook",
                                external_comment_id=ext_id,
                                parent_external_id=post_id,
                                target_external_id=post_id,
                                source_name=target.title_or_name or "Facebook Post",
                                post_url=canonical_url,
                                comment_url=c_url,
                                author_external_id=author_id,
                                author_username=author_name,
                                author_display_name=author_name,
                                author_profile_url=author_url,
                                content_text=body,
                                published_at=pub_dt,
                            )
                            total_yielded += 1
                            if total_yielded >= limit:
                                return

                        page_info = comment_conn.get("page_info", {})
                        has_next = page_info.get("has_next_page", False)
                        end_cursor = page_info.get("end_cursor")

                        if not has_next or not end_cursor or end_cursor == current_cursor:
                            break
                        current_cursor = end_cursor

                    except Exception as e:
                        logger.debug("Facebook Comet GraphQL pagination error", error=str(e))
                        break

        # Engine 3: Playwright fallback if direct HTTP and Comet returned zero comments
        if total_yielded == 0:
            try:
                pw_comments = await self._fetch_via_playwright(
                    post_url=canonical_url,
                    target=target,
                    cookies=cookies,
                    user_agent=user_agent,
                    proxy_url=proxy_url,
                    limit=limit,
                )
                for c in pw_comments:
                    if c.external_comment_id not in seen_comment_ids:
                        seen_comment_ids.add(c.external_comment_id)
                        yield c
                        total_yielded += 1
                        if total_yielded >= limit:
                            return
            except Exception as pw_err:
                logger.warning("Facebook Playwright fallback failed", post_id=post_id, error=str(pw_err))
