import argparse
import asyncio
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from poem_bot.config import DEFAULT_TIMEZONE, get_bot_token
from poem_bot.db import Database
from poem_bot.handlers import router as main_router
from poem_bot.reminders import reminder_loop
from poem_bot.training_handlers import router as training_router

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def start_health_server() -> web.AppRunner:
    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def index(_: web.Request) -> web.Response:
        return web.Response(text="Poem Memory Bot is running")

    application = web.Application()
    application.router.add_get("/", index)
    application.router.add_get("/health", health)
    runner = web.AppRunner(application)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("Health server listening on port %s", port)
    return runner


async def run() -> None:
    token = get_bot_token()
    db = Database()
    await asyncio.to_thread(db.init)
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=None))
    runner = await start_health_server()
    reminder_task = None
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Приветствие и инструкция"),
            BotCommand(command="learn", description="Начать учить стих"),
            BotCommand(command="train", description="Тренировка с пропусками"),
            BotCommand(command="plan", description="План и расписание"),
            BotCommand(command="progress", description="Статистика тестов"),
            BotCommand(command="cancel", description="Отменить текущий стих"),
        ])
        await bot.delete_webhook(drop_pending_updates=False)
        dispatcher = Dispatcher()
        dispatcher.include_router(main_router)
        dispatcher.include_router(training_router)
        reminder_task = asyncio.create_task(reminder_loop(bot, db))
        logger.info("Bot started; timezone=%s", DEFAULT_TIMEZONE)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        if reminder_task:
            reminder_task.cancel()
            try:
                await reminder_task
            except asyncio.CancelledError:
                pass
        await runner.cleanup()
        await bot.session.close()


def local_check() -> None:
    get_bot_token()
    db = Database()
    db.init()
    db.get_user(1)
    print(f"Локальная проверка пройдена. SQLite готова: {db.path}")
    print(f"Часовой пояс: {DEFAULT_TIMEZONE}. BOT_TOKEN задан. Сеть Telegram в этом режиме не проверяется.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Проверить конфигурацию и SQLite без подключения к Telegram")
    args = parser.parse_args()
    if args.check:
        local_check()
    else:
        try:
            asyncio.run(run())
        except (KeyboardInterrupt, SystemExit):
            logger.info("Bot stopped")
