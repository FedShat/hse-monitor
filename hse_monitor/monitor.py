import asyncio
import logging
import time
from datetime import UTC, datetime

from .config import Config
from .lms import LMSClient
from .models import AuthExpired, LMSError, RateLimited, Slot
from .store import Store

log = logging.getLogger(__name__)

AUTH_EXPIRED_MESSAGE = (
    "🔐 SmartLMS: cookie истекла или недействительна.\n"
    "Без действующей сессии проверка слотов и запись недоступны.\n\n"
    "1. Войдите в edu.hse.ru в браузере.\n"
    "2. Обновите LMS_COOKIE_HEADER в локальной .env. "
    "На Mac получить cookie можно командой: python3 scripts/import_chrome_cookie.py\n"
    "3. Для сервера скопируйте файлы через ./scripts/sync.sh user@server:/path/to/hse-monitor/ "
    "и выполните на сервере: docker compose up -d --force-recreate\n\n"
    "При локальном запуске обновлённая .env перечитается автоматически. "
    "После успешной проверки бот сообщит о восстановлении."
)


def table_changes(previous: list[dict], current: list[dict]) -> list[str]:
    old, new = ({s["id"]: s for s in data} for data in (previous, current))
    result = []
    for slot_id in sorted(new.keys() - old.keys()):
        result.append("+ " + Slot(**new[slot_id]).label())
    for slot_id in sorted(old.keys() - new.keys()):
        result.append("− " + Slot(**old[slot_id]).label())
    labels = {
        "name": "название",
        "starts": "начало",
        "capacity": "вместимость",
        "participants": "участников",
        "duration": "минут",
        "comment": "комментарий",
    }
    for slot_id in sorted(new.keys() & old.keys()):
        changes = [
            f"{label}: {str(old[slot_id][key])[:160]} → {str(new[slot_id][key])[:160]}"
            for key, label in labels.items()
            if old[slot_id][key] != new[slot_id][key]
        ]
        if changes:
            result.append(f"~ #{slot_id}: " + "; ".join(changes))
    return result


class Monitor:
    def __init__(self, config: Config, lms: LMSClient, store: Store):
        self.config, self.lms, self.store = config, lms, store
        self.lock = asyncio.Lock()
        self.wakeup = asyncio.Event()
        self.next_allowed_at = 0.0
        self.failures = 0

    @property
    def autobook(self) -> bool:
        return self.store.get("autobook", self.config.autobook_enabled)

    async def cycle(self) -> None:
        async with self.lock:
            slots = await self.lms.slots()
            candidates = sorted(
                (s for s in slots if s.free and s.matches(self.config)),
                key=lambda s: (
                    self.config.slot_ids.index(s.id) if self.config.slot_ids else 0,
                    s.starts,
                    int(s.id),
                ),
            )
            # JSON omits action buttons. Fetch live forms only when needed; do not infer
            # enrollment eligibility from participant counts alone.
            if candidates or self.store.get("pending"):
                page = await self.lms.refresh_forms()
            else:
                page = self.lms.page
            if page is None:
                raise LMSError("Не загружена форма SmartLMS")
            enrolled = [f.slot_id for f in page.forms.values() if f.enrolled]
            if enrolled and not self.store.get("booked"):
                self.store.finish_booking(
                    enrolled[0],
                    f"✅ Запись подтверждена: слот #{enrolled[0]}. Автозапись выключена.",
                )
            available = [
                s
                for s in candidates
                if s.id in page.forms and page.forms[s.id].enabled and not page.forms[s.id].enrolled
            ]

            # Submission precedes Telegram delivery, which runs in a separate worker.
            if (
                available
                and self.autobook
                and not self.store.get("booked")
                and not self.store.get("pending")
                and not enrolled
            ):
                slot = available[0]
                if self.config.dry_run:
                    if slot.id not in self.store.get("available", []):
                        self.store.notify(
                            "🧪 Пробная автозапись (POST не отправлен):\n" + slot.label()
                        )
                else:
                    self.store.set("pending", {"id": slot.id, "at": datetime.now(UTC).isoformat()})
                    try:
                        await self.lms.register(page.forms[slot.id])
                        confirmed = await self.lms.refresh_forms()
                        own = confirmed.forms.get(slot.id)
                        if own and own.enrolled:
                            self.store.finish_booking(
                                slot.id, "✅ Успешная запись:\n" + slot.label()
                            )
                        else:
                            self.booking_uncertain(slot)
                    except LMSError:
                        self.booking_uncertain(slot)
                        raise

            previous = self.store.get("snapshot")
            previous_available = set(self.store.get("available", []))
            events = []
            opened = [s for s in available if s.id not in previous_available]
            if opened and (previous is not None or self.config.notify_initial):
                events.append("🟢 Можно записаться:\n" + "\n".join(s.label() for s in opened))
            closed = previous_available - {s.id for s in available}
            if closed:
                events.append(
                    "🔴 Запись больше недоступна: " + ", ".join(f"#{i}" for i in sorted(closed))
                )
            snapshot = [s.to_dict() for s in slots]
            if previous is not None and self.config.notify_changes:
                changes = table_changes(previous, snapshot)
                if changes:
                    events.append("📋 Таблица слотов изменилась:\n" + "\n".join(changes))
            self.store.commit_snapshot(snapshot, [s.id for s in available], events)
            self.store.set("last_success", datetime.now(UTC).isoformat())
            self.store.clear_error()

    def booking_uncertain(self, slot: Slot) -> None:
        self.store.set("autobook", False)
        self.store.notify(
            "⚠️ Результат записи не подтверждён:\n"
            + slot.label()
            + "\nАвтозапись остановлена. Повторный POST запрещён до проверки. "
            "Проверьте LMS; если записи нет, используйте /clear_pending, затем /autobook on."
        )

    async def run(self) -> None:
        while True:
            self.wakeup.clear()
            delay = self.config.poll_interval
            remaining = self.next_allowed_at - time.monotonic()
            if remaining > 0:
                delay = remaining
            elif not self.store.get("paused", False):
                start = time.monotonic()
                try:
                    await self.cycle()
                    self.failures = 0
                    # POLL_INTERVAL is measured between request cycles, not after the response.
                    delay = max(0.1, self.config.poll_interval - (time.monotonic() - start))
                except LMSError as error:
                    self.failures += 1
                    delay = min(
                        self.config.max_backoff,
                        self.config.poll_interval * 2 ** min(self.failures, 10),
                    )
                    if isinstance(error, RateLimited):
                        delay = max(delay, error.retry_after)
                    self.next_allowed_at = time.monotonic() + delay
                    self.report_error(error)
                    log.warning(
                        "LMS check failed (%s); retry in %.1fs", type(error).__name__, delay
                    )
            try:
                async with asyncio.timeout(delay):
                    await self.wakeup.wait()
            except TimeoutError:
                pass

    def report_error(self, error: LMSError) -> None:
        auth_expired = isinstance(error, AuthExpired)
        notification = AUTH_EXPIRED_MESSAGE if auth_expired else "⚠️ Мониторинг: " + str(error)
        self.store.record_error(str(error), notification, auth_expired=auth_expired)

    def status(self) -> str:
        snapshot = self.store.get("snapshot", [])
        return (
            f"Мониторинг: {'пауза' if self.store.get('paused') else 'включён'}\n"
            f"Автозапись: {'вкл' if self.autobook else 'выкл'}; "
            f"режим: {'пробный' if self.config.dry_run else 'реальная запись'}\n"
            f"Интервал: {self.config.poll_interval:g} с; зона времени: {self.config.timezone}\n"
            f"Слотов: {len(snapshot)}; "
            f"доступно по фильтрам: {len(self.store.get('available', []))}\n"
            f"Последняя проверка (UTC): {self.store.get('last_success', 'ещё не было')}\n"
            f"Подтверждённая запись: {self.store.get('booked') or 'нет'}\n"
            f"Неподтверждённый запрос: {(self.store.get('pending') or {}).get('id', 'нет')}\n"
            f"Ошибка: {self.store.get('error') or 'нет'}"
        )
