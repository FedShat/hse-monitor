import re
from dataclasses import asdict, dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Config


@dataclass(frozen=True)
class Slot:
    id: str
    name: str
    starts: str  # ISO local date and time, interpreted in Config.timezone.
    duration: int
    capacity: int
    participants: int
    comment: str

    @property
    def free(self) -> int:
        return max(0, self.capacity - self.participants)

    def matches(self, config: Config, now: datetime | None = None) -> bool:
        start = datetime.fromisoformat(self.starts).replace(tzinfo=ZoneInfo(config.timezone))
        now = now or datetime.now(ZoneInfo(config.timezone))
        return (
            start > now
            and (not config.slot_ids or self.id in config.slot_ids)
            and (not config.name_pattern or re.search(config.name_pattern, self.name) is not None)
            and (not config.date_from or start.date() >= config.date_from)
            and (not config.date_to or start.date() <= config.date_to)
            and (not config.time_from or start.time() >= config.time_from)
            and (not config.time_to or start.time() <= config.time_to)
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def label(self) -> str:
        stamp = datetime.fromisoformat(self.starts).strftime("%d.%m.%Y %H:%M")
        return (
            f"#{self.id} · {self.name} · {stamp} · мест {self.free} "
            f"({self.participants}/{self.capacity})"
        )


@dataclass(frozen=True)
class BookingForm:
    slot_id: str
    fields: dict[str, str]
    enabled: bool
    enrolled: bool


@dataclass
class Page:
    sesskey: str
    forms: dict[str, BookingForm]


class LMSError(Exception):
    """Messages must be safe to send to Telegram (no URLs/tokens/response bodies)."""


class AuthExpired(LMSError):
    pass


class ParseError(LMSError):
    pass


class RateLimited(LMSError):
    def __init__(self, retry_after: float):
        super().__init__("SmartLMS ограничил частоту запросов")
        self.retry_after = retry_after
