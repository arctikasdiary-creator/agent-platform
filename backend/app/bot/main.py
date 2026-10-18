from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import redis.asyncio as aioredis
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from app.bot import notifications
from app.bot.handlers import AccessMiddleware, router
from app.core.config import get_settings
from app.db.session import dispose_engine, init_models

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand(command="start", description="Начало работы"),
    BotCommand(command="new_task", description="Создать задачу"),
    BotCommand(command="tasks", description="Последние задачи"),
    BotCommand(command="task", description="Детали задачи по id"),
    BotCommand(command="delete", description="Удалить задачу"),
    BotCommand(command="cancel", description="Отменить ввод"),
]


async def _run() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not configured")

    await init_models()

    session_kwargs: dict[str, object] = {}
    if settings.telegram_proxy:
        session_kwargs["proxy"] = settings.telegram_proxy
    if settings.telegram_api_base_url:
        session_kwargs["api"] = TelegramAPIServer.from_base(settings.telegram_api_base_url)

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=AiohttpSession(**session_kwargs) if session_kwargs else None,
    )
    dispatcher = Dispatcher()
    access = AccessMiddleware(set(settings.telegram_allowed_user_ids))
    dispatcher.message.middleware(access)
    dispatcher.callback_query.middleware(access)
    dispatcher.include_router(router)

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    listener = asyncio.create_task(notifications.listen(bot, redis_client), name="notifications")

    try:
        await bot.set_my_commands(BOT_COMMANDS)
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            polling_timeout=settings.telegram_poll_timeout_seconds,
        )
    finally:
        listener.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await listener
        await redis_client.aclose()
        await bot.session.close()
        await dispose_engine()
        logger.info("bot stopped")


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
