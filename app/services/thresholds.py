"""
Граничные значения (пороги).

Пороги хранятся в БД (таблица thresholds) и кэшируются в памяти: поллер
проверяет каждое измерение по кэшу, не обращаясь к БД раз в секунду.
Изменить порог можно только через API сайта — каждое изменение пишется
в audit_log, в файловый журнал и публикуется как алерт threshold_changed.
"""

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select

from app.config import settings
from app.logging_setup import journal
from app.models.db import (
    PARAMETER_LABELS,
    AlertType,
    AuditLog,
    Threshold,
    ThresholdParameter,
    as_utc,
    async_session,
    utcnow,
)
from app.services.alerts import alert_service

logger = logging.getLogger("climatica.thresholds")


class ThresholdError(ValueError):
    pass


@dataclass
class ActorInfo:
    name: str | None = None
    ip: str | None = None
    host: str | None = None
    user_agent: str | None = None
    comment: str | None = None

    def describe(self) -> str:
        who = self.name or "без имени"
        where = self.ip or "неизвестный IP"
        if self.host and self.host != self.ip:
            where += f", компьютер {self.host}"
        return f"{who} ({where})"


def threshold_to_dict(t: Threshold) -> dict:
    label, unit = PARAMETER_LABELS.get(t.parameter, (t.parameter, ""))
    return {
        "chamber_id": t.chamber_id,
        "chamber_name": settings.chamber_name(t.chamber_id),
        "parameter": t.parameter,
        "label": label,
        "unit": unit,
        "min_value": t.min_value,
        "max_value": t.max_value,
        "enabled": t.enabled,
        "updated_at": as_utc(t.updated_at).isoformat() if t.updated_at else None,
        "updated_by": t.updated_by,
    }


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:g}"


def _fmt_range(d: dict) -> str:
    text = f"{_fmt(d['min_value'])}…{_fmt(d['max_value'])}"
    return text if d["enabled"] else text + " (проверка выключена)"


def active_parameters() -> list[str]:
    params = [ThresholdParameter.TEMPERATURE.value]
    if settings.humidity_enabled:
        params.append(ThresholdParameter.HUMIDITY.value)
    return params


class ThresholdService:
    def __init__(self) -> None:
        self._cache: dict[tuple[int, str], dict] = {}
        self._lock = asyncio.Lock()

    def _defaults(self, parameter: str) -> tuple[float, float]:
        if parameter == ThresholdParameter.HUMIDITY.value:
            return settings.DEFAULT_HUMIDITY_MIN, settings.DEFAULT_HUMIDITY_MAX
        return settings.DEFAULT_TEMPERATURE_MIN, settings.DEFAULT_TEMPERATURE_MAX

    async def load(self) -> None:
        """Читает пороги из БД и создаёт значения по умолчанию для новых камер."""
        self._lock = asyncio.Lock()
        async with async_session() as session:
            existing = {
                (t.chamber_id, t.parameter): t
                for t in (await session.execute(select(Threshold))).scalars().all()
            }

            for chamber_id in settings.chambers_dict:
                for parameter in ThresholdParameter:
                    key = (chamber_id, parameter.value)
                    if key not in existing:
                        lo, hi = self._defaults(parameter.value)
                        t = Threshold(
                            chamber_id=chamber_id,
                            parameter=parameter.value,
                            min_value=lo,
                            max_value=hi,
                            enabled=True,
                            updated_by="по умолчанию",
                        )
                        session.add(t)
                        existing[key] = t

            await session.commit()

        self._cache = {key: threshold_to_dict(t) for key, t in existing.items()}
        logger.info("Loaded %d thresholds", len(self._cache))

    def get(self, chamber_id: int, parameter: str) -> dict | None:
        return self._cache.get((chamber_id, parameter))

    def list(self, chamber_id: int | None = None) -> list[dict]:
        params = active_parameters()
        items = [
            v for (cid, param), v in self._cache.items()
            if param in params and (chamber_id is None or cid == chamber_id)
        ]
        return sorted(items, key=lambda d: (d["chamber_id"], params.index(d["parameter"])))

    @staticmethod
    def validate(chamber_id: int, parameter: str, min_value, max_value) -> None:
        if chamber_id not in settings.chambers_dict:
            raise ThresholdError(f"Камера {chamber_id} не сконфигурирована")

        if parameter not in {p.value for p in ThresholdParameter}:
            raise ThresholdError(f"Неизвестный параметр {parameter!r}")

        if min_value is None and max_value is None:
            raise ThresholdError("Нужно задать хотя бы одну границу")

        if min_value is not None and max_value is not None and min_value >= max_value:
            raise ThresholdError("Нижняя граница должна быть меньше верхней")

    async def update(
        self,
        chamber_id: int,
        parameter: str,
        min_value: float | None,
        max_value: float | None,
        enabled: bool,
        actor: ActorInfo,
    ) -> dict:
        self.validate(chamber_id, parameter, min_value, max_value)

        async with self._lock:
            async with async_session() as session:
                result = await session.execute(
                    select(Threshold).where(
                        Threshold.chamber_id == chamber_id,
                        Threshold.parameter == parameter,
                    )
                )
                t = result.scalar_one_or_none()

                if t is None:
                    t = Threshold(chamber_id=chamber_id, parameter=parameter)
                    session.add(t)
                    old = None
                else:
                    old = threshold_to_dict(t)

                t.min_value = min_value
                t.max_value = max_value
                t.enabled = enabled
                t.updated_at = utcnow()
                t.updated_by = actor.name or actor.ip

                await session.flush()
                new = threshold_to_dict(t)

                old_short = None if old is None else {
                    k: old[k] for k in ("min_value", "max_value", "enabled")
                }
                new_short = {k: new[k] for k in ("min_value", "max_value", "enabled")}

                session.add(
                    AuditLog(
                        action="threshold_changed",
                        chamber_id=chamber_id,
                        parameter=parameter,
                        actor_name=actor.name,
                        actor_ip=actor.ip,
                        actor_host=actor.host,
                        user_agent=(actor.user_agent or "")[:512] or None,
                        old_value=old_short,
                        new_value=new_short,
                        comment=actor.comment,
                    )
                )
                await session.commit()

            self._cache[(chamber_id, parameter)] = new

        label, unit = PARAMETER_LABELS.get(parameter, (parameter, ""))
        before = _fmt_range(old) if old else "не задано"
        message = (
            f"{settings.chamber_name(chamber_id)}: изменены границы ({label}, {unit}) "
            f"{before} → {_fmt_range(new)}. Изменил: {actor.describe()}."
        )
        if actor.comment:
            message += f" Комментарий: {actor.comment}"

        journal.info(
            "THRESHOLD CHANGED chamber=%s parameter=%s %s -> %s by name=%r ip=%s host=%s comment=%r",
            chamber_id, parameter, old_short, new_short, actor.name, actor.ip, actor.host, actor.comment,
        )

        alert_service.publish(
            AlertType.THRESHOLD_CHANGED,
            message,
            chamber_id=chamber_id,
            payload={
                "parameter": parameter,
                "old": old_short,
                "new": new_short,
                "actor_name": actor.name,
                "actor_ip": actor.ip,
                "actor_host": actor.host,
                "comment": actor.comment,
            },
        )

        return new


def evaluate(value: float, threshold: dict | None, was_breached: str | None) -> str | None:
    """
    Возвращает состояние: None (в норме), "above" или "below".

    Гистерезис: выйти за границу можно сразу, а вернуться в норму — только
    отступив от границы на THRESHOLD_HYSTERESIS. Иначе значение, дрожащее
    около границы, слало бы breach/recovery на каждом опросе.
    """
    if threshold is None or not threshold["enabled"]:
        return None

    lo, hi = threshold["min_value"], threshold["max_value"]
    h = settings.THRESHOLD_HYSTERESIS

    if hi is not None and value > hi:
        return "above"
    if lo is not None and value < lo:
        return "below"

    if was_breached == "above" and hi is not None and value > hi - h:
        return "above"
    if was_breached == "below" and lo is not None and value < lo + h:
        return "below"

    return None


threshold_service = ThresholdService()
