import asyncio
import logging
from datetime import datetime, timezone

from app.config import settings
from app.modbus.client import FridgeClient
from app.models.db import Measurement, async_session
from app.services.broadcaster import broadcaster

logger = logging.getLogger("climatica.poller")


class ChamberPoller:
    """
    Опрашивает одну камеру в цикле и публикует результат.

    Аналог FridgeWorker(QThread) из оригинала:
      - там: QThread.run() -> emit(Signal) -> GUI + CSVLogger
      - здесь: asyncio task -> broadcaster.broadcast() (WS) + запись в Postgres
    """

    def __init__(self, chamber_id: int, ip: str):
        self.chamber_id = chamber_id
        self.ip = ip
        self.client = FridgeClient(chamber_id, ip)
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())

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
            now = datetime.now(timezone.utc)

            try:
                temperature = await self.client.read_temperature()

                await self._save(temperature, now, online=True)

                await broadcaster.broadcast({
                    "type": "measurement",
                    "chamber_id": self.chamber_id,
                    "ip": self.ip,
                    "temperature": round(temperature, 2),
                    "online": True,
                    "recorded_at": now.isoformat(),
                })

            except Exception as exc:
                logger.warning("Chamber %s (%s) offline: %s", self.chamber_id, self.ip, exc)

                await broadcaster.broadcast({
                    "type": "measurement",
                    "chamber_id": self.chamber_id,
                    "ip": self.ip,
                    "temperature": None,
                    "online": False,
                    "error": str(exc),
                    "recorded_at": now.isoformat(),
                })

            await asyncio.sleep(settings.POLL_INTERVAL_MS / 1000)

    async def _save(self, temperature: float, recorded_at: datetime, online: bool) -> None:
        async with async_session() as session:
            session.add(
                Measurement(
                    chamber_id=self.chamber_id,
                    chamber_ip=self.ip,
                    temperature=temperature,
                    online=online,
                    recorded_at=recorded_at,
                )
            )
            await session.commit()


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


poller_manager = PollerManager()
