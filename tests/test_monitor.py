import asyncio
import time
from dataclasses import replace

import pytest
from conftest import form

from hse_monitor.models import AuthExpired, LMSError, Page, RateLimited
from hse_monitor.monitor import Monitor
from hse_monitor.store import Store


def messages(store):
    return "\n".join(row[0] for row in store.db.execute("SELECT text FROM outbox"))


async def test_available_no_duplicates_and_reopen(config, lms, store, slot):
    monitor = Monitor(config, lms, store)
    await monitor.cycle()
    assert messages(store).count("🟢 Можно") == 1
    await monitor.cycle()
    assert messages(store).count("🟢 Можно") == 1
    lms.slots.return_value = [replace(slot, participants=36)]
    await monitor.cycle()
    assert "🔴" in messages(store) and "📋" in messages(store)
    lms.slots.return_value = [slot]
    await monitor.cycle()
    assert messages(store).count("🟢 Можно") == 2


async def test_disabled_button_never_books_or_notifies(config, lms, store, slot):
    lms.refresh_forms.return_value = Page("TEST", {slot.id: form(enabled=False)})
    await Monitor(replace(config, autobook_enabled=True, dry_run=False), lms, store).cycle()
    lms.register.assert_not_called()
    assert store.get("available") == []
    assert not store.next_message()


async def test_dry_run(config, lms, store):
    await Monitor(replace(config, autobook_enabled=True), lms, store).cycle()
    lms.register.assert_not_called()
    assert "🧪" in messages(store)


async def test_success_verified_then_never_books_again(config, lms, store, slot):
    lms.refresh_forms.side_effect = [
        lms.page,
        Page("TEST", {slot.id: form(enrolled=True)}),
        lms.page,
    ]
    monitor = Monitor(replace(config, autobook_enabled=True, dry_run=False), lms, store)
    await monitor.cycle()
    assert store.get("booked") == slot.id
    assert store.get("pending") is None
    assert "✅ Успешная" in messages(store)
    await monitor.cycle()
    assert lms.register.await_count == 1


async def test_timeout_does_not_retry_even_after_restart(config, lms, store, slot):
    config = replace(config, autobook_enabled=True, dry_run=False)
    monitor = Monitor(config, lms, store)
    lms.register.side_effect = LMSError("Тайм-аут")
    with pytest.raises(LMSError):
        await monitor.cycle()
    assert store.get("pending")["id"] == slot.id
    assert not monitor.autobook
    restarted = Monitor(config, lms, store)
    await restarted.cycle()
    assert lms.register.await_count == 1
    assert "не подтверждён" in messages(store)


async def test_pending_response_can_be_reconciled(config, lms, store, slot):
    store.set("pending", {"id": slot.id})
    lms.refresh_forms.return_value = Page("TEST", {slot.id: form(enrolled=True)})
    await Monitor(config, lms, store).cycle()
    assert store.get("booked") == slot.id and store.get("pending") is None
    lms.register.assert_not_called()


async def test_http_200_without_membership_is_not_success(config, lms, store):
    await Monitor(replace(config, autobook_enabled=True, dry_run=False), lms, store).cycle()
    assert store.get("pending") and not store.get("booked")
    assert "Успешная запись" not in messages(store)


async def test_existing_membership_prevents_new_booking(config, lms, store):
    lms.page.forms["1111"] = form("1111", enrolled=True)
    await Monitor(replace(config, autobook_enabled=True, dry_run=False), lms, store).cycle()
    assert store.get("booked") == "1111"
    lms.register.assert_not_called()


async def test_auth_failure_preserves_snapshot_and_recovery_notifies(config, lms, store):
    monitor = Monitor(config, lms, store)
    await monitor.cycle()
    saved = store.get("snapshot")
    lms.slots.side_effect = AuthExpired("Сессия истекла")
    with pytest.raises(AuthExpired):
        await monitor.cycle()
    assert store.get("snapshot") == saved
    store.set("error", "Сессия истекла")
    lms.slots.side_effect = None
    await monitor.cycle()
    assert "восстановлена" in messages(store)


async def test_expired_cookie_in_monitor_loop_queues_actionable_alert(config, lms, store):
    monitor = Monitor(config, lms, store)
    await monitor.cycle()
    saved = store.get("snapshot")
    rejected = asyncio.Event()

    async def reject_session():
        rejected.set()
        raise AuthExpired("SmartLMS перенаправляет на вход")

    lms.slots.side_effect = reject_session
    task = asyncio.create_task(monitor.run())
    try:
        async with asyncio.timeout(2):
            await rejected.wait()
        text = messages(store)
        assert text.count("🔐") == 1
        assert "LMS_COOKIE_HEADER" in text
        assert "docker compose up -d --force-recreate" in text
        assert store.get("snapshot") == saved
        lms.register.assert_not_called()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cookie_alert_deduplicates_across_errors_and_restart(config, lms):
    store = Store(config.state_path, "test")
    try:
        monitor = Monitor(config, lms, store)
        monitor.report_error(AuthExpired("Редирект на вход"))
        monitor.report_error(AuthExpired("HTTP 401"))
        monitor.report_error(LMSError("Сетевая ошибка"))
    finally:
        store.close()
    store = Store(config.state_path, "test")
    try:
        monitor = Monitor(config, lms, store)
        monitor.report_error(AuthExpired("HTTP 403"))
        assert messages(store).count("🔐") == 1
        await monitor.cycle()
        await monitor.cycle()
        assert messages(store).count("🔓") == 1
        assert not store.get("auth_expired")
        assert store.get("error") is None
        monitor.report_error(AuthExpired("Сессия снова истекла"))
        assert messages(store).count("🔐") == 2
    finally:
        store.close()


async def test_priority(config, lms, store, slot):
    other = replace(slot, id="9999")
    lms.slots.return_value = [slot, other]
    lms.page.forms[other.id] = form(other.id)
    config = replace(config, autobook_enabled=True, dry_run=False, slot_ids=("9999", slot.id))
    await Monitor(config, lms, store).cycle()
    assert lms.register.call_args.args[0].slot_id == "9999"


async def test_retry_after_is_not_bypassed_by_manual_check(config, lms, store):
    lms.slots.side_effect = RateLimited(120)
    monitor = Monitor(config, lms, store)
    task = asyncio.create_task(monitor.run())
    try:
        for _ in range(10):
            await asyncio.sleep(0)
            if monitor.next_allowed_at:
                break
        assert monitor.next_allowed_at - time.monotonic() > 119
        for _ in range(10):
            monitor.wakeup.set()
            await asyncio.sleep(0)
        assert lms.slots.await_count == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_outbox_persists_and_process_lock(config):
    first = Store(config.state_path, "test")
    first.notify("Message")
    first.set("pending", {"id": "9243"})
    with pytest.raises(ValueError, match="Уже запущен"):
        Store(config.state_path, "test")
    first.close()
    second = Store(config.state_path, "test")
    assert second.next_message()[1] == "Message"
    assert second.get("pending")["id"] == "9243"
    second.close()
