from datetime import datetime
from uuid import uuid4

import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.config import settings
from app.models.db import Measurement, async_session

router = APIRouter(prefix="/api")


@router.get("/chambers")
async def list_chambers():
    """
    Список сконфигурированных камер — фронтенд использует это,
    чтобы нарисовать нужное число панелей без хардкода.
    """
    return [
        {"chamber_id": chamber_id, "ip": ip}
        for chamber_id, ip in settings.chambers_dict.items()
    ]


@router.get("/history/{chamber_id}")
async def get_history(
    chamber_id: int,
    limit: int = Query(default=None, ge=1, le=10000),
):
    """
    Последние N измерений камеры (по умолчанию settings.GRAPH_POINTS) —
    используется фронтендом, чтобы отрисовать график сразу при загрузке
    страницы, до того как придут новые точки по WebSocket.
    """
    limit = limit or settings.GRAPH_POINTS

    async with async_session() as session:
        result = await session.execute(
            select(Measurement)
            .where(Measurement.chamber_id == chamber_id)
            .order_by(Measurement.recorded_at.desc())
            .limit(limit)
        )

        rows = list(reversed(result.scalars().all()))

    return [
        {
            "temperature": row.temperature,
            "online": row.online,
            "recorded_at": row.recorded_at.isoformat(),
        }
        for row in rows
    ]


def _fetch_rows(chamber_id: int, date_from: datetime | None, date_to: datetime | None):
    stmt = select(Measurement).where(Measurement.chamber_id == chamber_id)

    if date_from is not None:
        stmt = stmt.where(Measurement.recorded_at >= date_from)

    if date_to is not None:
        stmt = stmt.where(Measurement.recorded_at <= date_to)

    return stmt.order_by(Measurement.recorded_at.asc())


@router.get("/export/{chamber_id}")
async def export_chamber(
    chamber_id: int,
    format: str = Query(default="csv", pattern="^(csv|xlsx)$"),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
):
    """
    Экспорт истории измерений камеры в CSV или Excel — по требованию,
    а не автозаписью на каждое измерение (в отличие от оригинального
    CSVLogger, который дозаписывал файл на каждый poll).
    """
    if chamber_id not in settings.chambers_dict:
        raise HTTPException(status_code=404, detail="Unknown chamber_id")

    async with async_session() as session:
        result = await session.execute(_fetch_rows(chamber_id, date_from, date_to))
        rows = result.scalars().all()

    if not rows:
        raise HTTPException(status_code=404, detail="No data for given range")

    df = pd.DataFrame([
        {
            "Date": row.recorded_at.strftime("%Y-%m-%d"),
            "Time": row.recorded_at.strftime("%H:%M:%S"),
            "Temperature": round(row.temperature, 2),
            "Online": row.online,
        }
        for row in rows
    ])

    filename = f"chamber_{chamber_id}_{uuid4().hex[:8]}.{format}"
    filepath = settings.EXPORT_DIR / filename

    if format == "csv":
        df.to_csv(filepath, index=False, encoding="utf-8")
        media_type = "text/csv"
    else:
        df.to_excel(filepath, index=False, sheet_name="Temperature")
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    return FileResponse(
        path=filepath,
        media_type=media_type,
        filename=f"climatica_chamber_{chamber_id}.{format}",
    )
