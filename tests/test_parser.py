import json
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hse_monitor.models import AuthExpired, ParseError
from hse_monitor.parser import parse_page, parse_slots


def test_real_export_shape(export_json):
    (slot,) = parse_slots(export_json)
    assert slot.id == "9243" and slot.free == 1
    assert slot.starts == "2026-11-02T14:40"
    assert slot.comment == "max 36"


@pytest.mark.parametrize("text", ["{}", "[]", "[{}]", "[[{}]]", "<html>Error</html>"])
def test_bad_export_is_error_not_empty_table(text):
    with pytest.raises(ParseError):
        parse_slots(text)


def test_empty_table_and_duplicate_ids(export_json):
    assert parse_slots("[[]]") == []
    data = json.loads(export_json)
    data[0] *= 2
    with pytest.raises(ParseError):
        parse_slots(json.dumps(data))


def test_expired_session_is_not_table_change():
    with pytest.raises(AuthExpired):
        parse_slots('<body id="page-login-index">Login</body>')
    with pytest.raises(AuthExpired):
        parse_page('<body class="notloggedin">', "https://edu.hse.ru", "1887331")


def test_enabled_disabled_and_enrolled(html_page):
    def parse(html):
        return parse_page(html, "https://edu.hse.ru", "1887331").forms["9243"]

    assert parse(html_page).enabled
    assert not parse(html_page.replace("<button ", "<button disabled ")).enabled
    assert parse(html_page.replace('value="on"', 'value="off"')).enrolled


@pytest.mark.parametrize(
    "old,new",
    [
        ("https://edu.hse.ru/mod", "https://example.com/mod"),
        ('value="1887331"', 'value="999999"'),
        ('method="post"', 'method="get"'),
        ('value="on"', 'value="delete"'),
    ],
)
def test_unexpected_mutation_form_fails_closed(html_page, old, new):
    with pytest.raises(ParseError):
        parse_page(html_page.replace(old, new), "https://edu.hse.ru", "1887331")


def test_zero_capacity_and_past_slots(config, slot):
    assert replace(slot, capacity=0).free == 0
    now = datetime(2026, 11, 1, tzinfo=ZoneInfo("Europe/Moscow"))
    assert slot.matches(config, now)
    assert not replace(slot, starts="2026-04-01T09:40").matches(config, now)
    assert not slot.matches(replace(config, slot_ids=("9999",)), now)


def test_explicit_empty_html_is_valid():
    page = parse_page(
        """<body id="page-mod-selfgrouping-view">
    <input name="sesskey" value="TEST"><div class="alert">Nothing to display</div>""",
        "https://edu.hse.ru",
        "1887331",
    )
    assert page.forms == {}


def test_timezone_and_filters(slot, config):
    slot = replace(slot, starts="2026-11-02T14:40")
    # 11:41 UTC is already 14:41 in Moscow: the slot is past.
    assert not slot.matches(config, datetime.fromisoformat("2026-11-02T11:41:00+00:00"))
    assert slot.matches(config, datetime.fromisoformat("2026-11-02T11:39:00+00:00"))
