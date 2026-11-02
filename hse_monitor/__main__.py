import argparse
import asyncio
import logging
import os
import sys

from aiogram import Bot

from .config import Config, load_config
from .lms import LMSClient
from .models import LMSError
from .monitor import Monitor
from .store import Store
from .telegram import build_dispatcher, deliver


async def inspect(config: Config) -> None:
    async with LMSClient(config) as lms:
        slots = await lms.slots()
        page = await lms.refresh_forms()
        possible = [s for s in slots if s.free and s.matches(config)]
        available = [
            s
            for s in possible
            if s.id in page.forms and page.forms[s.id].enabled and not page.forms[s.id].enrolled
        ]
        print(
            f"Доступ OK. Слотов: {len(slots)}. Подходящих с местами: {len(possible)}. "
            f"С активной кнопкой записи: {len(available)}."
        )
        for slot in available:
            print(slot.label())
        own = [f.slot_id for f in page.forms.values() if f.enrolled]
        print("Текущая запись:", ", ".join(own) or "не обнаружена")
        print("Только чтение: POST и Telegram не использовались.")


async def run(config: Config) -> None:
    store = Store(config.state_path, config.origin + ":" + config.cmid)
    try:
        async with LMSClient(config) as lms, Bot(config.bot_token) as bot:
            monitor = Monitor(config, lms, store)
            dispatcher = build_dispatcher(config, monitor)
            store.notify("🚀 Монитор SmartLMS запущен.\n" + monitor.status())
            tasks = [
                asyncio.create_task(monitor.run(), name="monitor"),
                asyncio.create_task(deliver(bot, store, config), name="notifications"),
                asyncio.create_task(
                    dispatcher.start_polling(bot, close_bot_session=False), name="telegram"
                ),
            ]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        store.close()


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="SmartLMS exam slot monitor")
    parser.add_argument("command", nargs="?", choices=["run", "inspect"], default="run")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    try:
        config = load_config(args.env_file)
        config.validate(telegram=args.command == "run")
        asyncio.run(inspect(config) if args.command == "inspect" else run(config))
    except (ValueError, LMSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        pass
    except Exception as error:
        # Avoid dumping library exception payloads that can contain authentication data.
        logging.error("Fatal error: %s (credentials omitted)", type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
