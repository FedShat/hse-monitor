import json
import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .models import AuthExpired, BookingForm, Page, ParseError, Slot


def parse_slots(text: str) -> list[Slot]:
    try:
        data = json.loads(text)
    except ValueError:
        if re.search(r"notloggedin|page-login|kc-form-login", text):
            raise AuthExpired("Сессия SmartLMS истекла; обновите LMS_COOKIE_HEADER") from None
        raise ParseError("SmartLMS вернул не JSON: проверьте доступ к экспорту") from None
    # Moodle JSON dataformat exports a list of tables, each a list of records.
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], list):
        raise ParseError("Изменился формат JSON-экспорта SmartLMS")
    result = []
    try:
        for row in data[0]:
            slot_id = str(row["idofslot"]).strip()
            if not slot_id.isdigit():
                raise ValueError
            stamp = datetime.strptime(
                f"{row['startingdate']} {row['startingtime']}", "%d.%m.%y %H:%M"
            )
            counts = [
                int(row[k])
                for k in ("timelimitminutes", "maximumnumberofparticipants", "numberofparticipants")
            ]
            if any(v < 0 for v in counts):
                raise ValueError
            result.append(
                Slot(
                    slot_id,
                    str(row["name"]).strip(),
                    stamp.isoformat(timespec="minutes"),
                    *counts,
                    " ".join(str(row["commentary"]).split()),
                )
            )
    except (TypeError, KeyError, ValueError):
        raise ParseError("Не удалось разобрать строку слота; снимок не обновлён") from None
    if len({s.id for s in result}) != len(result):
        raise ParseError("В JSON-экспорте повторяются ID слотов")
    return result


def parse_page(text: str, origin: str, cmid: str) -> Page:
    soup = BeautifulSoup(text, "html.parser")
    if soup.select_one("body.notloggedin, #page-login-index, #kc-form-login"):
        raise AuthExpired("Сессия SmartLMS истекла; обновите LMS_COOKIE_HEADER")
    if not soup.select_one("#page-mod-selfgrouping-view"):
        raise ParseError("Вместо таблицы получена другая страница SmartLMS")
    token = soup.select_one('input[name="sesskey"]')
    if not token or not token.get("value"):
        raise ParseError("На странице отсутствует sesskey")
    # Do not silently treat a partial/paginated table as a complete enrollment view.
    if soup.select_one('.pagination a[href*="page="]'):
        raise ParseError("Появилась пагинация HTML; проверка записи требует адаптации")
    forms = {}
    for form in soup.select("form[action]"):
        action = urlsplit(urljoin(origin, form["action"]))
        if action.path != "/mod/selfgrouping/userselfrecord.php":
            continue
        if f"{action.scheme}://{action.netloc}" != origin or action.query:
            raise ParseError("Неожиданный адрес формы записи")
        fields = {x["name"]: str(x.get("value", "")) for x in form.select("input[name]")}
        if (
            str(form.get("method", "get")).lower() != "post"
            or set(fields) != {"cmid", "id", "mode", "sesskey"}
            or fields["cmid"] != cmid
            or not fields["id"].isdigit()
            or fields["mode"] not in {"on", "off"}
            or not fields["sesskey"]
        ):
            raise ParseError("Изменился формат формы записи; автозапись остановлена")
        button = form.select_one('button[type="submit"], input[type="submit"]')
        enabled = bool(
            button is not None
            and not button.has_attr("disabled")
            and button.get("aria-disabled") != "true"
        )
        forms[fields["id"]] = BookingForm(
            fields["id"],
            fields,
            enabled,
            fields["mode"] == "off",
        )
    if not forms:
        # An empty filtered table is valid only when Moodle explicitly says so.
        notices = " ".join(x.get_text(" ", strip=True) for x in soup.select(".alert"))
        if "Nothing to display" not in notices and "Нечего отображать" not in notices:
            raise ParseError("Не найдены формы записи или сообщение о пустой таблице")
    return Page(str(token["value"]), forms)
