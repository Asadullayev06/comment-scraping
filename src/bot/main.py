"""Aiogram 3 Bot entrypoint and background alert delivery runner."""

import asyncio
import json
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
import structlog
import redis.asyncio as aioredis
from src.config import settings
from src.bot.middlewares import AdminAuthMiddleware
from src.bot.alert_dispatcher import AlertDispatcher
from src.bot.handlers import targets, lookup, settings as bot_settings
from src.schemas.alert import AlertPayload

logger = structlog.get_logger(__name__)


async def background_alert_worker(bot: Bot, dispatcher: AlertDispatcher):
    """Continuously poll Redis alert queue and deliver formatted alerts to administrators."""
    try:
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception as e:
        logger.warning("Redis connection failed for alert worker; alert queue listening disabled", error=str(e))
        return

    logger.info("Background Alert Dispatcher started listening on Redis 'alert_queue'...")
    while True:
        try:
            # BLPOP blocks with 2-second timeout so it's non-busy and responsive to cancellation
            item = await r.blpop("alert_queue", timeout=2)
            if item:
                _, payload_json = item
                data = json.loads(payload_json)
                alert = AlertPayload.model_validate(data)
                await dispatcher.send_alert(alert)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in background alert queue worker", error=str(e))
            await asyncio.sleep(2.0)


async def start_bot(mtproto_collector=None):
    """Initialize and run the Aiogram 3 bot controller."""
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN is not set. Bot controller cannot start.")
        return

    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()

    # Register admin authorization middleware
    dp.message.middleware(AdminAuthMiddleware())
    dp.callback_query.middleware(AdminAuthMiddleware())

    # Register routers
    dp.include_router(targets.router)
    dp.include_router(lookup.router)
    dp.include_router(bot_settings.router)

    if mtproto_collector:
        lookup.set_mtproto_collector_instance(mtproto_collector)

    dispatcher = AlertDispatcher(bot=bot)

    # Launch background alert delivery worker
    alert_task = asyncio.create_task(background_alert_worker(bot, dispatcher))

    # Register bot menu commands in Telegram UI
    try:
        from aiogram.types import BotCommand
        await bot.set_my_commands([
            BotCommand(command="start", description="Dashboard & Main Menu"),
            BotCommand(command="targets", description="Monitored Targets"),
            BotCommand(command="add_target", description="Add Monitored URL / Handle"),
            BotCommand(command="lookup", description="Search User in Mutual Groups"),
            BotCommand(command="stats", description="Ingestion & AI Metrics"),
            BotCommand(command="threshold", description="Set Alert Severity Threshold"),
            BotCommand(command="help", description="Help & Documentation"),
        ])
    except Exception as cmd_err:
        logger.warning("Could not register bot commands menu", error=str(cmd_err))

    logger.info("Starting Aiogram 3 Bot polling...")
    try:
        await dp.start_polling(bot)
    finally:
        alert_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(start_bot())
