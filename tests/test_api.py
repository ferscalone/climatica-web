"""
Интеграционные тесты: настоящее приложение FastAPI (TestClient), SQLite,
реальные HTTP-запросы и WebSocket. Не опрашиваются только камеры.
"""

import asyncio
import io
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.models.db import Measurement, async_session
from app.services.alerts import alert_service
from tests.conftest import FakeChannel

T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def seed(rows):
    async def go():
        async with async_session() as session:
            session.add_all(rows)
            await session.commit()
    asyncio.run(go())


def series(chamber_id, n, start=T0, step=1.0, temp=lambda i: 5.0, offline=()):
    return [
        Measurement(
            chamber_id=chamber_id,
            chamber_ip="127.0.0.1",
            temperature=None if i in offline else temp(i),
            online=i not in offline,
            recorded_at=start + timedelta(seconds=i * step),
        )
        for i in range(n)
    ]


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except (IndexError, KeyError):
            pass
        time.sleep(0.05)
    return False


# ------------------------------------------------------------------ history

def test_unknown_chamber_is_404_everywhere(client):
    assert client.get("/api/history/99").status_code == 404
    assert client.get("/api/export/99").status_code == 404
    assert client.put("/api/thresholds/99/temperature", json={"min_value": 1, "max_value": 2}).status_code == 404


def test_history_period_and_downsampling_keeps_peak_and_gap(client):
    offline = set(range(5000, 5050))

    def temp(i):
        return 40.0 if i == 7777 else 5.0 + (i % 10) * 0.01   # единственный пик

    seed(series(1, 12000, temp=temp, offline=offline))

    r = client.get("/api/history/1", params={
        "date_from": T0.isoformat(),
        "date_to": (T0 + timedelta(hours=4)).isoformat(),
    }).json()

    assert r["total"] == 12000
    assert r["downsampled"] is True
    assert r["returned"] <= 4000
    temps = [p["temperature"] for p in r["points"] if p["temperature"] is not None]
    assert max(temps) == 40.0, "пик потерялся при прореживании"
    assert any(not p["online"] for p in r["points"]), "разрыв связи потерялся"
    times = [p["recorded_at"] for p in r["points"]]
    assert times == sorted(times)

    # Узкий срез отдаётся целиком
    small = client.get("/api/history/1", params={
        "date_from": T0.isoformat(),
        "date_to": (T0 + timedelta(seconds=99)).isoformat(),
    }).json()
    assert small["total"] == small["returned"] == 100
    assert small["downsampled"] is False

    # Без периода — последние N точек
    assert len(client.get("/api/history/1?limit=10").json()["points"]) == 10


def test_history_rejects_inverted_period(client):
    r = client.get("/api/history/1", params={
        "date_from": (T0 + timedelta(hours=1)).isoformat(),
        "date_to": T0.isoformat(),
    })
    assert r.status_code == 400


# ---------------------------------------------------------- export / import

@pytest.mark.parametrize("fmt", ["csv", "xlsx"])
def test_export_period_and_import_roundtrip(client, fmt):
    seed(series(1, 200, temp=lambda i: 3.0 + i / 100, offline=set(range(50, 60))))

    r = client.get("/api/export/1", params={
        "format": fmt,
        "date_from": (T0 + timedelta(seconds=10)).isoformat(),
        "date_to": (T0 + timedelta(seconds=109)).isoformat(),
    })
    assert r.status_code == 200
    assert "climatica_chamber_1_" in r.headers["content-disposition"]

    if fmt == "csv":
        df = pd.read_csv(io.BytesIO(r.content), encoding="utf-8-sig")
    else:
        df = pd.read_excel(io.BytesIO(r.content))
    assert len(df) == 100
    assert list(df.columns) == ["Date", "Time", "Temperature", "Online"]
    # Время в выгрузке — московское (UTC+3)
    assert df.iloc[0]["Time"] == "15:00:10"

    imported = client.post("/api/import", files={"file": (f"export.{fmt}", r.content)}).json()
    assert imported["total"] == 100
    offline_points = [p for p in imported["points"] if not p["online"]]
    assert len(offline_points) == 10
    first = datetime.fromisoformat(imported["points"][0]["recorded_at"])
    assert first == T0 + timedelta(seconds=10)


def test_export_empty_period(client):
    assert client.get("/api/export/1").status_code == 404


def test_import_semicolon_csv_with_russian_headers(client):
    csv = "Дата;Время;Температура, °C\n2026-01-01;10:00:00;4,5\n2026-01-01;10:00:01;4,6\n"
    r = client.post("/api/import", files={"file": ("ru.csv", csv.encode("utf-8"))})
    assert r.status_code == 200
    assert [p["temperature"] for p in r.json()["points"]] == [4.5, 4.6]


def test_import_errors(client):
    assert client.post("/api/import", files={"file": ("a.csv", b"")}).status_code == 400
    assert client.post("/api/import", files={"file": ("a.txt", b"x")}).status_code == 400
    r = client.post("/api/import", files={"file": ("a.csv", b"foo,bar\n1,2\n")})
    assert r.status_code == 400
    assert "Temperature" in r.json()["detail"]


# ------------------------------------------------------- thresholds / audit

def test_threshold_validation(client):
    ok = {"min_value": 1, "max_value": 9}
    assert client.put("/api/thresholds/1/temperatur", json=ok).status_code == 422
    assert client.put("/api/thresholds/1/temperature", json={"min_value": 9, "max_value": 1}).status_code == 422
    assert client.put("/api/thresholds/1/temperature", json={}).status_code == 422
    assert client.put("/api/thresholds/1/temperature", json=ok).status_code == 200


def test_threshold_change_audit_alert_and_websocket(client):
    channel = FakeChannel()
    alert_service.channels = [channel]

    with client.websocket_connect("/ws") as ws:
        r = client.put("/api/thresholds/1/temperature", json={
            "min_value": 1.5, "max_value": 7.5, "enabled": False,
            "actor_name": "Иван Иванов", "comment": "летние нормы",
        })
        assert r.status_code == 200
        assert r.json()["enabled"] is False

        msg = ws.receive_json()
        assert msg["type"] == "alert"
        assert msg["alert_type"] == "threshold_changed"
        assert "2…8 → 1.5…7.5 (проверка выключена)" in msg["message"]
        assert "Иван Иванов" in msg["message"] and "летние нормы" in msg["message"]

    audit = client.get("/api/audit-log").json()[0]
    assert audit["actor_name"] == "Иван Иванов"
    assert audit["actor_ip"] == "testclient"
    assert audit["old_value"] == {"min_value": 2.0, "max_value": 8.0, "enabled": True}
    assert audit["new_value"] == {"min_value": 1.5, "max_value": 7.5, "enabled": False}
    assert audit["comment"] == "летние нормы"

    assert wait_for(lambda: len(channel.sent) == 1)
    assert channel.sent[0]["alert_type"] == "threshold_changed"
    assert wait_for(lambda: client.get("/api/alerts").json()[0]["delivered"])

    t = [x for x in client.get("/api/thresholds").json() if x["chamber_id"] == 1][0]
    assert (t["min_value"], t["max_value"], t["enabled"]) == (1.5, 7.5, False)


def test_alert_retry_until_delivered(client):
    channel = FakeChannel(fail_times=2)
    alert_service.channels = [channel]

    client.put("/api/thresholds/2/temperature", json={"min_value": 0, "max_value": 10})

    # 1-я попытка сразу, ещё 2 — в retry-цикле (интервал 0.3 с в conftest)
    assert wait_for(lambda: client.get("/api/alerts").json()[0]["delivered"], timeout=10)
    event = client.get("/api/alerts").json()[0]
    assert event["delivery_attempts"] == 3
    assert len(channel.sent) == 1


def test_alert_gives_up_and_reports_failure(client):
    channel = FakeChannel(fail_times=100)
    alert_service.channels = [channel]

    client.put("/api/thresholds/2/temperature", json={"min_value": 0, "max_value": 11})

    def failed_reported():
        return any(a["alert_type"] == "delivery_failed" for a in client.get("/api/alerts").json())

    assert wait_for(failed_reported, timeout=10)
    original = [a for a in client.get("/api/alerts").json() if a["alert_type"] == "threshold_changed"][0]
    assert original["delivered"] is False
    assert original["delivery_attempts"] == 3   # ALERT_MAX_ATTEMPTS


# ------------------------------------------------------------------ journal

def test_journal_exports_and_full_copy(client):
    client.put("/api/thresholds/1/temperature", json={"min_value": 1, "max_value": 9, "actor_name": "Пётр"})
    assert wait_for(lambda: len(client.get("/api/alerts").json()) == 1)

    now = datetime.now(timezone.utc)
    period = {"date_from": (now - timedelta(minutes=5)).isoformat(), "date_to": (now + timedelta(minutes=5)).isoformat()}
    old = {"date_from": "2020-01-01T00:00:00Z", "date_to": "2020-01-02T00:00:00Z"}

    assert len(client.get("/api/audit-log", params=period).json()) == 1
    assert client.get("/api/audit-log", params=old).json() == []

    csv = client.get("/api/audit-log/export", params={**period, "format": "csv"})
    assert csv.status_code == 200 and "Пётр" in csv.content.decode("utf-8-sig")
    assert client.get("/api/audit-log/export", params={**old, "format": "csv"}).status_code == 404

    xlsx = client.get("/api/alerts/export", params={**period, "format": "xlsx"})
    assert pd.read_excel(io.BytesIO(xlsx.content)).iloc[0]["Тип"] == "threshold_changed"

    log = client.get("/api/journal/download")
    assert log.status_code == 200
    text = log.text
    assert "THRESHOLD CHANGED chamber=1" in text and "ALERT #" in text and "Пётр" in text


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
