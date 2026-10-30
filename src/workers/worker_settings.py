"""Arq asynchronous job queue worker definitions and periodic cron tasks."""

import asyncio
from typing import Any, Dict
import structlog
from arq import cron
from arq.connections import RedisSettings
import redis.asyncio as aioredis
from src.config import settings
from src.db.base import async_session_factory
from src.db.repositories.target_repo import TargetRepository
from src.db.repositories.author_repo import AuthorRepository
from src.db.repositories.comment_repo import CommentRepository
from src.collectors.pool_manager import SessionPoolManager
from src.collectors.youtube import YouTubeCollector
from src.collectors.twitter_x import TwitterXCollector
from src.collectors.instagram import InstagramCollector
from src.collectors.facebook import FacebookCollector
from src.llm.processor import MicroBatchProcessor

logger = structlog.get_logger(__name__)


async def scrape_target_job(ctx: Dict[str, Any], target_id: str, is_backfill: bool = False) -> int:
    """Execute comment scraping for an external post/target with incremental deduplication.

    For routine monitoring (is_backfill=False), starts at cursor=None (top/newest)
    and stops early once MAX_CONSECUTIVE_DUPLICATES (8) existing comments are reached.
    """
    async with async_session_factory() as session:
        t_repo = TargetRepository(session)
        target = await t_repo.get_by_id(target_id)
        if not target or not target.is_active:
            return 0

        pool_mgr = SessionPoolManager(session)
        collector = None

        if target.platform == "youtube":
            collector = YouTubeCollector()
        elif target.platform == "x":
            collector = TwitterXCollector(pool_mgr)
        elif target.platform == "instagram":
            collector = InstagramCollector(pool_mgr)
        elif target.platform == "facebook":
            collector = FacebookCollector(pool_mgr)

        if not collector:
            return 0

        author_repo = AuthorRepository(session)
        comment_repo = CommentRepository(session)
        newly_inserted = 0
        consecutive_duplicates = 0
        MAX_CONSECUTIVE_DUPLICATES = 8

        # If backfilling, use target.last_cursor; otherwise start from top (None)
        start_cursor = target.last_cursor if is_backfill else None

        logger.info(
            "Scrape job starting",
            target_id=target_id,
            platform=target.platform,
            external_id=target.external_id,
            is_backfill=is_backfill,
            start_cursor=bool(start_cursor),
        )

        try:
            async for norm_comment in collector.fetch_comments(target, cursor=start_cursor, limit=1000):
                # Upsert author
                author = await author_repo.upsert_author(
                    platform=norm_comment.platform,
                    external_user_id=norm_comment.author_external_id,
                    username=norm_comment.author_username,
                    display_name=norm_comment.author_display_name,
                    profile_url=norm_comment.author_profile_url,
                )

                # Ingest comment (returns Comment if inserted, None if duplicate)
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
                    # Stop early if we hit consecutive existing comments on a monitoring run
                    if not is_backfill and consecutive_duplicates >= MAX_CONSECUTIVE_DUPLICATES:
                        logger.info(
                            "Early stopping reached: existing comment history encountered",
                            target_id=target_id,
                            platform=target.platform,
                            consecutive_duplicates=consecutive_duplicates,
                        )
                        break

            # Update target statistics without corrupting the top-of-feed monitoring state
            await t_repo.update_cursor(target.id, increment_comments=newly_inserted)
            await session.commit()

            logger.info(
                "Scrape job completed",
                target_id=target_id,
                platform=target.platform,
                newly_inserted=newly_inserted,
            )

            # Trigger micro-batch processing until all newly inserted comments are evaluated
            if newly_inserted > 0:
                processor = MicroBatchProcessor()
                redis_client = ctx.get("redis")
                for _ in range(15):
                    eval_count, _ = await processor.process_batch(session, redis_client=redis_client)
                    if eval_count == 0:
                        break

            return newly_inserted
        except Exception as e:
            logger.error("Scrape job failed", target_id=target_id, platform=target.platform, error=str(e))
            return 0


async def process_llm_queue_cron(ctx: Dict[str, Any]) -> None:
    """Periodic task to flush pending comments through DeepSeek micro-batch processor."""
    async with async_session_factory() as session:
        processor = MicroBatchProcessor()
        redis_client = ctx.get("redis")
        count, _ = await processor.process_batch(session, redis_client=redis_client)
        if count > 0:
            logger.info("Processed LLM cron micro-batch", count=count)


async def poll_active_targets_cron(ctx: Dict[str, Any]) -> None:
    """Poll active non-Telegram targets and enqueue scrape tasks."""
    async with async_session_factory() as session:
        t_repo = TargetRepository(session)
        targets = await t_repo.get_active_targets()
        redis_client = ctx.get("redis")

        for t in targets:
            if t.platform != "telegram":
                # Enqueue job to Arq
                await ctx["arq_redis"].enqueue_job("scrape_target_job", t.id)


async def startup(ctx: Dict[str, Any]) -> None:
    """Initialize Redis connection on worker startup."""
    try:
        ctx["redis"] = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        logger.info("Worker started with Redis connection.")
    except Exception as e:
        logger.warning("Worker could not connect to Redis", error=str(e))


async def shutdown(ctx: Dict[str, Any]) -> None:
    """Close Redis on shutdown."""
    if "redis" in ctx and ctx["redis"]:
        await ctx["redis"].aclose()


class WorkerSettings:
    functions = [scrape_target_job, process_llm_queue_cron, poll_active_targets_cron]
    on_startup = startup
    on_shutdown = shutdown
    cron_jobs = [
        # Run LLM buffer flush every 5 seconds
        cron(process_llm_queue_cron, second={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        # Run external targets check every 5 minutes
        cron(poll_active_targets_cron, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
    ]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
