import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

from .config import Config
from .models import Slot
from .monitor import Monitor
from .store import Store

log = logging.getLogger(__name__)


def build_dispatcher(config: Config, monitor: Monitor) -> Dispatcher:
    router = Router()
    router.message.filter(
        F.chat.type == "private", F.chat.id == config.chat_id, F.from_user.id == config.chat_id
    )

    @router.message(Command("start", "help"))
    async def help_command(message: Message):
        await message.answer(
            "SmartLMS monitor\n/status — состояние\n/slots — доступные слоты\n"
            "/check — проверить сейчас\n/pause — пауза\n/resume — продолжить\n"
            "/autobook on|off — автозапись\n/clear_pending — снять блокировку "
            "неподтверждённой записи после ручной проверки LMS\n"
            "/reload_auth — перечитать cookie из .env\n"
            "Реальная запись требует AUTOBOOK_DRY_RUN=false в .env и перезапуска."
        )

    @router.message(Command("status"))
    async def status(message: Message):
        await message.answer(monitor.status())

    @router.message(Command("slots"))
    async def slots(message: Message):
        ids = set(monitor.store.get("available", []))
        available = [Slot(**s) for s in monitor.store.get("snapshot", []) if s["id"] in ids]
        text = "\n".join(s.label() for s in available[:15]) or "Доступных слотов пока нет."
        if len(available) > 15:
            text += f"\nВсего: {len(available)}. Остальные — на странице LMS."
        await message.answer(text[:3500])

    @router.message(Command("check"))
    async def check(message: Message):
        monitor.wakeup.set()
        await message.answer("Проверка запрошена. Пауза и задержка после ошибок учитываются.")

    @router.message(Command("pause", "resume"))
    async def pause(message: Message):
        paused = (message.text or "").split()[0].split("@")[0] == "/pause"
        async with monitor.lock:
            monitor.store.set("paused", paused)
        monitor.wakeup.set()
        await message.answer("Мониторинг на паузе." if paused else "Мониторинг продолжен.")

    @router.message(Command("autobook"))
    async def autobook(message: Message):
        argument = (message.text or "").split()[1:]
        if argument not in [["on"], ["off"]]:
            await message.answer("Используйте /autobook on или /autobook off.")
            return
        enabled = argument == ["on"]
        async with monitor.lock:
            if enabled and (monitor.store.get("pending") or monitor.store.get("booked")):
                await message.answer("Уже есть запись или неподтверждённый запрос. См. /status.")
                return
            monitor.store.set("autobook", enabled)
        monitor.wakeup.set()
        await message.answer(monitor.status())

    @router.message(Command("clear_pending"))
    async def clear_pending(message: Message):
        async with monitor.lock:
            monitor.store.set("pending", None)
            monitor.store.set("autobook", False)
        await message.answer("Блокировка снята. Автозапись остаётся выключенной.")

    @router.message(Command("reload_auth"))
    async def reload_auth(message: Message):
        # The next request reloads .env by mtime; never accept cookies in bot messages.
        monitor.wakeup.set()
        await message.answer("Cookie будет перечитана из .env при следующей проверке.")

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher


async def deliver(bot: Bot, store: Store, config: Config) -> None:
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть слоты SmartLMS", url=config.lms_url)]]
    )
    while True:
        item = store.next_message()
        if not item:
            await asyncio.sleep(0.3)
            continue
        message_id, text = item
        try:
            await bot.send_message(
                config.chat_id,
                text,
                reply_markup=keyboard,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            store.acknowledge(message_id)
            await asyncio.sleep(1.1)
        except TelegramRetryAfter as error:
            await asyncio.sleep(error.retry_after + 1)
        except (TelegramNetworkError, TelegramServerError):
            log.warning("Telegram unavailable; notification remains in outbox")
            await asyncio.sleep(10)
        except (TelegramForbiddenError, TelegramBadRequest):
            log.warning("Telegram delivery rejected; check chat ID and send /start to bot")
            await asyncio.sleep(60)
