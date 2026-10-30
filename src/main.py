"""Main entrypoint running the Bot Controller, MTProto Userbot, and Alert Dispatcher."""

import asyncio
import signal
import structlog
from src.config import settings
from src.db.base import engine, Base
from src.collectors.telegram_mtproto import TelegramMTProtoCollector
from src.bot.main import start_bot
from src.api.bridge import start_bridge_server

logger = structlog.get_logger(__name__)


async def init_models():
    """Ensure database tables exist and clean up legacy author/flag records on startup."""
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        try:
            await conn.execute(
                text(
                    "UPDATE platform_authors SET display_name = REPLACE(display_name, ' None', '') "
                    "WHERE display_name LIKE '% None'"
                )
            )
            await conn.execute(
                text(
                    "UPDATE comments SET is_severely_negative = (severity_score >= :thresh) "
                    "WHERE severity_score IS NOT NULL"
                ),
                {"thresh": settings.LLM_ALERT_SEVERITY_THRESHOLD},
            )
            await conn.execute(
                text(
                    "UPDATE comments SET severity_score = 0, is_severely_negative = FALSE, is_on_topic = FALSE, "
                    "llm_reason = 'Commercial loan or broker spam offering fast cash.' "
                    "WHERE LOWER(content_text) LIKE '%kimga pul kere%'"
                )
            )
        except Exception as clean_err:
            logger.debug("Optional startup SQL cleanup skipped", error=str(clean_err))
    logger.info("Database tables initialized and calibrated successfully.")


async def main():
    logger.info("Initializing Operatives Monitoring System...")
    await init_models()

    # Initialize Telegram MTProto Collector
    mtproto = TelegramMTProtoCollector()
    try:
        await mtproto.start()
    except Exception as e:
        logger.warning("Could not start Telegram MTProto userbot", error=str(e))

    stop_event = asyncio.Event()

    def handle_exit():
        logger.info("Shutdown signal received...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_exit)
        except NotImplementedError:
            # Signal handlers not implemented on Windows
            pass

    # Start Internal Hermes Bridge API Server (default port 8080)
    bridge_runner = await start_bridge_server(mtproto=mtproto)

    # Start Aiogram 3 Bot in task
    bot_task = asyncio.create_task(start_bot(mtproto_collector=mtproto))
    stop_wait_task = asyncio.create_task(stop_event.wait())

    try:
        if settings.TELEGRAM_BOT_TOKEN:
            await asyncio.wait([bot_task, stop_wait_task], return_when=asyncio.FIRST_COMPLETED)
        else:
            await stop_wait_task
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        logger.info("Shutting down Bridge API, MTProto client, and resources...")
        bot_task.cancel()
        stop_wait_task.cancel()
        await bridge_runner.cleanup()
        await mtproto.stop()
        await engine.dispose()
        logger.info("Operatives system stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
