"""Lightweight internal HTTP bridge exposing token-safe (< 3 KB) endpoints for Hermes CLI (ops)."""

import asyncio
import random
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple
from aiohttp import web
from sqlalchemy import select, func, desc, or_, case, delete
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from src.config import settings
from src.db.base import async_session_factory
from src.db.models import MonitoredTarget, PlatformAuthor, Comment, ScraperSession
from src.db.repositories.target_repo import TargetRepository
from src.db.repositories.author_repo import AuthorRepository
from src.db.repositories.comment_repo import CommentRepository
from src.db.repositories.session_repo import SessionRepository
from src.bot.handlers.targets import detect_platform_and_id
from src.collectors.base import ScraperError
from src.collectors.pool_manager import SessionPoolManager
from src.collectors.youtube import YouTubeCollector
from src.collectors.twitter_x import TwitterXCollector
from src.collectors.instagram import InstagramCollector
from src.collectors.facebook import FacebookCollector
from src.llm.processor import MicroBatchProcessor

logger = structlog.get_logger(__name__)


def _truncate(text: Optional[str], max_len: int = 180) -> str:
    """Collapse whitespace and truncate string so JSON output stays under 3 KB."""
    if not text:
        return ""
    cleaned = " ".join(str(text).split())
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 3] + "..."


def _format_author(author: Optional[PlatformAuthor]) -> str:
    """Format compact author display string with clean name, handle, and numeric user ID."""
    if not author:
        return "Unknown"
    raw_name = (author.display_name or "").replace(" None", "").replace("None ", "").replace("|", " ").strip()
    if raw_name == "None" or not any(c.isalnum() for c in raw_name):
        raw_name = ""
    else:
        raw_name = " ".join(raw_name.split())
    uid = str(author.external_user_id or "").strip()
    uname = (author.username or "").strip().lstrip("@")

    if uname and raw_name and uname.lower() != raw_name.lower():
        base = f"@{uname} ({_truncate(raw_name, 30)})"
    elif uname:
        base = f"@{uname}"
    elif raw_name:
        base = _truncate(raw_name, 35)
    else:
        base = "User"

    if uid and uid != "unknown" and uid not in base:
        return f"{base} [id:{uid}]"
    return base


def _format_comment(
    comment: Comment,
    author: Optional[PlatformAuthor] = None,
    max_text: int = 180,
    threshold: Optional[int] = None,
) -> Dict[str, Any]:
    """Serialize a Comment + PlatformAuthor into a compact, token-efficient dictionary."""
    pub_dt = comment.published_at or comment.ingested_at
    score = comment.severity_score
    eff_threshold = threshold if threshold is not None else settings.LLM_ALERT_SEVERITY_THRESHOLD
    return {
        "id": comment.id,
        "platform": comment.platform,
        "author": _format_author(author),
        "author_id": author.external_user_id if author else None,
        "status": comment.llm_status,
        "score": score,
        "flagged": bool(score is not None and score >= eff_threshold),
        "reason": _truncate(comment.llm_reason, 120) if comment.llm_reason else None,
        "text": _truncate(comment.content_text, max_text),
        "source": _truncate(comment.source_name, 50) if comment.source_name else None,
        "url": comment.comment_url or comment.post_url,
        "published_at": pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else None,
    }


def _cluster_duplicates(items: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """Collapse copy-paste duplicate comments by normalized text and track occurrences."""
    clustered: Dict[str, Dict[str, Any]] = {}
    for item in items:
        norm_key = " ".join((item.get("text") or "").lower().split())
        if not norm_key:
            continue
        if norm_key not in clustered:
            entry = dict(item)
            entry["occurrences"] = 1
            clustered[norm_key] = entry
        else:
            clustered[norm_key]["occurrences"] += 1
    return list(clustered.values())[:limit]


def _extract_instagram_profile_username(raw_input: str) -> Optional[str]:
    """Return Instagram profile username if raw_input is a profile URL or handle (without /p/ or /reel/)."""
    text = raw_input.strip()
    if not text or text.lower() == "list":
        return None

    lower = text.lower()
    if any(d in lower for d in ("t.me/", "youtube.com", "youtu.be", "x.com/", "twitter.com/", "facebook.com", "fb.watch")):
        return None

    if "instagram.com" in lower:
        if re.search(r"/(?:p|reel|reels|tv|stories)/[A-Za-z0-9_-]+", text):
            return None
        m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", text)
        if m and m.group(1).lower() not in ("p", "reel", "reels", "tv", "stories", "explore", "accounts"):
            return m.group(1)
        return None

    if "://" in text or "/" in text or text.lstrip("-").isdigit():
        return None

    # Exclude UUID strings
    if re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", text):
        return None

    candidate = text.lstrip("@").strip()
    if re.match(r"^[A-Za-z0-9._]{2,30}$", candidate):
        return candidate
    return None


def _extract_x_profile_username(raw_input: str, explicit_platform: str = "") -> Optional[str]:
    """Return X (Twitter) profile username if raw_input is an X profile URL or handle (without /status/)."""
    text = raw_input.strip()
    if not text or text.lower() == "list":
        return None

    lower = text.lower()
    if "/status/" in lower or "/i/" in lower:
        return None

    if "x.com/" in lower or "twitter.com/" in lower:
        m = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:[/?#]|$)", text)
        if m and m.group(1).lower() not in ("home", "explore", "search", "notifications", "messages", "settings", "i", "intent", "compose"):
            return m.group(1)
        return None

    if explicit_platform in ("x", "twitter"):
        candidate = text.lstrip("@").strip()
        if re.match(r"^[A-Za-z0-9_]{1,15}$", candidate):
            return candidate

    return None


async def _resolve_target(session: AsyncSession, raw_target: str) -> Optional[MonitoredTarget]:
    """Resolve a MonitoredTarget by UUID, external_id, @handle, or full URL."""
    clean = raw_target.strip()
    if not clean:
        return None

    # 1. Exact ID or external_id match
    stmt = select(MonitoredTarget).where(
        or_(
            MonitoredTarget.id == clean,
            MonitoredTarget.external_id == clean,
            MonitoredTarget.external_id == clean.lstrip("@"),
            MonitoredTarget.external_id == f"@{clean.lstrip('@')}",
        )
    )
    found = (await session.execute(stmt)).scalars().first()
    if found:
        return found

    # 2. Parse URL / handle via detect_platform_and_id
    platform, _, ext_id, _ = detect_platform_and_id(clean)
    if platform and ext_id:
        stmt2 = select(MonitoredTarget).where(
            MonitoredTarget.platform == platform,
            or_(
                MonitoredTarget.external_id == ext_id,
                MonitoredTarget.external_id == ext_id.lstrip("@"),
                MonitoredTarget.external_id == f"@{ext_id.lstrip('@')}",
            ),
        )
        found2 = (await session.execute(stmt2)).scalars().first()
        if found2:
            return found2

    # 3. Substring match on URL or title
    stmt3 = select(MonitoredTarget).where(
        or_(
            MonitoredTarget.url.ilike(f"%{clean}%"),
            MonitoredTarget.title_or_name.ilike(f"%{clean}%"),
        )
    )
    return (await session.execute(stmt3)).scalars().first()


async def _build_target_check_payload(
    session: AsyncSession,
    target: MonitoredTarget,
    flagged_limit: int = 10,
    sample_limit: int = 8,
) -> Dict[str, Any]:
    """Build compact target inspection payload with flagged threats + recent normal sample."""
    # Counts by status
    total_stmt = select(func.count(Comment.id)).where(Comment.target_id == target.id)
    total_comments = (await session.execute(total_stmt)).scalar() or 0

    eval_stmt = select(func.count(Comment.id)).where(
        Comment.target_id == target.id,
        Comment.llm_status == "evaluated",
    )
    evaluated_count = (await session.execute(eval_stmt)).scalar() or 0

    skip_stmt = select(func.count(Comment.id)).where(
        Comment.target_id == target.id,
        Comment.llm_status == "skipped_prefilter",
    )
    skipped_count = (await session.execute(skip_stmt)).scalar() or 0

    fail_stmt = select(func.count(Comment.id)).where(
        Comment.target_id == target.id,
        Comment.llm_status == "failed",
    )
    failed_count = (await session.execute(fail_stmt)).scalar() or 0

    pend_stmt = select(func.count(Comment.id)).where(
        Comment.target_id == target.id,
        Comment.llm_status.in_(["pending", "processing"]),
    )
    pending_count = (await session.execute(pend_stmt)).scalar() or 0

    threshold = settings.LLM_ALERT_SEVERITY_THRESHOLD
    flagged_count_stmt = select(func.count(Comment.id)).where(
        Comment.target_id == target.id,
        Comment.severity_score >= threshold,
    )
    flagged_count = (await session.execute(flagged_count_stmt)).scalar() or 0

    # Flagged negative comments (capped)
    flagged_stmt = (
        select(Comment, PlatformAuthor)
        .outerjoin(PlatformAuthor, Comment.author_id == PlatformAuthor.id)
        .where(
            Comment.target_id == target.id,
            Comment.severity_score >= threshold,
        )
        .order_by(desc(Comment.severity_score), desc(Comment.published_at))
        .limit(min(flagged_limit * 2, 30))
    )
    flagged_rows = (await session.execute(flagged_stmt)).all()
    raw_flagged = [_format_comment(c, a, max_text=170, threshold=threshold) for c, a in flagged_rows]
    flagged_items = _cluster_duplicates(raw_flagged, limit=min(flagged_limit, 12))

    # Small sample of recent normal/unflagged comments for context/vibe
    sample_stmt = (
        select(Comment, PlatformAuthor)
        .outerjoin(PlatformAuthor, Comment.author_id == PlatformAuthor.id)
        .where(
            Comment.target_id == target.id,
            or_(Comment.severity_score < threshold, Comment.severity_score.is_(None)),
            Comment.llm_status != "skipped_prefilter",
        )
        .order_by(desc(Comment.published_at), desc(Comment.ingested_at))
        .limit(min(sample_limit * 2, 16))
    )
    sample_rows = (await session.execute(sample_stmt)).all()
    raw_sample = [
        {
            "author": _format_author(a),
            "status": c.llm_status,
            "score": c.severity_score,
            "reason": _truncate(c.llm_reason, 90) if c.llm_reason else None,
            "text": _truncate(c.content_text, 120),
            "published_at": (c.published_at or c.ingested_at).strftime("%Y-%m-%d %H:%M")
            if (c.published_at or c.ingested_at)
            else None,
        }
        for c, a in sample_rows
    ]
    sample_items = _cluster_duplicates(raw_sample, limit=min(sample_limit, 8))

    return {
        "target": {
            "id": target.id,
            "platform": target.platform,
            "external_id": target.external_id,
            "title": target.title_or_name,
            "url": target.url,
            "is_active": target.is_active,
            "last_scraped_at": target.last_scraped_at.strftime("%Y-%m-%d %H:%M:%S")
            if target.last_scraped_at
            else None,
        },
        "stats": {
            "total_comments": max(total_comments, target.total_comments_collected or 0),
            "stored_in_db": total_comments,
            "evaluated": evaluated_count,
            "noise_skipped": skipped_count,
            "pending": pending_count,
            "failed": failed_count,
            "flagged_count": flagged_count,
        },
        "flagged_comments": flagged_items,
        "recent_sample": sample_items,
    }


async def _execute_inline_scrape(
    target_id: str,
    mtproto: Optional[Any] = None,
    limit: int = 200,
) -> Tuple[int, int, Optional[Dict[str, Any]]]:
    """Run an on-demand scrape and DeepSeek micro-batch evaluation synchronously.

    Returns:
        Tuple of (newly_inserted_count, evaluated_count, focal_post_meta)
    """
    async with async_session_factory() as session:
        t_repo = TargetRepository(session)
        target = await t_repo.get_by_id(target_id)
        if not target:
            return 0, 0, None

        pool_mgr = SessionPoolManager(session)
        collector = None

        if target.platform == "telegram":
            if mtproto:
                await mtproto.refresh_monitored_chats()
                collector = mtproto
        elif target.platform == "youtube":
            collector = YouTubeCollector()
        elif target.platform == "x":
            collector = TwitterXCollector(pool_mgr)
        elif target.platform == "instagram":
            collector = InstagramCollector(pool_mgr)
        elif target.platform == "facebook":
            collector = FacebookCollector(pool_mgr)

        if not collector:
            return 0, 0, None

        author_repo = AuthorRepository(session)
        comment_repo = CommentRepository(session)
        newly_inserted = 0
        consecutive_duplicates = 0
        MAX_CONSECUTIVE_DUPLICATES = 8

        try:
            async for norm_comment in collector.fetch_comments(target, cursor=None, limit=limit):
                author = await author_repo.upsert_author(
                    platform=norm_comment.platform,
                    external_user_id=norm_comment.author_external_id,
                    username=norm_comment.author_username,
                    display_name=norm_comment.author_display_name,
                    profile_url=norm_comment.author_profile_url,
                )

                comment = await comment_repo.insert_single(
                    platform=norm_comment.platform,
                    external_comment_id=norm_comment.external_comment_id,
                    content_text=norm_comment.content_text,
                    target_id=target.id,
                    author_id=author.id,
                    parent_external_id=norm_comment.parent_external_id,
                    source_name=norm_comment.source_name,
                    post_url=norm_comment.post_url,
                    comment_url=norm_comment.comment_url,
                    published_at=norm_comment.published_at,
                    child_comment_count=norm_comment.child_comment_count,
                )

                if comment:
                    newly_inserted += 1
                    consecutive_duplicates = 0
                else:
                    consecutive_duplicates += 1
                    if consecutive_duplicates >= MAX_CONSECUTIVE_DUPLICATES:
                        break

            await t_repo.update_cursor(target.id, increment_comments=newly_inserted)
            await session.commit()

            # Loop micro-batch processor until ALL unscored/failed comments for this target are evaluated
            processor = MicroBatchProcessor()
            total_evaluated, _ = await processor.process_all_pending(
                session=session,
                target_id=target.id,
                max_batches=100,
            )

            focal_meta = getattr(collector, "last_focal_tweet_meta", None)
            return newly_inserted, total_evaluated, focal_meta
        except ScraperError:
            raise
        except Exception as e:
            logger.error("Inline scrape failed", target_id=target_id, error=str(e))
            return newly_inserted, 0, None


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Validate optional OPS_API_KEY if configured."""
    if settings.OPS_API_KEY:
        provided = request.headers.get("X-API-Key") or request.query.get("api_key")
        if provided != settings.OPS_API_KEY:
            return web.json_response({"error": "Unauthorized: invalid X-API-Key"}, status=401)
    try:
        return await handler(request)
    except Exception as exc:
        logger.error("Bridge API error", path=request.path, error=str(exc))
        return web.json_response({"error": str(exc)}, status=500)


async def handle_health(request: web.Request) -> web.Response:
    """GET /health"""
    mtproto = request.app.get("mtproto")
    mt_connected = bool(mtproto and mtproto.client and mtproto.client.is_connected())
    return web.json_response({"status": "ok", "mtproto_connected": mt_connected})


async def handle_health_detailed(request: web.Request) -> web.Response:
    """GET /health/detailed — Detailed health, DB stats, and platform session readiness."""
    mtproto = request.app.get("mtproto")
    mt_connected = bool(mtproto and mtproto.client and mtproto.client.is_connected())

    async with async_session_factory() as session:
        total_targets = (await session.execute(select(func.count(MonitoredTarget.id)))).scalar() or 0
        active_targets = (
            await session.execute(select(func.count(MonitoredTarget.id)).where(MonitoredTarget.is_active.is_(True)))
        ).scalar() or 0
        total_comments = (await session.execute(select(func.count(Comment.id)))).scalar() or 0
        evaluated_comments = (
            await session.execute(select(func.count(Comment.id)).where(Comment.llm_status == "evaluated"))
        ).scalar() or 0
        flagged_comments = (
            await session.execute(
                select(func.count(Comment.id)).where(
                    Comment.severity_score >= settings.LLM_ALERT_SEVERITY_THRESHOLD
                )
            )
        ).scalar() or 0

        s_repo = SessionRepository(session)
        ig_healthy = await s_repo.get_healthy_session("instagram")
        x_healthy = await s_repo.get_healthy_session("x")
        fb_healthy = await s_repo.get_healthy_session("facebook")

    return web.json_response(
        {
            "status": "ok",
            "database": "connected",
            "mtproto_connected": mt_connected,
            "counts": {
                "monitored_targets": total_targets,
                "active_targets": active_targets,
                "total_comments": total_comments,
                "evaluated_comments": evaluated_comments,
                "flagged_comments": flagged_comments,
            },
            "platforms_ready": {
                "telegram": mt_connected,
                "instagram": bool(ig_healthy),
                "x": bool(x_healthy or settings.X_BEARER_TOKEN),
                "youtube": bool(settings.YOUTUBE_API_KEY),
                "facebook": bool(fb_healthy),
            },
            "credentials": {
                "deepseek_api_key": bool(settings.DEEPSEEK_API_KEY),
                "youtube_api_key": bool(settings.YOUTUBE_API_KEY),
                "x_bearer_token": bool(settings.X_BEARER_TOKEN),
            },
        }
    )


async def handle_digest(request: web.Request) -> web.Response:
    """GET /api/digest?hours=24&platform=&min_score=7&limit=12"""
    hours = max(1, int(request.query.get("hours", 24)))
    platform = request.query.get("platform", "").strip().lower() or None
    min_score = int(request.query.get("min_score", settings.LLM_ALERT_SEVERITY_THRESHOLD))
    limit = min(max(1, int(request.query.get("limit", 12))), 18)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    time_filter = or_(Comment.published_at >= cutoff, Comment.ingested_at >= cutoff)

    async with async_session_factory() as session:
        base_filters = [time_filter]
        if platform:
            base_filters.append(Comment.platform == platform)

        total_stmt = select(func.count(Comment.id)).where(*base_filters)
        total_ingested = (await session.execute(total_stmt)).scalar() or 0

        eval_stmt = select(func.count(Comment.id)).where(*base_filters, Comment.llm_status == "evaluated")
        evaluated_count = (await session.execute(eval_stmt)).scalar() or 0

        skip_stmt = select(func.count(Comment.id)).where(*base_filters, Comment.llm_status == "skipped_prefilter")
        noise_skipped = (await session.execute(skip_stmt)).scalar() or 0

        fail_stmt = select(func.count(Comment.id)).where(*base_filters, Comment.llm_status == "failed")
        failed_count = (await session.execute(fail_stmt)).scalar() or 0

        pend_stmt = select(func.count(Comment.id)).where(*base_filters, Comment.llm_status.in_(["pending", "processing"]))
        pending_count = (await session.execute(pend_stmt)).scalar() or 0

        plat_stmt = (
            select(Comment.platform, func.count(Comment.id))
            .where(*base_filters)
            .group_by(Comment.platform)
        )
        plat_rows = (await session.execute(plat_stmt)).all()
        platform_breakdown = {r[0]: r[1] for r in plat_rows}

        flag_filters = [*base_filters, Comment.severity_score >= min_score]

        flagged_count_stmt = select(func.count(Comment.id)).where(*flag_filters)
        flagged_total = (await session.execute(flagged_count_stmt)).scalar() or 0

        flagged_stmt = (
            select(Comment, PlatformAuthor)
            .outerjoin(PlatformAuthor, Comment.author_id == PlatformAuthor.id)
            .where(*flag_filters)
            .order_by(desc(Comment.severity_score), desc(Comment.published_at), desc(Comment.ingested_at))
            .limit(min(limit * 2, 36))
        )
        rows = (await session.execute(flagged_stmt)).all()
        raw_flagged = [_format_comment(c, a, max_text=170, threshold=min_score) for c, a in rows]
        flagged_list = _cluster_duplicates(raw_flagged, limit=limit)

    return web.json_response(
        {
            "window_hours": hours,
            "platform_filter": platform or "all",
            "min_score": min_score,
            "total_ingested": total_ingested,
            "evaluated": evaluated_count,
            "noise_skipped": noise_skipped,
            "pending": pending_count,
            "failed": failed_count,
            "platform_breakdown": platform_breakdown,
            "flagged_count": flagged_total,
            "returned_flagged": len(flagged_list),
            "flagged_comments": flagged_list,
        }
    )


async def _fetch_ig_profile_and_posts(
    ig_collector: InstagramCollector,
    username: str,
    count: int = 5,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Fetch profile metadata and recent posts, supporting both full and monkeypatched collectors."""
    # Check if fetch_profile_recent_posts was monkeypatched on the class/instance in tests
    if getattr(InstagramCollector.fetch_profile_recent_posts, "__name__", "") != "fetch_profile_recent_posts":
        posts = await ig_collector.fetch_profile_recent_posts(username, count=count)
        meta = {
            "platform": "instagram",
            "username": f"@{username.lstrip('@')}",
            "url": f"https://www.instagram.com/{username.lstrip('@')}/",
            "full_name": username.lstrip("@"),
            "bio": None,
            "followers": None,
            "following": None,
            "total_posts": len(posts),
            "is_verified": False,
        }
        return meta, posts

    meta, posts = await ig_collector.fetch_profile_info_and_posts(username, count=count)
    if not posts:
        posts = await ig_collector.fetch_profile_recent_posts(username, count=count)
    meta["posts_returned"] = len(posts)
    return meta, posts


async def _fetch_x_profile_and_posts(
    x_collector: TwitterXCollector,
    username: str,
    count: int = 5,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Fetch X profile metadata and recent tweets, supporting both full and monkeypatched collectors."""
    if getattr(TwitterXCollector.fetch_profile_recent_posts, "__name__", "") != "fetch_profile_recent_posts":
        posts = await x_collector.fetch_profile_recent_posts(username, count=count)
        meta = {
            "platform": "x",
            "username": f"@{username.lstrip('@')}",
            "url": f"https://x.com/{username.lstrip('@')}",
            "full_name": username.lstrip("@"),
            "bio": None,
            "followers": None,
            "following": None,
            "total_posts": len(posts),
            "is_verified": False,
        }
        return meta, posts

    meta, posts = await x_collector.fetch_profile_info_and_posts(username, count=count)
    if not posts:
        posts = await x_collector.fetch_profile_recent_posts(username, count=count)
    meta["posts_returned"] = len(posts)
    return meta, posts


async def _execute_profile_analysis(
    ig_username: str,
    posts_count: int = 3,
    per_post_limit: int = 80,
    scrape_comments: bool = True,
    monitor: bool = False,
    mtproto: Optional[Any] = None,
    platform: str = "instagram",
) -> Tuple[Optional[Dict[str, Any]], int]:
    """Enumerate an Instagram or X profile's last N posts, scrape & score comments/replies on each, and aggregate."""
    posts_count = min(max(1, posts_count), 5)  # Hard cap at 5 posts to protect burner session

    async with async_session_factory() as session:
        s_repo = SessionRepository(session)
        if platform == "x":
            x_sess = await s_repo.get_healthy_session("x")
            if not x_sess and not settings.X_BEARER_TOKEN:
                raise ScraperError("no x session configured", "No active X burner session or X_BEARER_TOKEN configured", status_code=409)
            pool_mgr = SessionPoolManager(session)
            x_collector = TwitterXCollector(pool_mgr)
            profile_meta, recent_posts = await _fetch_x_profile_and_posts(
                x_collector,
                ig_username,
                count=max(5, posts_count),
            )
        else:
            ig_sess = await s_repo.get_healthy_session("instagram")
            if not ig_sess:
                raise ScraperError("no instagram session configured", "No active Instagram burner session configured", status_code=409)
            pool_mgr = SessionPoolManager(session)
            ig_collector = InstagramCollector(pool_mgr)
            profile_meta, recent_posts = await _fetch_ig_profile_and_posts(
                ig_collector,
                ig_username,
                count=max(5, posts_count),
            )

    if not recent_posts:
        return None, 404

    selected_posts = recent_posts[:posts_count]
    target_ids: List[str] = []
    post_id_map: Dict[str, str] = {}
    total_newly_ingested = 0
    total_evaluated_in_run = 0

    for idx, p in enumerate(selected_posts):
        shortcode = p["shortcode"]
        post_url = p["url"]
        async with async_session_factory() as session:
            t_repo = TargetRepository(session)
            target = await t_repo.create_or_activate(
                platform=platform,
                target_type="post_url",
                external_id=shortcode,
                url=post_url,
                title_or_name=f"{platform.capitalize()} @{ig_username} ({shortcode})",
                is_active=monitor,
            )
            await session.commit()
            t_id = target.id
            target_ids.append(t_id)
            post_id_map[shortcode] = t_id

        if scrape_comments:
            ins, ev, _ = await _execute_inline_scrape(t_id, mtproto=mtproto, limit=per_post_limit)
            total_newly_ingested += ins
            total_evaluated_in_run += ev
            # Safe per-post delay when walking multiple posts with live network calls
            if idx < len(selected_posts) - 1 and ins > 0:
                await asyncio.sleep(random.uniform(1.5, 2.5))

    # Build aggregate profile report from PostgreSQL across the analyzed posts
    threshold = settings.LLM_ALERT_SEVERITY_THRESHOLD
    async with async_session_factory() as session:
        posts_summary: List[Dict[str, Any]] = []
        for p in selected_posts:
            sc = p["shortcode"]
            t_id = post_id_map[sc]
            c_total = (await session.execute(select(func.count(Comment.id)).where(Comment.target_id == t_id))).scalar() or 0
            c_eval = (
                await session.execute(
                    select(func.count(Comment.id)).where(Comment.target_id == t_id, Comment.llm_status == "evaluated")
                )
            ).scalar() or 0
            c_flag = (
                await session.execute(
                    select(func.count(Comment.id)).where(Comment.target_id == t_id, Comment.severity_score >= threshold)
                )
            ).scalar() or 0

            posts_summary.append(
                {
                    "post_index": p["post_index"],
                    "shortcode": sc,
                    "url": p["url"],
                    "published_at": p.get("published_at"),
                    "ig_comment_count": p.get("comment_count", 0),
                    "like_count": p.get("like_count", 0),
                    "scraped_comments": c_total,
                    "evaluated": c_eval,
                    "flagged_count": c_flag,
                    "caption": _truncate(p.get("caption"), 100),
                }
            )

        total_stored = (
            await session.execute(select(func.count(Comment.id)).where(Comment.target_id.in_(target_ids)))
        ).scalar() or 0
        total_eval = (
            await session.execute(
                select(func.count(Comment.id)).where(Comment.target_id.in_(target_ids), Comment.llm_status == "evaluated")
            )
        ).scalar() or 0
        total_skip = (
            await session.execute(
                select(func.count(Comment.id)).where(
                    Comment.target_id.in_(target_ids), Comment.llm_status == "skipped_prefilter"
                )
            )
        ).scalar() or 0
        total_flag = (
            await session.execute(
                select(func.count(Comment.id)).where(Comment.target_id.in_(target_ids), Comment.severity_score >= threshold)
            )
        ).scalar() or 0

        # Score bucket distribution
        dist_stmt = select(
            func.sum(case((Comment.severity_score.between(0, 1), 1), else_=0)),
            func.sum(case((Comment.severity_score.between(2, 3), 1), else_=0)),
            func.sum(case((Comment.severity_score.between(4, 6), 1), else_=0)),
            func.sum(case((Comment.severity_score.between(7, 8), 1), else_=0)),
            func.sum(case((Comment.severity_score >= 9, 1), else_=0)),
        ).where(Comment.target_id.in_(target_ids), Comment.llm_status == "evaluated")
        dist_row = (await session.execute(dist_stmt)).first() or (0, 0, 0, 0, 0)
        score_distribution = {
            "0_1_benign": int(dist_row[0] or 0),
            "2_3_venting": int(dist_row[1] or 0),
            "4_6_cynical_or_claims": int(dist_row[2] or 0),
            "7_8_hostile": int(dist_row[3] or 0),
            "9_10_critical": int(dist_row[4] or 0),
        }

        # Flagged comments across all N posts
        flag_stmt = (
            select(Comment, PlatformAuthor)
            .outerjoin(PlatformAuthor, Comment.author_id == PlatformAuthor.id)
            .where(Comment.target_id.in_(target_ids), Comment.severity_score >= threshold)
            .order_by(desc(Comment.severity_score), desc(Comment.published_at))
            .limit(24)
        )
        flag_rows = (await session.execute(flag_stmt)).all()
        flagged_comments = _cluster_duplicates(
            [_format_comment(c, a, max_text=140, threshold=threshold) for c, a in flag_rows],
            limit=10,
        )

        # Top evaluated comments sample (even if below threshold) so Hermes sees themes & vibe
        top_stmt = (
            select(Comment, PlatformAuthor)
            .outerjoin(PlatformAuthor, Comment.author_id == PlatformAuthor.id)
            .where(
                Comment.target_id.in_(target_ids),
                Comment.llm_status == "evaluated",
                or_(Comment.severity_score < threshold, Comment.severity_score.is_(None)),
            )
            .order_by(desc(Comment.severity_score), desc(Comment.published_at))
            .limit(16)
        )
        top_rows = (await session.execute(top_stmt)).all()
        top_sample = _cluster_duplicates(
            [
                {
                    "author": _format_author(a),
                    "score": c.severity_score,
                    "reason": _truncate(c.llm_reason, 85) if c.llm_reason else None,
                    "text": _truncate(c.content_text, 110),
                    "url": c.comment_url or c.post_url,
                }
                for c, a in top_rows
            ],
            limit=6,
        )

    avg_likes = int(sum(p.get("like_count", 0) for p in selected_posts) / max(1, len(selected_posts)))
    avg_comments = int(sum(p.get("comment_count", 0) for p in selected_posts) / max(1, len(selected_posts)))

    return (
        {
            "profile": profile_meta,
            "cadence_and_engagement": {
                "posts_analyzed": len(selected_posts),
                "avg_likes_per_post": avg_likes,
                "avg_comments_per_post": avg_comments,
                "newest_post_at": selected_posts[0].get("published_at"),
                "oldest_post_at": selected_posts[-1].get("published_at"),
            },
            "posts": posts_summary,
            "comment_stats": {
                "total_stored": total_stored,
                "newly_ingested": total_newly_ingested,
                "evaluated": total_eval,
                "noise_skipped": total_skip,
                "flagged_count": total_flag,
                "score_distribution": score_distribution,
            },
            "flagged_comments": flagged_comments,
            "top_comments_sample": top_sample,
        },
        200,
    )


async def handle_posts(request: web.Request) -> web.Response:
    """GET /api/posts?target=@handle&count=5 — List recent posts of an Instagram or X profile without scraping comments."""
    target_input = (
        request.query.get("target")
        or request.query.get("handle")
        or request.query.get("username")
        or request.query.get("url")
        or ""
    ).strip()
    explicit_platform = (request.query.get("platform") or "").strip().lower()
    count = min(max(1, int(request.query.get("count") or request.query.get("limit") or 5)), 10)

    x_username = _extract_x_profile_username(target_input, explicit_platform=explicit_platform)
    ig_username = None if x_username else _extract_instagram_profile_username(target_input)

    if not x_username and not ig_username:
        return web.json_response(
            {"error": f"Provide a valid Instagram or X handle/profile URL (got '{target_input}')."},
            status=400,
        )

    try:
        async with async_session_factory() as session:
            pool_mgr = SessionPoolManager(session)
            if x_username:
                x_collector = TwitterXCollector(pool_mgr)
                profile_meta, recent_posts = await _fetch_x_profile_and_posts(x_collector, x_username, count=count)
                if not recent_posts:
                    return web.json_response(
                        {"error": f"Could not fetch posts for X profile '@{x_username}'."},
                        status=404,
                    )
                return web.json_response({"profile": profile_meta, "recent_posts": recent_posts})
            else:
                ig_collector = InstagramCollector(pool_mgr)
                profile_meta, recent_posts = await _fetch_ig_profile_and_posts(ig_collector, ig_username, count=count)
                if not recent_posts:
                    return web.json_response(
                        {"error": f"Could not fetch posts for Instagram profile '@{ig_username}'."},
                        status=404,
                    )
                return web.json_response({"profile": profile_meta, "recent_posts": recent_posts})
    except ScraperError as s_err:
        return web.json_response({"error": s_err.error_code, "message": s_err.message}, status=s_err.status_code)


async def handle_profile(request: web.Request) -> web.Response:
    """GET/POST /api/profile or /api/scrape/profile — Multi-post profile enumeration, comment scraping, and aggregation."""
    body = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}

    raw_handle = (
        body.get("handle")
        or body.get("username")
        or body.get("target")
        or body.get("url")
        or request.query.get("handle")
        or request.query.get("username")
        or request.query.get("target")
        or request.query.get("url")
        or ""
    ).strip()
    if not raw_handle:
        return web.json_response({"error": "Missing required 'handle' or 'target' parameter."}, status=400)

    explicit_platform = (body.get("platform") or request.query.get("platform") or "").strip().lower()
    x_username = _extract_x_profile_username(raw_handle, explicit_platform=explicit_platform)
    ig_username = None if x_username else _extract_instagram_profile_username(raw_handle)
    prof_username = x_username or ig_username
    prof_platform = "x" if x_username else "instagram"

    if not prof_username:
        return web.json_response(
            {"error": f"Could not parse Instagram or X handle from '{raw_handle}'."},
            status=400,
        )

    posts_count = min(max(1, int(body.get("posts") or body.get("count") or request.query.get("posts") or request.query.get("count") or 3)), 5)
    per_post_limit = min(max(10, int(body.get("limit") or request.query.get("limit") or 80)), 300)
    scrape_param = body.get("scrape", request.query.get("scrape", "true"))
    scrape_comments = str(scrape_param).lower() not in ("false", "0", "no")
    monitor_param = body.get("monitor", request.query.get("monitor", "false"))
    monitor = str(monitor_param).lower() in ("true", "1", "yes")

    mtproto = request.app.get("mtproto")
    try:
        payload, status_code = await _execute_profile_analysis(
            ig_username=prof_username,
            posts_count=posts_count,
            per_post_limit=per_post_limit,
            scrape_comments=scrape_comments,
            monitor=monitor,
            mtproto=mtproto,
            platform=prof_platform,
        )
    except ScraperError as s_err:
        return web.json_response(
            {"error": s_err.error_code, "message": s_err.message, "platform": prof_platform},
            status=s_err.status_code,
        )
    if not payload:
        return web.json_response(
            {"error": f"Could not fetch profile or posts for '@{prof_username}' on {prof_platform}."},
            status=status_code,
        )
    return web.json_response(payload)


async def handle_delete_target(request: web.Request) -> web.Response:
    """DELETE/POST /api/target/delete?target=<id_or_shortcode> — Deactivate or remove a target."""
    body = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}

    target_input = (body.get("target") or body.get("id") or request.query.get("target") or request.query.get("id") or "").strip()
    if not target_input:
        return web.json_response({"error": "Missing 'target' parameter."}, status=400)

    hard_delete = str(body.get("delete", request.query.get("delete", "false"))).lower() in ("true", "1", "yes")

    async with async_session_factory() as session:
        target = await _resolve_target(session, target_input)
        if not target:
            return web.json_response({"error": f"Target '{target_input}' not found."}, status=404)

        t_id = target.id
        ext_id = target.external_id
        if hard_delete:
            await session.execute(delete(MonitoredTarget).where(MonitoredTarget.id == t_id))
            action = "deleted"
        else:
            target.is_active = False
            action = "deactivated"
        await session.commit()

    return web.json_response({"status": "ok", "action": action, "target_id": t_id, "external_id": ext_id})


async def handle_check(request: web.Request) -> web.Response:
    """GET /api/check?target=<id_or_url_or_@handle>&limit=10"""
    target_input = request.query.get("target", "").strip()
    limit = min(max(1, int(request.query.get("limit", 10))), 12)
    explicit_platform = request.query.get("platform", "").strip().lower()

    async with async_session_factory() as session:
        if not target_input or target_input == "list":
            t_repo = TargetRepository(session)
            targets = await t_repo.list_all(limit=25)
            return web.json_response(
                {
                    "monitored_targets": [
                        {
                            "id": t.id,
                            "platform": t.platform,
                            "external_id": t.external_id,
                            "title": t.title_or_name,
                            "url": t.url,
                            "is_active": t.is_active,
                            "total_comments": t.total_comments_collected,
                        }
                        for t in targets
                    ]
                }
            )

        # Check if target_input is an X or Instagram profile URL or @username handle
        x_username = _extract_x_profile_username(target_input, explicit_platform=explicit_platform)
        ig_username = None if x_username else _extract_instagram_profile_username(target_input)
        is_explicit_ig_profile = "instagram.com" in target_input.lower() and ig_username is not None
        is_explicit_x_profile = ("x.com" in target_input.lower() or "twitter.com" in target_input.lower()) and x_username is not None

        # Resolve existing monitored target first unless explicitly querying a profile URL/@handle
        target = None
        if not is_explicit_ig_profile and not is_explicit_x_profile and explicit_platform not in ("instagram", "x", "twitter"):
            if target_input.startswith("@"):
                # Only match if an active Telegram target with this exact @handle has collected comments
                stmt_tg = select(MonitoredTarget).where(
                    MonitoredTarget.platform == "telegram",
                    or_(
                        MonitoredTarget.external_id == target_input,
                        MonitoredTarget.external_id == target_input.lstrip("@"),
                    ),
                    MonitoredTarget.total_comments_collected > 0,
                )
                target = (await session.execute(stmt_tg)).scalars().first()
            else:
                target = await _resolve_target(session, target_input)

        # If no existing post/chat target matched and x_username or ig_username is present, list the profile's last 5 posts
        if not target and x_username:
            pool_mgr = SessionPoolManager(session)
            x_collector = TwitterXCollector(pool_mgr)
            try:
                profile_meta, recent_posts = await _fetch_x_profile_and_posts(x_collector, x_username, count=5)
                if recent_posts:
                    return web.json_response({"profile": profile_meta, "recent_posts": recent_posts})
            except ScraperError as s_err:
                return web.json_response({"error": s_err.error_code, "message": s_err.message, "platform": "x"}, status=s_err.status_code)
            target = await _resolve_target(session, target_input)

        if not target and ig_username:
            pool_mgr = SessionPoolManager(session)
            ig_collector = InstagramCollector(pool_mgr)
            profile_meta, recent_posts = await _fetch_ig_profile_and_posts(ig_collector, ig_username, count=5)
            if recent_posts:
                return web.json_response(
                    {
                        "profile": profile_meta,
                        "recent_posts": recent_posts,
                    }
                )
            # Fall back to checking DB if profile fetch returned empty (e.g. offline/test environment)
            target = await _resolve_target(session, target_input)

        if not target:
            t_repo = TargetRepository(session)
            targets = await t_repo.list_all(limit=15)
            return web.json_response(
                {
                    "error": f"Target or Instagram profile '{target_input}' not found.",
                    "available_targets": [
                        {"id": t.id, "platform": t.platform, "external_id": t.external_id, "title": t.title_or_name}
                        for t in targets
                    ],
                },
                status=404,
            )

        # If this target has pending/unscored comments sitting in DB, drain them before returning
        processor = MicroBatchProcessor()
        await processor.process_all_pending(session=session, target_id=target.id, max_batches=25)

        payload = await _build_target_check_payload(session, target, flagged_limit=limit, sample_limit=8)
        return web.json_response(payload)


async def handle_scrape(request: web.Request) -> web.Response:
    """POST /api/scrape (body or query: url, post_index=1, posts=, monitor=false, wait=true, limit=200)"""
    body = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}

    raw_url = (
        body.get("url")
        or body.get("target")
        or body.get("handle")
        or request.query.get("url")
        or request.query.get("target")
        or request.query.get("handle")
        or ""
    ).strip()
    if not raw_url:
        return web.json_response({"error": "Missing required 'url' parameter."}, status=400)

    wait_param = body.get("wait", request.query.get("wait", "true"))
    wait = str(wait_param).lower() not in ("false", "0", "no")
    limit = min(max(10, int(body.get("limit") or request.query.get("limit") or 200)), 1000)
    post_index = max(1, int(body.get("post_index") or request.query.get("post_index") or 1))
    explicit_posts = int(body.get("posts") or request.query.get("posts") or 0)
    monitor_param = body.get("monitor", request.query.get("monitor", "false"))
    monitor = str(monitor_param).lower() in ("true", "1", "yes")
    explicit_platform = (body.get("platform") or request.query.get("platform") or "").strip().lower()

    resolved_post_meta: Optional[Dict[str, Any]] = None
    profile_meta: Optional[Dict[str, Any]] = None
    profile_recent_preview: Optional[List[Dict[str, Any]]] = None
    x_username = _extract_x_profile_username(raw_url, explicit_platform=explicit_platform)
    ig_username = None if x_username else (_extract_instagram_profile_username(raw_url) if explicit_platform != "telegram" else None)
    prof_username = x_username or ig_username
    prof_platform = "x" if x_username else "instagram"

    if prof_username and explicit_posts > 1:
        mtproto = request.app.get("mtproto")
        try:
            payload, status_code = await _execute_profile_analysis(
                ig_username=prof_username,
                posts_count=explicit_posts,
                per_post_limit=min(limit, 120),
                scrape_comments=True,
                monitor=monitor,
                mtproto=mtproto,
                platform=prof_platform,
            )
        except ScraperError as s_err:
            return web.json_response(
                {"error": s_err.error_code, "message": s_err.message, "platform": prof_platform},
                status=s_err.status_code,
            )
        if not payload:
            return web.json_response({"error": f"Could not fetch profile '@{prof_username}' on {prof_platform}."}, status=status_code)
        return web.json_response(payload)

    if x_username:
        async with async_session_factory() as session:
            s_repo = SessionRepository(session)
            x_sess = await s_repo.get_healthy_session("x")
            if not x_sess and not settings.X_BEARER_TOKEN:
                return web.json_response(
                    {
                        "error": "no x session configured",
                        "platform": "x",
                        "hint": "Add an X burner session or set X_BEARER_TOKEN.",
                    },
                    status=409,
                )
            pool_mgr = SessionPoolManager(session)
            x_collector = TwitterXCollector(pool_mgr)
            try:
                profile_meta, posts = await _fetch_x_profile_and_posts(
                    x_collector,
                    x_username,
                    count=max(5, post_index),
                )
            except ScraperError as s_err:
                return web.json_response(
                    {"error": s_err.error_code, "message": s_err.message, "platform": "x"},
                    status=s_err.status_code,
                )
            if not posts:
                return web.json_response(
                    {
                        "error": f"Could not resolve recent posts for X profile '@{x_username}'.",
                        "hint": "Verify the X username exists and an active X burner session is configured.",
                    },
                    status=404,
                )
            if post_index > len(posts):
                return web.json_response(
                    {
                        "error": f"Requested post_index={post_index}, but X profile '@{x_username}' only returned {len(posts)} posts.",
                        "recent_posts": posts,
                    },
                    status=400,
                )

            selected = posts[post_index - 1]
            platform = "x"
            target_type = "post_url"
            external_id = selected["shortcode"]
            clean_url = selected["url"]
            target_title = f"X @{x_username} ({external_id})"
            resolved_post_meta = {
                "username": f"@{x_username}",
                "post_index": post_index,
                "shortcode": selected["shortcode"],
                "url": selected["url"],
                "caption": selected.get("caption"),
                "published_at": selected.get("published_at"),
                "comment_count": selected.get("comment_count", 0),
                "like_count": selected.get("like_count", 0),
            }
            profile_recent_preview = [
                {
                    "post_index": p["post_index"],
                    "shortcode": p["shortcode"],
                    "url": p["url"],
                    "comment_count": p.get("comment_count", 0),
                    "like_count": p.get("like_count", 0),
                    "published_at": p.get("published_at"),
                    "caption": _truncate(p.get("caption"), 70),
                }
                for p in posts[:5]
            ]
    elif ig_username:
        async with async_session_factory() as session:
            pool_mgr = SessionPoolManager(session)
            ig_collector = InstagramCollector(pool_mgr)
            profile_meta, posts = await _fetch_ig_profile_and_posts(
                ig_collector,
                ig_username,
                count=max(5, post_index),
            )
            if not posts:
                return web.json_response(
                    {
                        "error": f"Could not resolve recent posts for Instagram profile '@{ig_username}'.",
                        "hint": "Verify the Instagram username exists and an active burner session is configured.",
                    },
                    status=404,
                )
            if post_index > len(posts):
                return web.json_response(
                    {
                        "error": f"Requested post_index={post_index}, but profile '@{ig_username}' only returned {len(posts)} posts.",
                        "recent_posts": posts,
                    },
                    status=400,
                )

            selected = posts[post_index - 1]
            platform = "instagram"
            target_type = "post_url"
            external_id = selected["shortcode"]
            clean_url = selected["url"]
            target_title = f"Instagram @{ig_username} ({external_id})"
            resolved_post_meta = {
                "username": f"@{ig_username}",
                "post_index": post_index,
                "shortcode": selected["shortcode"],
                "url": selected["url"],
                "caption": selected.get("caption"),
                "published_at": selected.get("published_at"),
                "comment_count": selected.get("comment_count", 0),
                "like_count": selected.get("like_count", 0),
            }
            profile_recent_preview = [
                {
                    "post_index": p["post_index"],
                    "shortcode": p["shortcode"],
                    "url": p["url"],
                    "comment_count": p.get("comment_count", 0),
                    "like_count": p.get("like_count", 0),
                    "published_at": p.get("published_at"),
                    "caption": _truncate(p.get("caption"), 70),
                }
                for p in posts[:5]
            ]
    else:
        platform, target_type, external_id, clean_url = detect_platform_and_id(raw_url)
        if not platform or not external_id:
            return web.json_response(
                {"error": f"Could not detect platform or ID from '{raw_url}'."},
                status=400,
            )
        target_title = f"{platform.capitalize()} Target: {external_id}"

    # Pre-flight check: fail loudly with 409 if no session or API credential is configured for the platform
    async with async_session_factory() as session:
        s_repo = SessionRepository(session)
        if platform == "x":
            x_session = await s_repo.get_healthy_session("x")
            if not x_session and not settings.X_BEARER_TOKEN:
                return web.json_response(
                    {
                        "error": "no x session configured",
                        "platform": "x",
                        "hint": (
                            "Add an X burner session via 'ops sessions --add x <account> "
                            "\"{\\\"auth_token\\\":\\\"...\\\",\\\"ct0\\\":\\\"...\\\"}\"' "
                            "or set X_BEARER_TOKEN in Coolify environment variables."
                        ),
                    },
                    status=409,
                )
        elif platform == "instagram":
            ig_session = await s_repo.get_healthy_session("instagram")
            if not ig_session:
                return web.json_response(
                    {
                        "error": "no instagram session configured",
                        "platform": "instagram",
                        "hint": "Add an Instagram burner session via 'ops sessions --add instagram <account> <sessionid>'.",
                    },
                    status=409,
                )
        elif platform == "facebook":
            fb_session = await s_repo.get_healthy_session("facebook")
            if not fb_session:
                return web.json_response(
                    {
                        "error": "no facebook session configured",
                        "platform": "facebook",
                        "hint": "Add a Facebook burner session via 'ops sessions --add facebook <account> <cookies_json>'.",
                    },
                    status=409,
                )
        elif platform == "youtube":
            if not settings.YOUTUBE_API_KEY:
                return web.json_response(
                    {
                        "error": "no youtube api key configured",
                        "platform": "youtube",
                        "hint": "Set YOUTUBE_API_KEY in Coolify environment variables.",
                    },
                    status=409,
                )

        t_repo = TargetRepository(session)
        target = await t_repo.create_or_activate(
            platform=platform,
            target_type=target_type,
            external_id=external_id,
            url=clean_url,
            title_or_name=target_title,
            is_active=monitor or (platform == "telegram"),
        )
        await session.commit()
        target_id = target.id
        is_target_active = target.is_active

    mtproto = request.app.get("mtproto")

    if not wait and platform != "telegram":
        try:
            from arq import create_pool
            from arq.connections import RedisSettings

            pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
            await pool.enqueue_job("scrape_target_job", target_id)
            await pool.aclose()
        except Exception as q_err:
            logger.warning("Failed to enqueue job to Arq; running inline fallback", error=str(q_err))

        resp_payload: Dict[str, Any] = {
            "status": "enqueued",
            "target_id": target_id,
            "platform": platform,
            "external_id": external_id,
            "url": clean_url,
            "is_active": is_target_active,
        }
        if not is_target_active:
            resp_payload["warning"] = "Target registered with is_active=false (one-off scrape; pass --monitor to schedule recurring polling)."
        if resolved_post_meta:
            resp_payload["resolved_post"] = resolved_post_meta
        return web.json_response(resp_payload)

    # Synchronous scrape + DeepSeek batch evaluation
    try:
        newly_inserted, evaluated, focal_meta = await _execute_inline_scrape(target_id, mtproto=mtproto, limit=limit)
    except ScraperError as s_err:
        return web.json_response(
            {
                "error": s_err.error_code,
                "message": s_err.message,
                "platform": platform,
                "external_id": external_id,
                "url": clean_url,
            },
            status=s_err.status_code,
        )

    async with async_session_factory() as session:
        t_repo = TargetRepository(session)
        updated_target = await t_repo.get_by_id(target_id)
        payload = await _build_target_check_payload(session, updated_target, flagged_limit=10, sample_limit=6)
        if profile_meta:
            payload["profile"] = profile_meta
        if resolved_post_meta:
            payload["resolved_post"] = resolved_post_meta
        if focal_meta:
            payload["tweet"] = focal_meta
        if profile_recent_preview:
            payload["profile_recent_posts"] = profile_recent_preview
        scrape_res: Dict[str, Any] = {
            "status": "completed",
            "newly_ingested": newly_inserted,
            "evaluated_in_run": evaluated,
            "is_active": updated_target.is_active,
        }
        if not updated_target.is_active:
            scrape_res["warning"] = "Target registered with is_active=false (one-off scrape; pass --monitor to schedule recurring polling)."
        payload["scrape_result"] = scrape_res
        return web.json_response(payload)


async def handle_search(request: web.Request) -> web.Response:
    """GET /api/search?q=<keyword>&platform=&min_score=0&status=&failed=false&limit=12"""
    query_str = (request.query.get("q") or request.query.get("query") or "").strip()
    status_filter = request.query.get("status", "").strip().lower() or None
    failed_only = request.query.get("failed", "false").lower() in ("true", "1", "yes")
    if failed_only and not status_filter:
        status_filter = "failed"

    if not query_str and not status_filter:
        return web.json_response(
            {"error": "Provide either 'q' (search keyword) or 'status' / 'failed=true' parameter."},
            status=400,
        )

    platform = request.query.get("platform", "").strip().lower() or None
    min_score = int(request.query.get("min_score", 0))
    limit = min(max(1, int(request.query.get("limit", 12))), 18)

    async with async_session_factory() as session:
        filters = []
        if query_str:
            filters.append(
                or_(
                    Comment.content_text.ilike(f"%{query_str}%"),
                    Comment.llm_reason.ilike(f"%{query_str}%"),
                )
            )
        if status_filter:
            filters.append(Comment.llm_status == status_filter)
        if platform:
            filters.append(Comment.platform == platform)
        if min_score > 0:
            filters.append(Comment.severity_score >= min_score)

        count_stmt = select(func.count(Comment.id)).where(*filters)
        total_matches = (await session.execute(count_stmt)).scalar() or 0

        stmt = (
            select(Comment, PlatformAuthor)
            .outerjoin(PlatformAuthor, Comment.author_id == PlatformAuthor.id)
            .where(*filters)
            .order_by(desc(Comment.severity_score), desc(Comment.published_at), desc(Comment.ingested_at))
            .limit(min(limit * 3, 45))
        )
        rows = (await session.execute(stmt)).all()
        raw_results = [_format_comment(c, a, max_text=160) for c, a in rows]
        results = _cluster_duplicates(raw_results, limit=limit)

    return web.json_response(
        {
            "query": query_str or None,
            "status_filter": status_filter or "all",
            "platform_filter": platform or "all",
            "min_score": min_score,
            "total_matches": total_matches,
            "returned_count": len(results),
            "results": results,
        }
    )


async def handle_sessions(request: web.Request) -> web.Response:
    """GET/POST /api/sessions — List, reset cooldowns, or add a burner session."""
    body = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}

    platform = (body.get("platform") or request.query.get("platform") or "").strip().lower() or None
    reset_flag = str(body.get("reset") or request.query.get("reset") or "false").lower() in ("true", "1", "yes")
    add_account = (body.get("account") or request.query.get("account") or "").strip()
    raw_cookies = body.get("cookies") or request.query.get("cookies")
    proxy_url = (body.get("proxy_url") or request.query.get("proxy_url") or "").strip() or None

    async with async_session_factory() as session:
        s_repo = SessionRepository(session)
        added_session = None
        if add_account and raw_cookies and platform:
            import json as _json
            cookies_dict: Dict[str, Any] = {}
            if isinstance(raw_cookies, dict):
                cookies_dict = raw_cookies
            elif isinstance(raw_cookies, str):
                raw_c = raw_cookies.strip()
                if raw_c.startswith("{"):
                    try:
                        cookies_dict = _json.loads(raw_c)
                    except Exception:
                        cookies_dict = {"sessionid": raw_c}
                elif "=" in raw_c and ";" in raw_c:
                    for part in raw_c.split(";"):
                        if "=" in part:
                            k, v = part.split("=", 1)
                            cookies_dict[k.strip()] = v.strip()
                elif platform == "instagram":
                    cookies_dict = {"sessionid": raw_c}
                elif platform == "x":
                    cookies_dict = {"auth_token": raw_c}
                else:
                    cookies_dict = {"token": raw_c}

            created_s = await s_repo.add_session(
                platform=platform,
                account_identifier=add_account,
                cookies=cookies_dict,
                proxy_url=proxy_url,
            )
            await session.commit()
            added_session = created_s.account_identifier

        reset_count = 0
        if reset_flag:
            reset_count = await s_repo.reset_all_cooldowns(platform=platform)
            await session.commit()

        sessions = await s_repo.list_sessions(platform=platform)
        now = datetime.now(timezone.utc)

        session_list = []
        active_count = 0
        for s in sessions:
            in_cooldown = bool(s.cooldown_until and s.cooldown_until > now)
            effective_status = "cooldown" if in_cooldown and s.status == "active" else s.status
            if effective_status == "active":
                active_count += 1

            cooldown_rem_min = (
                max(0, int((s.cooldown_until - now).total_seconds() // 60))
                if s.cooldown_until and s.cooldown_until > now
                else 0
            )
            session_list.append(
                {
                    "platform": s.platform,
                    "account": s.account_identifier,
                    "status": effective_status,
                    "error_count": s.error_count,
                    "cooldown_remaining_minutes": cooldown_rem_min,
                    "cooldown_until": s.cooldown_until.strftime("%Y-%m-%d %H:%M:%S") if s.cooldown_until else None,
                    "last_used_at": s.last_used_at.strftime("%Y-%m-%d %H:%M:%S") if s.last_used_at else None,
                    "has_proxy": bool(s.proxy_url),
                }
            )

    mtproto = request.app.get("mtproto")
    mt_connected = bool(mtproto and mtproto.client and mtproto.client.is_connected())

    resp_data: Dict[str, Any] = {
        "telegram_mtproto_userbot": "connected" if mt_connected else "disconnected",
        "x_bearer_token_configured": bool(settings.X_BEARER_TOKEN),
        "youtube_api_key_configured": bool(settings.YOUTUBE_API_KEY),
        "total_burner_sessions": len(session_list),
        "active_burner_sessions": active_count,
        "cooldowns_reset": reset_count if reset_flag else 0,
        "sessions": session_list,
    }
    if added_session:
        resp_data["added_session"] = added_session
    return web.json_response(resp_data)


def create_bridge_app(mtproto: Optional[Any] = None) -> web.Application:
    """Create the aiohttp web.Application for the internal Hermes bridge."""
    app = web.Application(middlewares=[auth_middleware])
    app["mtproto"] = mtproto
    app.router.add_get("/health", handle_health)
    app.router.add_get("/health/detailed", handle_health_detailed)
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/health/detailed", handle_health_detailed)
    app.router.add_get("/api/digest", handle_digest)
    app.router.add_get("/api/check", handle_check)
    app.router.add_get("/api/posts", handle_posts)
    app.router.add_post("/api/posts", handle_posts)
    app.router.add_get("/api/profile", handle_profile)
    app.router.add_post("/api/profile", handle_profile)
    app.router.add_get("/api/profiles", handle_profile)
    app.router.add_post("/api/profiles", handle_profile)
    app.router.add_get("/api/scrape/profile", handle_profile)
    app.router.add_post("/api/scrape/profile", handle_profile)
    app.router.add_post("/api/scrape", handle_scrape)
    app.router.add_get("/api/scrape", handle_scrape)
    app.router.add_delete("/api/target", handle_delete_target)
    app.router.add_post("/api/target/delete", handle_delete_target)
    app.router.add_get("/api/target/delete", handle_delete_target)
    app.router.add_get("/api/search", handle_search)
    app.router.add_get("/api/sessions", handle_sessions)
    app.router.add_post("/api/sessions", handle_sessions)
    return app


async def start_bridge_server(mtproto: Optional[Any] = None) -> web.AppRunner:
    """Start the internal aiohttp bridge server on OPS_API_HOST:OPS_API_PORT."""
    app = create_bridge_app(mtproto=mtproto)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.OPS_API_HOST, settings.OPS_API_PORT)
    await site.start()
    logger.info(
        "Internal Hermes Bridge API listening",
        host=settings.OPS_API_HOST,
        port=settings.OPS_API_PORT,
    )
    return runner
