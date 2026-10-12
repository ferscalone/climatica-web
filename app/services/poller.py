import asyncio
import logging
from datetime import datetime, timezone

from app.config import settings
from app.logging_setup import journal
from app.modbus.client import FridgeClient
from app.models.db import PARAMETER_LABELS, AlertType, Measurement, async_session
from app.services.alerts import alert_service
from app.services.broadcaster import broadcaster
from app.services.thresholds import active_parameters, evaluate, threshold_service

logger = logging.getLogger("climatica.poller")


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


class ChamberPoller:
    """
    Опрашивает одну камеру в цикле и публикует результат.

    Аналог FridgeWorker(QThread) из оригинала:
      - там: QThread.run() -> emit(Signal) -> GUI + CSVLogger
      - здесь: asyncio task -> broadcaster.broadcast() (WS) + запись в Postgres

    Алерты шлются только на ПЕРЕХОД состояния (online→offline, норма→вне нормы
    и обратно), а не на каждый опрос — иначе при опросе раз в секунду бот
    получал бы тысячи одинаковых сообщений.
    """

    def __init__(self, chamber_id: int, ip: str, client: FridgeClient | None = None):
        self.chamber_id = chamber_id
        self.ip = ip
        self.name = settings.chamber_name(chamber_id)
        self.client = client or FridgeClient(chamber_id, ip)
        self._task: asyncio.Task | None = None
        self._running = False

        # None = состояние ещё не известно (первый опрос не считается переходом)
        self.online: bool | None = None
        self.failures = 0
        self.last_error: str | None = None
        # Последняя прочитанная температура (для /api/health и стартовой страницы шлюза)
        self.temperature: float | None = None
        # Все последние прочитанные значения и время чтения (для /status в боте)
        self.last_values: dict[str, float | None] = {}
        self.last_seen: datetime | None = None
        # parameter -> None | "above" | "below"
        self.breaches: dict[str, str | None] = {}

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name=f"poller-{self.chamber_id}")

    async def stop(self) -> None:
        self._running = False

        if self._task is not None:
            self._task.cancel()

            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while self._running:
            started = asyncio.get_running_loop().time()

            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Сюда попадают только наши собственные ошибки (например, БД недоступна),
                # ошибки связи с камерой обрабатываются внутри poll_once
                logger.exception("Chamber %s: poll cycle failed", self.chamber_id)

            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.0, settings.POLL_INTERVAL_MS / 1000 - elapsed))

    async def poll_once(self) -> dict:
        now = datetime.now(timezone.utc)

        try:
            values = await self.client.read_values()
        except Exception as exc:
            return await self._handle_failure(now, exc)

        self.failures = 0
        self.last_error = None
        self.temperature = values.get("temperature")
        self.last_values = dict(values)
        self.last_seen = now
        self._set_online(True)

        for parameter in active_parameters():
            value = values.get(parameter)
            if value is not None:
                self._check_threshold(parameter, value)

        await self._save(now, values, online=True)

        message = {
            "type": "measurement",
            "chamber_id": self.chamber_id,
            "ip": self.ip,
            "online": True,
            "recorded_at": now.isoformat(),
            "breaches": dict(self.breaches),
            **{k: _round(v) for k, v in values.items()},
        }
        await broadcaster.broadcast(message)
        return message

    async def _handle_failure(self, now: datetime, exc: Exception) -> dict:
        self.failures += 1
        self.last_error = str(exc) or exc.__class__.__name__
        logger.debug("Chamber %s (%s) poll failed: %s", self.chamber_id, self.ip, self.last_error)

        if self.failures >= settings.OFFLINE_AFTER_FAILURES:
            if self.online is not False:
                logger.warning("Chamber %s (%s) offline: %s", self.chamber_id, self.ip, self.last_error)
            self._set_online(False)
            self.temperature = None

        # Пока камера не признана офлайн (единичный сбой) — не пишем разрыв на график
        if self.online is False:
            await self._save(now, {}, online=False)

        message = {
            "type": "measurement",
            "chamber_id": self.chamber_id,
            "ip": self.ip,
            # Единичный сбой у камеры, которая была online, ещё не считается отключением
            "online": bool(self.online),
            "stale": True,
            "temperature": None,
            "humidity": None,
            "target_temperature": None,
            "target_humidity": None,
            "error": self.last_error,
            "recorded_at": now.isoformat(),
            "breaches": dict(self.breaches),
        }
        await broadcaster.broadcast(message)
        return message

    def _set_online(self, online: bool) -> None:
        previous = self.online
        self.online = online

        if previous is None:
            journal.info(
                "CHAMBER %s (%s) initial state: %s", self.chamber_id, self.ip, "online" if online else "offline"
            )
            return

        if previous == online:
            return

        if online:
            alert_service.publish(
                AlertType.CHAMBER_ONLINE,
                f"{self.name} ({self.ip}) снова на связи.",
                chamber_id=self.chamber_id,
                payload={"ip": self.ip},
            )
        else:
            alert_service.publish(
                AlertType.CHAMBER_OFFLINE,
                f"{self.name} ({self.ip}) не отвечает "
                f"({self.failures} попыток подряд). Ошибка: {self.last_error}",
                chamber_id=self.chamber_id,
                payload={"ip": self.ip, "error": self.last_error},
            )

    def _check_threshold(self, parameter: str, value: float) -> None:
        threshold = threshold_service.get(self.chamber_id, parameter)
        previous = self.breaches.get(parameter)
        state = evaluate(value, threshold, previous)
        self.breaches[parameter] = state

        if state == previous:
            return

        label, unit = PARAMETER_LABELS.get(parameter, (parameter, ""))
        payload = {
            "parameter": parameter,
            "value": round(value, 2),
            "min_value": threshold["min_value"] if threshold else None,
            "max_value": threshold["max_value"] if threshold else None,
            "direction": state or previous,
        }

        if state is not None:
            bound = threshold["max_value"] if state == "above" else threshold["min_value"]
            where = "выше верхней" if state == "above" else "ниже нижней"
            alert_service.publish(
                AlertType.THRESHOLD_BREACH,
                f"{self.name}: {label} {value:.2f} {unit} — {where} границы ({bound:g} {unit}).",
                chamber_id=self.chamber_id,
                payload=payload,
            )
        else:
            alert_service.publish(
                AlertType.THRESHOLD_RECOVERY,
                f"{self.name}: {label} вернулась в норму ({value:.2f} {unit}).",
                chamber_id=self.chamber_id,
                payload=payload,
            )

    async def _save(self, recorded_at: datetime, values: dict, online: bool) -> None:
        async with async_session() as session:
            session.add(
                Measurement(
                    chamber_id=self.chamber_id,
                    chamber_ip=self.ip,
                    temperature=values.get("temperature"),
                    humidity=values.get("humidity"),
                    target_temperature=values.get("target_temperature"),
                    target_humidity=values.get("target_humidity"),
                    online=online,
                    recorded_at=recorded_at,
                )
            )
            await session.commit()

    def status(self) -> dict:
        return {
            "chamber_id": self.chamber_id,
            "name": self.name,
            "ip": self.ip,
            "online": self.online,
            "temperature": _round(self.temperature),
            "humidity": _round(self.last_values.get("humidity")),
            "target_temperature": _round(self.last_values.get("target_temperature")),
            "target_humidity": _round(self.last_values.get("target_humidity")),
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "failures": self.failures,
            "last_error": self.last_error,
            "breaches": dict(self.breaches),
        }


class PollerManager:
    """
    Управляет пуллерами всех камер сразу — старт/стоп при запуске/остановке приложения.
    """

    def __init__(self) -> None:
        self.pollers: list[ChamberPoller] = []

    def start_all(self) -> None:
        for chamber_id, ip in settings.chambers_dict.items():
            poller = ChamberPoller(chamber_id, ip)
            poller.start()
            self.pollers.append(poller)

        logger.info("Started %d chamber pollers", len(self.pollers))

    async def stop_all(self) -> None:
        await asyncio.gather(*(p.stop() for p in self.pollers))
        self.pollers.clear()

    def status(self) -> list[dict]:
        return [p.status() for p in self.pollers]


poller_manager = PollerManager()
