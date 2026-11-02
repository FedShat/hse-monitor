import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from dotenv import dotenv_values


@dataclass
class Config:
    env_file: Path = Path(".env")
    lms_url: str = "https://edu.hse.ru/mod/selfgrouping/view.php?tab=slot&id=1887331"
    cookie_header: str = field(default="", repr=False)
    bot_token: str = field(default="", repr=False)
    chat_id: int = 0
    poll_interval: float = 3
    request_timeout: float = 20
    auth_refresh_interval: float = 300
    max_backoff: float = 300
    autobook_enabled: bool = False
    dry_run: bool = True
    notify_changes: bool = True
    notify_initial: bool = True
    slot_ids: tuple[str, ...] = ()
    name_pattern: str = ""
    date_from: date | None = None
    date_to: date | None = None
    time_from: time | None = None
    time_to: time | None = None
    timezone: str = "Europe/Moscow"
    state_path: Path = Path("data/state.sqlite3")

    @property
    def cmid(self) -> str:
        return parse_qs(urlsplit(self.lms_url).query)["id"][0]

    @property
    def origin(self) -> str:
        u = urlsplit(self.lms_url)
        return f"{u.scheme}://{u.netloc}"

    @property
    def view_url(self) -> str:
        return self.origin + "/mod/selfgrouping/view.php"

    def validate(self, telegram: bool = False) -> None:
        u = urlsplit(self.lms_url)
        if (
            u.scheme != "https"
            or u.username
            or u.password
            or u.path != "/mod/selfgrouping/view.php"
            or not parse_qs(u.query).get("id", [""])[0].isdigit()
        ):
            raise ValueError("LMS_URL: нужен HTTPS URL selfgrouping/view.php с числовым id")
        if set(parse_qs(u.query)) - {"id", "tab"} or u.fragment:
            raise ValueError("LMS_URL должен содержать только id и tab, без токенов и фильтров")
        if not all(
            math.isfinite(x)
            for x in (
                self.poll_interval,
                self.request_timeout,
                self.auth_refresh_interval,
                self.max_backoff,
            )
        ):
            raise ValueError("Интервалы и тайм-ауты должны быть конечными числами")
        if self.poll_interval < 1 or self.request_timeout <= 0:
            raise ValueError("POLL_INTERVAL >= 1, REQUEST_TIMEOUT > 0")
        if self.auth_refresh_interval < 30 or self.max_backoff < self.poll_interval:
            raise ValueError("AUTH_REFRESH_INTERVAL >= 30, MAX_BACKOFF >= POLL_INTERVAL")
        ZoneInfo(self.timezone)
        re.compile(self.name_pattern)
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("DATE_FROM позже DATE_TO")
        if self.time_from and self.time_to and self.time_from > self.time_to:
            raise ValueError("TIME_FROM позже TIME_TO")
        if any(not x.isdigit() for x in self.slot_ids):
            raise ValueError("SLOT_IDS: числовые ID через запятую")
        if telegram and (not self.bot_token or self.chat_id <= 0):
            raise ValueError("Заполните TELEGRAM_BOT_TOKEN и положительный TELEGRAM_CHAT_ID")


def load_config(env_file: str | Path = ".env") -> Config:
    values = {**dotenv_values(env_file), **os.environ}

    def get(key: str, default: str = "") -> str:
        return str(values.get(key) or default)

    def boolean(key: str, default: bool) -> bool:
        value = get(key, str(default)).lower()
        if value not in {"true", "false", "1", "0", "yes", "no"}:
            raise ValueError(f"{key}: ожидается true/false")
        return value in {"true", "1", "yes"}

    return Config(
        env_file=Path(env_file),
        lms_url=get("LMS_URL", Config.lms_url),
        cookie_header=get("LMS_COOKIE_HEADER"),
        bot_token=get("TELEGRAM_BOT_TOKEN"),
        chat_id=int(get("TELEGRAM_CHAT_ID", "0")),
        poll_interval=float(get("POLL_INTERVAL", "3")),
        request_timeout=float(get("REQUEST_TIMEOUT", "20")),
        auth_refresh_interval=float(get("AUTH_REFRESH_INTERVAL", "300")),
        max_backoff=float(get("MAX_BACKOFF", "300")),
        autobook_enabled=boolean("AUTOBOOK_ENABLED", False),
        dry_run=boolean("AUTOBOOK_DRY_RUN", True),
        notify_changes=boolean("NOTIFY_TABLE_CHANGES", True),
        notify_initial=boolean("NOTIFY_INITIAL_AVAILABLE", True),
        slot_ids=tuple(x.strip() for x in get("SLOT_IDS").split(",") if x.strip()),
        name_pattern=get("SLOT_NAME_REGEX"),
        date_from=date.fromisoformat(get("DATE_FROM")) if get("DATE_FROM") else None,
        date_to=date.fromisoformat(get("DATE_TO")) if get("DATE_TO") else None,
        time_from=time.fromisoformat(get("TIME_FROM")) if get("TIME_FROM") else None,
        time_to=time.fromisoformat(get("TIME_TO")) if get("TIME_TO") else None,
        timezone=get("TZ", "Europe/Moscow"),
        state_path=Path(get("STATE_PATH", "data/state.sqlite3")),
    )
