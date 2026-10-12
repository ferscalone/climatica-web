"""
Общая настройка тестов: SQLite вместо PostgreSQL, временные папки для логов
и выгрузок, Telegram-доставка выключена (в тестах подставляется фейковый канал).
Переменные окружения выставляются ДО импорта app — настройки читаются при импорте.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="climatica-tests-"))

os.environ.update({
    "DATABASE_URL": f"sqlite+aiosqlite:///{(_TMP / 'test.db').as_posix()}",
    "CHAMBERS": "1:127.0.0.1;2:127.0.0.1",
    "UDP_PORT": "1",
    "SOCKET_TIMEOUT": "0.1",
    "LOG_DIR": str(_TMP / "logs"),
    "EXPORT_DIR": str(_TMP / "exports"),
    "TELEGRAM_BOT_INTERNAL_URL": "",
    "ALERT_RETRY_INTERVAL": "0.3",
    "ALERT_MAX_ATTEMPTS": "3",
    "OFFLINE_AFTER_FAILURES": "2",
    "THRESHOLD_HYSTERESIS": "0.5",
    "TIMEZONE": "Europe/Moscow",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_IDS": "",
    "INTERNAL_API_TOKEN": "",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from app.logging_setup import setup_logging  # noqa: E402

setup_logging()

from app.models.db import AlertEvent, AuditLog, Measurement, Threshold, async_session, init_db  # noqa: E402


class FakeChannel:
    """Канал доставки, который можно заставить падать первые N раз."""

    name = "fake"

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.sent: list[dict] = []
        self.calls = 0

    async def send(self, event: dict) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("бот недоступен")
        self.sent.append(event)


@pytest.fixture
async def clean_db():
    await init_db()
    async with async_session() as session:
        for model in (Measurement, AlertEvent, AuditLog, Threshold):
            await session.execute(delete(model))
        await session.commit()
    yield


@pytest.fixture
def client(monkeypatch):
    """Приложение целиком, но без реального опроса камер."""
    import asyncio

    from app.main import app
    from app.services.poller import poller_manager

    monkeypatch.setattr(poller_manager, "start_all", lambda: None)

    async def wipe():
        await init_db()
        async with async_session() as session:
            for model in (Measurement, AlertEvent, AuditLog, Threshold):
                await session.execute(delete(model))
            await session.commit()

    asyncio.run(wipe())

    with TestClient(app) as c:
        yield c
