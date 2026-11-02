import fcntl
import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path: Path, scope: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lockfile = path.with_suffix(".lock").open("a")
        try:
            fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lockfile.close()
            raise ValueError("Уже запущен монитор с этим STATE_PATH") from None
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        old_scope = self.get("scope")
        if old_scope and old_scope != scope:
            self.close()
            raise ValueError("STATE_PATH относится к другому LMS_URL; выберите отдельный файл")
        self.set("scope", scope)

    def close(self) -> None:
        self.db.close()
        self.lockfile.close()

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _put(self, key: str, value) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO state VALUES (?, ?)",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def set(self, key: str, value) -> None:
        with self.db:
            self._put(key, value)

    def _enqueue(self, text: str) -> None:
        # 1500 code points always fit Telegram's 4096 UTF-16 unit limit.
        for i in range(0, len(text), 1500):
            self.db.execute("INSERT INTO outbox(text) VALUES (?)", (text[i : i + 1500],))

    def notify(self, text: str) -> None:
        with self.db:
            self._enqueue(text)

    def record_error(self, message: str, notification: str, *, auth_expired: bool = False) -> None:
        with self.db:
            # Different login errors, intervening network failures and restarts are
            # still the same expired-session episode until a full check succeeds.
            notify = (
                not self.get("auth_expired", False)
                if auth_expired
                else self.get("error") != message
            )
            self._put("error", message)
            if auth_expired:
                self._put("auth_expired", True)
            if notify:
                self._enqueue(notification)

    def clear_error(self) -> None:
        with self.db:
            if self.get("auth_expired", False):
                self._enqueue("🔓 Сессия SmartLMS восстановлена. Проверка слотов снова работает.")
            elif self.get("error"):
                self._enqueue("🟢 Связь со SmartLMS восстановлена.")
            self._put("auth_expired", False)
            self._put("error", None)

    def commit_snapshot(
        self, snapshot: list[dict], available: list[str], events: list[str]
    ) -> None:
        with self.db:
            self._put("snapshot", snapshot)
            self._put("available", available)
            for event in events:
                self._enqueue(event)

    def finish_booking(self, slot_id: str, text: str) -> None:
        with self.db:
            self._put("booked", slot_id)
            self._put("pending", None)
            self._put("autobook", False)
            self._enqueue(text)

    def next_message(self):
        return self.db.execute("SELECT id, text FROM outbox ORDER BY id LIMIT 1").fetchone()

    def acknowledge(self, message_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM outbox WHERE id=?", (message_id,))
