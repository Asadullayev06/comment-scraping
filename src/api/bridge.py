"""Lightweight internal HTTP bridge exposing token-safe (< 3 KB) endpoints for Hermes CLI (ops)."""

from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple
from aiohttp import web
from sqlalchemy import select, func, desc, or_
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
) -> Tuple[int, int]:
    """Run an on-demand scrape and DeepSeek micro-batch evaluation synchronously.

    Returns:
        Tuple of (newly_inserted_count, evaluated_count)
    """
    async with async_session_factory() as session:
        t_repo = TargetRepository(session)
        target = await t_repo.get_by_id(target_id)
        if not target:
            return 0, 0

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
            return 0, 0

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

            return newly_inserted, total_evaluated
        except Exception as e:
            logger.error("Inline scrape failed", target_id=target_id, error=str(e))
            return newly_inserted, 0


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


async def handle_check(request: web.Request) -> web.Response:
    """GET /api/check?target=<id_or_url>&limit=10"""
    target_input = request.query.get("target", "").strip()
    limit = min(max(1, int(request.query.get("limit", 10))), 12)

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

        target = await _resolve_target(session, target_input)
        if not target:
            t_repo = TargetRepository(session)
            targets = await t_repo.list_all(limit=15)
            return web.json_response(
                {
                    "error": f"Target '{target_input}' not found.",
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
    """POST /api/scrape (body or query: url, wait=true, limit=200)"""
    body = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}

    raw_url = (body.get("url") or request.query.get("url") or "").strip()
    if not raw_url:
        return web.json_response({"error": "Missing required 'url' parameter."}, status=400)

    wait_param = body.get("wait", request.query.get("wait", "true"))
    wait = str(wait_param).lower() not in ("false", "0", "no")
    limit = min(max(10, int(body.get("limit") or request.query.get("limit") or 200)), 1000)

    platform, target_type, external_id, clean_url = detect_platform_and_id(raw_url)
    if not platform or not external_id:
        return web.json_response(
            {"error": f"Could not detect platform or ID from '{raw_url}'."},
            status=400,
        )

    async with async_session_factory() as session:
        t_repo = TargetRepository(session)
        target = await t_repo.create_or_activate(
            platform=platform,
            target_type=target_type,
            external_id=external_id,
            url=clean_url,
            title_or_name=f"{platform.capitalize()} Target: {external_id}",
        )
        await session.commit()
        target_id = target.id

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

        return web.json_response(
            {
                "status": "enqueued",
                "target_id": target_id,
                "platform": platform,
                "external_id": external_id,
                "url": clean_url,
            }
        )

    # Synchronous scrape + DeepSeek batch evaluation
    newly_inserted, evaluated = await _execute_inline_scrape(target_id, mtproto=mtproto, limit=limit)

    async with async_session_factory() as session:
        t_repo = TargetRepository(session)
        updated_target = await t_repo.get_by_id(target_id)
        payload = await _build_target_check_payload(session, updated_target, flagged_limit=10, sample_limit=6)
        payload["scrape_result"] = {
            "status": "completed",
            "newly_ingested": newly_inserted,
            "evaluated_in_run": evaluated,
        }
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
    """GET /api/sessions?platform=&reset=false"""
    platform = request.query.get("platform", "").strip().lower() or None
    reset_flag = request.query.get("reset", "false").lower() in ("true", "1", "yes")

    async with async_session_factory() as session:
        s_repo = SessionRepository(session)
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

    return web.json_response(
        {
            "telegram_mtproto_userbot": "connected" if mt_connected else "disconnected",
            "total_burner_sessions": len(session_list),
            "active_burner_sessions": active_count,
            "cooldowns_reset": reset_count if reset_flag else 0,
            "sessions": session_list,
        }
    )


def create_bridge_app(mtproto: Optional[Any] = None) -> web.Application:
    """Create the aiohttp web.Application for the internal Hermes bridge."""
    app = web.Application(middlewares=[auth_middleware])
    app["mtproto"] = mtproto
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/digest", handle_digest)
    app.router.add_get("/api/check", handle_check)
    app.router.add_post("/api/scrape", handle_scrape)
    app.router.add_get("/api/scrape", handle_scrape)
    app.router.add_get("/api/search", handle_search)
    app.router.add_get("/api/sessions", handle_sessions)
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
