import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import Update
from yarl import URL

from hse_monitor.lms import LMSClient, retry_seconds
from hse_monitor.models import AuthExpired, RateLimited
from hse_monitor.monitor import Monitor
from hse_monitor.telegram import build_dispatcher, deliver


async def test_cookie_is_only_sent_to_lms_and_reload(config):
    config.env_file.write_text("LMS_COOKIE_HEADER='MoodleSession=TEST_FIRST; unrelated=discard'\n")
    async with LMSClient(config) as client:
        client.reload_auth()
        assert (
            client.session.cookie_jar.filter_cookies(URL(config.origin))["MoodleSession"].value
            == "TEST_FIRST"
        )
        assert not client.session.cookie_jar.filter_cookies(URL("https://saml.hse.ru"))
        assert "unrelated" not in client.session.cookie_jar.filter_cookies(URL(config.origin))
        config.env_file.write_text("LMS_COOKIE_HEADER='MoodleSession=TEST_SECOND'\n")
        client.reload_auth()
        assert (
            client.session.cookie_jar.filter_cookies(URL(config.origin))["MoodleSession"].value
            == "TEST_SECOND"
        )
        config.env_file.write_text("LMS_COOKIE_HEADER=''\n")
        for _ in range(2):
            with pytest.raises(AuthExpired):
                client.reload_auth()


@pytest.mark.parametrize(
    "status,exc", [(302, AuthExpired), (401, AuthExpired), (403, AuthExpired), (429, RateLimited)]
)
async def test_http_errors_and_no_redirect_cookie_leak(config, status, exc):
    config.cookie_header = "MoodleSession=TEST"
    async with LMSClient(config) as client:
        response = MagicMock(
            status=status, headers={"Retry-After": "120", "Location": "https://saml.hse.ru/"}
        )
        request_context = MagicMock()
        request_context.__aenter__ = AsyncMock(return_value=response)
        client.session.request = MagicMock(return_value=request_context)
        with pytest.raises(exc):
            await client.request("GET", "/mod/selfgrouping/view.php")
        assert client.session.request.call_args.kwargs["allow_redirects"] is False


def test_retry_after_date_and_invalid():
    assert retry_seconds("120") == 120
    assert retry_seconds("nonsense") == 60
    assert retry_seconds("Wed, 01 Jan 2020 00:00:00 GMT") == 1


async def test_telegram_commands_are_owner_only(config, lms, store):
    bot = Bot("123456789:TEST_TOKEN_NOT_REAL_abcdefghijklmnopqrstuvwxyz")
    bot.session.make_request = AsyncMock(return_value=True)
    dispatcher = build_dispatcher(config, Monitor(config, lms, store))
    try:
        for user_id, chat_id in [(99, 99), (99, 42), (42, -100)]:
            update = Update.model_validate(
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 1,
                        "date": 1,
                        "chat": {"id": chat_id, "type": "private"},
                        "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                        "text": "/autobook on",
                    },
                }
            )
            await dispatcher.feed_update(bot, update)
        assert store.get("autobook") is None
        bot.session.make_request.assert_not_called()
        update = Update.model_validate(
            {
                "update_id": 2,
                "message": {
                    "message_id": 2,
                    "date": 1,
                    "chat": {"id": 42, "type": "private"},
                    "from": {"id": 42, "is_bot": False, "first_name": "Owner"},
                    "text": "/autobook on",
                },
            }
        )
        await dispatcher.feed_update(bot, update)
        assert store.get("autobook") is True
    finally:
        await bot.session.close()


async def test_telegram_retries_without_losing_outbox(config, store, monkeypatch):
    store.notify("🟢 Test")
    original_sleep = asyncio.sleep

    async def fast_sleep(_):
        await original_sleep(0)

    monkeypatch.setattr("hse_monitor.telegram.asyncio.sleep", fast_sleep)
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=[
            TelegramRetryAfter(
                method=SendMessage(chat_id=42, text="x"), message="retry", retry_after=1
            ),
            True,
        ]
    )
    task = asyncio.create_task(deliver(bot, store, config))
    try:
        for _ in range(20):
            await original_sleep(0)
            if not store.next_message():
                break
        assert not store.next_message()
        assert bot.send_message.await_count == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
