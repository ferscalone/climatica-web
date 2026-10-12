"""
Журналы: пул уведомлений (alert_events) и действия пользователей (audit_log).
Просмотр и выгрузка за период (CSV/XLSX), плюс копия всего файлового журнала.
"""

from datetime import datetime

import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.export import _check_range, dataframe_response, period_suffix
from app.config import settings
from app.logging_setup import journal_files
from app.models.db import AlertEvent, AuditLog, as_utc, async_session
from app.services.alerts import event_to_dict
from app.services.broadcaster import broadcaster
from app.services.poller import poller_manager

router = APIRouter(prefix="/api")


def audit_to_dict(row: AuditLog) -> dict:
    return {
        "id": row.id,
        "created_at": as_utc(row.created_at).isoformat(),
        "action": row.action,
        "chamber_id": row.chamber_id,
        "parameter": row.parameter,
        "actor_name": row.actor_name,
        "actor_ip": row.actor_ip,
        "actor_host": row.actor_host,
        "user_agent": row.user_agent,
        "old_value": row.old_value,
        "new_value": row.new_value,
        "comment": row.comment,
    }


async def _fetch(model, date_from, date_to, chamber_id=None, extra=None, limit=None, newest_first=True):
    _check_range(date_from, date_to)
    stmt = select(model)
    if date_from is not None:
        stmt = stmt.where(model.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(model.created_at <= date_to)
    if chamber_id is not None:
        stmt = stmt.where(model.chamber_id == chamber_id)
    if extra is not None:
        stmt = stmt.where(extra)
    order = model.created_at.desc() if newest_first else model.created_at.asc()
    stmt = stmt.order_by(order, model.id.desc() if newest_first else model.id.asc())
    if limit:
        stmt = stmt.limit(limit)
    async with async_session() as session:
        return list((await session.execute(stmt)).scalars().all())


def _local(dt: datetime | None) -> str:
    return as_utc(dt).astimezone(settings.tz).strftime("%Y-%m-%d %H:%M:%S") if dt else ""


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "ws_clients": broadcaster.count,
        "chambers": poller_manager.status(),
    }


@router.get("/alerts")
async def list_alerts(
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    chamber_id: int | None = None,
    alert_type: str | None = None,
    limit: int = Query(default=200, ge=1, le=5000),
):
    extra = AlertEvent.alert_type == alert_type if alert_type else None
    rows = await _fetch(AlertEvent, date_from, date_to, chamber_id, extra, limit)
    return [event_to_dict(r) for r in rows]


@router.get("/alerts/export")
async def export_alerts(
    format: str = Query(default="csv", pattern="^(csv|xlsx)$"),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    chamber_id: int | None = None,
):
    rows = await _fetch(AlertEvent, date_from, date_to, chamber_id, newest_first=False)
    if not rows:
        raise HTTPException(status_code=404, detail="Нет уведомлений за выбранный период")

    df = pd.DataFrame([
        {
            "ID": r.id,
            "Время": _local(r.created_at),
            "Тип": r.alert_type,
            "Важность": r.severity,
            "Камера": settings.chamber_name(r.chamber_id),
            "Сообщение": r.message,
            "Доставлено": r.delivered,
            "Попыток доставки": r.delivery_attempts,
            "Время доставки": _local(r.delivered_at),
            "Ошибка доставки": r.last_error or "",
        }
        for r in rows
    ])
    return dataframe_response(
        df, format, f"climatica_alerts_{period_suffix(date_from, date_to)}.{format}", sheet="Alerts"
    )


@router.get("/audit-log")
async def list_audit(
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    chamber_id: int | None = None,
    limit: int = Query(default=200, ge=1, le=5000),
):
    rows = await _fetch(AuditLog, date_from, date_to, chamber_id, limit=limit)
    return [audit_to_dict(r) for r in rows]


def _range_text(value: dict | None) -> str:
    if not value:
        return ""
    text = f"{value.get('min_value')}…{value.get('max_value')}"
    return text if value.get("enabled", True) else text + " (выкл)"


@router.get("/audit-log/export")
async def export_audit(
    format: str = Query(default="csv", pattern="^(csv|xlsx)$"),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    chamber_id: int | None = None,
):
    rows = await _fetch(AuditLog, date_from, date_to, chamber_id, newest_first=False)
    if not rows:
        raise HTTPException(status_code=404, detail="Нет действий за выбранный период")

    df = pd.DataFrame([
        {
            "ID": r.id,
            "Время": _local(r.created_at),
            "Действие": r.action,
            "Камера": settings.chamber_name(r.chamber_id),
            "Параметр": r.parameter,
            "Было": _range_text(r.old_value),
            "Стало": _range_text(r.new_value),
            "Имя": r.actor_name or "",
            "IP": r.actor_ip or "",
            "Компьютер": r.actor_host or "",
            "Комментарий": r.comment or "",
            "Браузер": r.user_agent or "",
        }
        for r in rows
    ])
    return dataframe_response(
        df, format, f"climatica_audit_{period_suffix(date_from, date_to)}.{format}", sheet="Audit"
    )


@router.get("/journal/download")
async def download_journal():
    """Копия всего файлового журнала (все ротированные части подряд, от старых к новым)."""
    files = journal_files()
    if not files:
        raise HTTPException(status_code=404, detail="Журнал пока пуст")

    def stream():
        for path in files:
            with open(path, "rb") as fh:
                while chunk := fh.read(64 * 1024):
                    yield chunk

    stamp = datetime.now(settings.tz).strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        stream(),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="climatica_journal_{stamp}.log"'},
    )
