import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie

import aiohttp
from dotenv import dotenv_values
from yarl import URL

from .config import Config
from .models import AuthExpired, BookingForm, LMSError, Page, RateLimited, Slot
from .parser import parse_page, parse_slots


def retry_seconds(value: str | None) -> float:
    try:
        return max(1, float(value or "60"))
    except ValueError:
        try:
            return max(1, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 60


class LMSClient:
    def __init__(self, config: Config):
        self.config = config
        self.session: aiohttp.ClientSession | None = None
        self.page: Page | None = None
        self.page_at = 0.0
        self._cookie_source = ""
        self._configured_cookie = config.cookie_header
        self._env_mtime: int | None = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.request_timeout),
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; HSESlotMonitor/0.1)",
                "Cache-Control": "no-cache",
            },
            connector=aiohttp.TCPConnector(limit=2, ttl_dns_cache=300),
        )
        return self

    async def __aexit__(self, *_):
        if self.session:
            await self.session.close()

    def reload_auth(self) -> None:
        assert self.session is not None
        path = self.config.env_file
        mtime = path.stat().st_mtime_ns if path.exists() else None
        if mtime != self._env_mtime:
            self._configured_cookie = str(
                dotenv_values(path).get("LMS_COOKIE_HEADER", self.config.cookie_header) or ""
            )
            self._env_mtime = mtime
        source = self._configured_cookie
        if source != self._cookie_source:
            cookies: SimpleCookie = SimpleCookie()
            try:
                cookies.load(source)
            except Exception:
                raise AuthExpired("Некорректный LMS_COOKIE_HEADER") from None
            if "MoodleSession" not in cookies or not cookies["MoodleSession"].value:
                raise AuthExpired("В LMS_COOKIE_HEADER отсутствует MoodleSession")
            self.session.cookie_jar.clear()
            # Only the session cookie is needed; never send it to the SSO host.
            self.session.cookie_jar.update_cookies(
                {"MoodleSession": cookies["MoodleSession"].value},
                response_url=URL(self.config.origin),
            )
            self._cookie_source = source
            self.page = None
        if not self._cookie_source:
            raise AuthExpired("Заполните LMS_COOKIE_HEADER в .env")

    async def request(self, method: str, path: str, **kwargs) -> str:
        assert self.session is not None
        self.reload_auth()
        try:
            async with self.session.request(
                method, self.config.origin + path, allow_redirects=False, **kwargs
            ) as response:
                if response.status == 429:
                    raise RateLimited(retry_seconds(response.headers.get("Retry-After")))
                if response.status in {401, 403}:
                    self.page = None
                    raise AuthExpired(
                        "SmartLMS отказал в доступе; обновите cookie или проверьте вход"
                    )
                if 300 <= response.status < 400:
                    location = response.headers.get("Location", "")
                    if method == "POST" and "/mod/selfgrouping/view.php" in location:
                        return ""  # Verification is a separate, authenticated GET.
                    self.page = None
                    raise AuthExpired("SmartLMS перенаправляет на вход; обновите LMS_COOKIE_HEADER")
                if response.status >= 400:
                    raise LMSError(f"SmartLMS вернул HTTP {response.status}")
                return await response.text()
        except (TimeoutError, aiohttp.ClientError):
            # aiohttp exception strings can contain URLs with sesskey. Do not log them.
            raise LMSError("Сетевая ошибка или тайм-аут SmartLMS") from None

    def params(self) -> dict[str, str]:
        result = {"id": self.config.cmid, "tab": "slot", "lang": "en"}
        if self.page:
            result.update(
                {
                    "sesskey": self.page.sesskey,
                    "_qf__mod_selfgrouping_local_form_filter": "1",
                    "currentnumberofgroup": "0",
                }
            )
        return result

    async def refresh_forms(self) -> Page:
        self.reload_auth()
        text = await self.request("GET", "/mod/selfgrouping/view.php", params=self.params())
        try:
            page = parse_page(text, self.config.origin, self.config.cmid)
        except AuthExpired:
            self.page = None
            raise
        self.page = page
        self.page_at = time.monotonic()
        return page

    async def slots(self) -> list[Slot]:
        self.reload_auth()
        if not self.page or time.monotonic() - self.page_at > self.config.auth_refresh_interval:
            await self.refresh_forms()
        try:
            text = await self.request(
                "GET", "/mod/selfgrouping/view.php", params={**self.params(), "download": "json"}
            )
            return parse_slots(text)
        except AuthExpired:
            self.page = None
            raise

    async def register(self, form: BookingForm) -> None:
        if not form.enabled or form.enrolled or form.fields["mode"] != "on":
            raise LMSError("Слот недоступен для записи")
        # Exactly the observed Moodle form; no automatic retries on mutation.
        await self.request(
            "POST",
            "/mod/selfgrouping/userselfrecord.php",
            data=form.fields,
            headers={"Referer": self.config.lms_url, "Origin": self.config.origin},
        )
