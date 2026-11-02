import json
from unittest.mock import AsyncMock

import pytest

from hse_monitor.config import Config
from hse_monitor.models import BookingForm, Page, Slot
from hse_monitor.store import Store


@pytest.fixture
def config(tmp_path):
    return Config(env_file=tmp_path / ".env", state_path=tmp_path / "state.sqlite3", chat_id=42)


@pytest.fixture
def store(config):
    result = Store(config.state_path, "test")
    yield result
    result.close()


@pytest.fixture
def slot():
    return Slot("9243", "TS_TEST", "2099-11-02T14:40", 60, 36, 35, "")


def form(slot_id="9243", enabled=True, enrolled=False):
    return BookingForm(
        slot_id,
        {
            "id": slot_id,
            "cmid": "1887331",
            "mode": "off" if enrolled else "on",
            "sesskey": "TEST_ONLY",
        },
        enabled,
        enrolled,
    )


@pytest.fixture
def lms(slot):
    class FakeLMS:
        page = Page("TEST_ONLY", {slot.id: form(slot.id)})
        slots = AsyncMock(return_value=[slot])
        register = AsyncMock()
        refresh_forms = AsyncMock(return_value=page)

    return FakeLMS()


@pytest.fixture
def export_json():
    return json.dumps(
        [
            [
                {
                    "idofslot": "9243",
                    "name": "TS021126",
                    "startingdate": "02.11.26",
                    "startingtime": "14:40",
                    "timelimitminutes": "60",
                    "maximumnumberofparticipants": "36",
                    "numberofparticipants": "35",
                    "commentary": " max 36   ",
                }
            ]
        ]
    )


@pytest.fixture
def html_page():
    # Minimal sanitized markup using the observed SmartLMS form contract.
    return """<html><body id="page-mod-selfgrouping-view">
    <input type="hidden" name="sesskey" value="TEST_ONLY">
    <table><tr><td>9243</td><td>
    <form method="post" action="https://edu.hse.ru/mod/selfgrouping/userselfrecord.php">
    <input type="hidden" name="cmid" value="1887331">
    <input type="hidden" name="id" value="9243">
    <input type="hidden" name="mode" value="on">
    <input type="hidden" name="sesskey" value="TEST_ONLY">
    <button type="submit">Register</button>
    </form></td></tr></table></body></html>"""
