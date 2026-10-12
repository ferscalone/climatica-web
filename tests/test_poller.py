"""
Логика переходов в поллере: алерт только на смену состояния, без спама.
Камера подменяется фейковым клиентом, БД — SQLite из conftest.
"""

import pytest
from sqlalchemy import select

from app.models.db import AlertType, Measurement, async_session
from app.services import poller as poller_module
from app.services.poller import ChamberPoller
from app.services.thresholds import threshold_service


class FakeClient:
    def __init__(self) -> None:
        self.script: list = []

    async def read_values(self):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return {"temperature": item, "humidity": None, "target_temperature": None, "target_humidity": None}


@pytest.fixture
async def setup(clean_db, monkeypatch):
    await threshold_service.load()  # пороги 2..8 по умолчанию
    published = []
    monkeypatch.setattr(
        poller_module.alert_service, "publish",
        lambda alert_type, message, **kw: published.append((alert_type, message)),
    )
    client = FakeClient()
    return ChamberPoller(1, "10.0.0.1", client=client), client, published


async def run(poller, client, script):
    client.script = list(script)
    for _ in script:
        await poller.poll_once()


async def test_online_offline_transitions(setup):
    poller, client, published = setup
    down = TimeoutError("нет ответа")

    # Первое состояние не считается переходом
    await run(poller, client, [5.0, 5.0])
    assert published == []

    # Один сбой — ещё не офлайн (OFFLINE_AFTER_FAILURES=2), второй — офлайн
    await run(poller, client, [down])
    assert published == []
    await run(poller, client, [down, down, down])
    assert [a for a, _ in published] == [AlertType.CHAMBER_OFFLINE]

    await run(poller, client, [5.0, 5.0])
    assert [a for a, _ in published] == [AlertType.CHAMBER_OFFLINE, AlertType.CHAMBER_ONLINE]


async def test_threshold_breach_and_recovery(setup):
    poller, client, published = setup

    await run(poller, client, [5.0, 9.0, 9.5, 10.0])
    assert [a for a, _ in published] == [AlertType.THRESHOLD_BREACH]
    assert "выше верхней" in published[0][1]

    # 7.8 — ещё в зоне гистерезиса, 7.0 — норма
    await run(poller, client, [7.8, 7.0, 7.0])
    assert [a for a, _ in published][1:] == [AlertType.THRESHOLD_RECOVERY]

    await run(poller, client, [1.0])
    assert published[-1][0] == AlertType.THRESHOLD_BREACH
    assert "ниже нижней" in published[-1][1]


async def test_offline_rows_are_saved_for_chart_gaps(setup):
    poller, client, _ = setup
    down = TimeoutError("нет ответа")
    await run(poller, client, [5.0, down, down, down, 6.0])

    async with async_session() as session:
        rows = (await session.execute(select(Measurement).order_by(Measurement.id))).scalars().all()

    # единичный сбой (первый down) не пишется, затем 2 офлайн-строки
    assert [(r.online, r.temperature) for r in rows] == [
        (True, 5.0), (False, None), (False, None), (True, 6.0),
    ]
