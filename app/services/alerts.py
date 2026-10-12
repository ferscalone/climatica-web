"""
Пул уведомлений.

Любая часть backend'а (поллер, API порогов) вызывает alert_service.publish(...).
Событие кладётся в asyncio.Queue, а фоновый диспетчер:
  1) сохраняет его в БД (alert_events) и в файловый журнал;
  2) сразу рассылает во все открытые вкладки браузера (WS, тип "alert");
  3) доставляет во внешние каналы (сейчас — Telegram-бот по HTTP).
Если канал недоступен, событие остаётся в БД с delivered=False, и retry-цикл
дошлёт его позже (до ALERT_MAX_ATTEMPTS попыток).

Новый канал (email, SMS, ...) = новый класс с методом send(); ядро не меняется.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from sqlalchemy import select

from app.config import settings
from app.logging_setup import journal
from app.models.db import AlertEvent, AlertType, as_utc, async_session, utcnow
from app.services.broadcaster import broadcaster

logger = logging.getLogger("climatica.alerts")

SEVERITY = {
    AlertType.CHAMBER_OFFLINE: "critical",
    AlertType.CHAMBER_ONLINE: "info",
    AlertType.THRESHOLD_BREACH: "warning",
    AlertType.THRESHOLD_RECOVERY: "info",
    AlertType.THRESHOLD_CHANGED: "info",
    AlertType.DELIVERY_FAILED: "warning",
    AlertType.SYSTEM: "info",
}

# Параллельно доставляем не больше стольких событий за раз при retry
_RETRY_CONCURRENCY = 5


@dataclass
class AlertMessage:
    alert_type: AlertType
    message: str
    chamber_id: int | None = None
    payload: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
    # Только журнал + браузер, без Telegram (служебные сообщения о самой доставке)
    local_only: bool = False


def event_to_dict(event: AlertEvent) -> dict:
    return {
        "id": event.id,
        "created_at": as_utc(event.created_at).isoformat(),
        "alert_type": event.alert_type,
        "severity": event.severity,
        "chamber_id": event.chamber_id,
        "message": event.message,
        "payload": event.payload or {},
        "delivered": event.delivered,
        "delivery_attempts": event.delivery_attempts,
        "delivered_at": as_utc(event.delivered_at).isoformat() if event.delivered_at else None,
        "last_error": event.last_error,
    }


class TelegramBotChannel:
    """Доставка в Telegram через внутренний HTTP-эндпоинт бота."""

    name = "telegram"

    def __init__(self, base_url: str) -> None:
        self.url = base_url.rstrip("/") + "/internal/send"

    async def send(self, event: dict) -> None:
        headers = {}
        if settings.INTERNAL_API_TOKEN:
            headers["X-Internal-Token"] = settings.INTERNAL_API_TOKEN

        async with httpx.AsyncClient(timeout=settings.ALERT_DELIVERY_TIMEOUT) as client:
            response = await client.post(self.url, json=event, headers=headers)

        if response.status_code >= 300:
            raise RuntimeError(f"бот ответил HTTP {response.status_code}: {response.text[:200]}")


class AlertService:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[AlertMessage] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self.channels: list = []

    def configure_channels(self) -> None:
        self.channels = []
        if settings.TELEGRAM_BOT_INTERNAL_URL:
            self.channels.append(TelegramBotChannel(settings.TELEGRAM_BOT_INTERNAL_URL))

    def start(self) -> None:
        # Очередь создаётся здесь, а не в __init__: asyncio-примитивы привязываются
        # к event loop, в котором запущено приложение
        self._queue = asyncio.Queue()
        self.configure_channels()
        self._tasks = [
            asyncio.create_task(self._dispatcher_loop(), name="alerts-dispatcher"),
            asyncio.create_task(self._retry_loop(), name="alerts-retry"),
        ]
        logger.info("Alert service started, channels=%s", [c.name for c in self.channels] or "none")

    async def stop(self) -> None:
        # Даём диспетчеру дописать уже поставленные в очередь события
        try:
            await asyncio.wait_for(self._queue.join(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("Alert queue not drained on shutdown (%d left)", self._queue.qsize())

        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    def publish(
        self,
        alert_type: AlertType,
        message: str,
        chamber_id: int | None = None,
        payload: dict | None = None,
        local_only: bool = False,
    ) -> None:
        """Неблокирующая постановка события в пул."""
        self._queue.put_nowait(
            AlertMessage(
                alert_type=alert_type,
                message=message,
                chamber_id=chamber_id,
                payload=payload or {},
                local_only=local_only,
            )
        )

    async def _dispatcher_loop(self) -> None:
        while True:
            alert = await self._queue.get()
            try:
                await self._process(alert)
            except Exception:
                logger.exception("Failed to process alert %s", alert)
            finally:
                self._queue.task_done()

    async def _process(self, alert: AlertMessage) -> None:
        no_channels = alert.local_only or not self.channels

        async with async_session() as session:
            event = AlertEvent(
                created_at=alert.created_at,
                alert_type=alert.alert_type.value,
                severity=SEVERITY.get(alert.alert_type, "info"),
                chamber_id=alert.chamber_id,
                message=alert.message,
                payload=alert.payload,
                # Некуда доставлять — считаем событие обработанным, чтобы retry его не трогал
                delivered=no_channels,
                delivered_at=alert.created_at if no_channels else None,
            )
            session.add(event)
            await session.commit()
            data = event_to_dict(event)

        journal.info(
            "ALERT #%s %s chamber=%s severity=%s: %s",
            data["id"], data["alert_type"], data["chamber_id"], data["severity"], data["message"],
        )

        # Браузер получает событие сразу, не дожидаясь Telegram
        await broadcaster.broadcast({"type": "alert", **data})

        if not no_channels:
            await self._try_deliver(data["id"])

    async def _try_deliver(self, event_id: int) -> bool:
        async with async_session() as session:
            event = await session.get(AlertEvent, event_id)
            if event is None or event.delivered:
                return True

            data = event_to_dict(event)
            errors = []

            for channel in self.channels:
                try:
                    await channel.send(data)
                except Exception as exc:
                    errors.append(f"{channel.name}: {exc!r}")

            event.delivery_attempts += 1

            if not errors:
                event.delivered = True
                event.delivered_at = utcnow()
                event.last_error = None
                journal.info("DELIVERED alert #%s (attempt %s)", event.id, event.delivery_attempts)
            else:
                event.last_error = "; ".join(errors)[:2000]
                logger.warning("Delivery of alert #%s failed: %s", event.id, event.last_error)

                if event.delivery_attempts >= settings.ALERT_MAX_ATTEMPTS:
                    journal.error(
                        "DELIVERY FAILED alert #%s after %s attempts: %s",
                        event.id, event.delivery_attempts, event.last_error,
                    )
                    # Сообщаем хотя бы в браузер/журнал, что Telegram не получил событие
                    self.publish(
                        AlertType.DELIVERY_FAILED,
                        f"Не удалось доставить уведомление #{event.id} в Telegram "
                        f"после {event.delivery_attempts} попыток: {event.message}",
                        chamber_id=event.chamber_id,
                        payload={"failed_event_id": event.id, "error": event.last_error},
                        local_only=True,
                    )

            await session.commit()
            return not errors

    async def _retry_loop(self) -> None:
        semaphore = asyncio.Semaphore(_RETRY_CONCURRENCY)

        async def limited(event_id: int) -> None:
            async with semaphore:
                await self._try_deliver(event_id)

        while True:
            await asyncio.sleep(settings.ALERT_RETRY_INTERVAL)

            if not self.channels:
                continue

            try:
                async with async_session() as session:
                    result = await session.execute(
                        select(AlertEvent.id)
                        .where(
                            AlertEvent.delivered.is_(False),
                            AlertEvent.delivery_attempts < settings.ALERT_MAX_ATTEMPTS,
                        )
                        .order_by(AlertEvent.created_at.asc())
                        .limit(50)
                    )
                    pending = list(result.scalars().all())

                if pending:
                    logger.info("Retrying delivery of %d alerts", len(pending))
                    await asyncio.gather(*(limited(i) for i in pending))
            except Exception:
                logger.exception("Alert retry loop error")


alert_service = AlertService()
