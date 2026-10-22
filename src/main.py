"""Main entrypoint running the Bot Controller, MTProto Userbot, and Alert Dispatcher."""

import asyncio
import signal
import structlog
from src.config import settings
from src.db.base import engine, Base
from src.collectors.telegram_mtproto import TelegramMTProtoCollector
from src.bot.main import start_bot

logger = structlog.get_logger(__name__)


async def init_models():
    """Ensure database tables exist on startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables initialized successfully.")


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

    # Start Aiogram 3 Bot in task
    bot_task = asyncio.create_task(start_bot(mtproto_collector=mtproto))

    try:
        # Wait until bot finishes or stop signal
        await bot_task
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        logger.info("Shutting down MTProto client and resources...")
        await mtproto.stop()
        await engine.dispose()
        logger.info("Operatives system stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
