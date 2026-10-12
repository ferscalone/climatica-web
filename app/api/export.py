import io
import math
import os
import re
from datetime import datetime
from uuid import uuid4

import pandas as pd
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from starlette.background import BackgroundTask

from app.config import settings
from app.models.db import Measurement, as_utc, async_session

router = APIRouter(prefix="/api")

# Сколько точек максимум отдаём на график. Если в срезе больше — прореживаем
# по временным корзинам, сохраняя минимум, максимум и разрывы связи.
MAX_CHART_POINTS = 4000

# Лимит строк на лист Excel — 1 048 576
XLSX_MAX_ROWS = 1_000_000

VALUE_FIELDS = ("temperature", "humidity", "target_temperature", "target_humidity")


def ensure_chamber(chamber_id: int) -> None:
    if chamber_id not in settings.chambers_dict:
        raise HTTPException(status_code=404, detail=f"Камера {chamber_id} не сконфигурирована")


def _serialize(row: Measurement) -> dict:
    return {
        "temperature": row.temperature,
        "humidity": row.humidity,
        "target_temperature": row.target_temperature,
        "target_humidity": row.target_humidity,
        "online": row.online,
        "recorded_at": as_utc(row.recorded_at).isoformat(),
    }


class _BucketSampler:
    """
    Прореживание для графика: ряд делится на корзины по bucket_size точек,
    из каждой берутся точки с минимальной и максимальной температурой
    (чтобы не терять пики) и первая точка без связи (чтобы не терять разрывы).
    """

    def __init__(self, bucket_size: int) -> None:
        self.bucket_size = bucket_size
        self.result: list[Measurement] = []
        self._bucket: list[Measurement] = []
        self.last: Measurement | None = None

    def add(self, row: Measurement) -> None:
        self.last = row
        self._bucket.append(row)
        if len(self._bucket) >= self.bucket_size:
            self._flush()

    def _flush(self) -> None:
        if not self._bucket:
            return

        online = [r for r in self._bucket if r.online and r.temperature is not None]
        picked: dict[int, Measurement] = {}

        if online:
            lo = min(online, key=lambda r: r.temperature)
            hi = max(online, key=lambda r: r.temperature)
            picked[lo.id] = lo
            picked[hi.id] = hi

        offline = next((r for r in self._bucket if not r.online), None)
        if offline is not None:
            picked[offline.id] = offline

        self.result.extend(sorted(picked.values(), key=lambda r: r.recorded_at))
        self._bucket = []

    def finish(self) -> list[Measurement]:
        self._flush()
        if self.last is not None and (not self.result or self.result[-1].id != self.last.id):
            self.result.append(self.last)
        return self.result


def _range_filters(chamber_id: int, date_from: datetime | None, date_to: datetime | None) -> list:
    filters = [Measurement.chamber_id == chamber_id]
    if date_from is not None:
        filters.append(Measurement.recorded_at >= date_from)
    if date_to is not None:
        filters.append(Measurement.recorded_at <= date_to)
    return filters


def _check_range(date_from: datetime | None, date_to: datetime | None) -> None:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=400, detail="Начало периода позже конца")


@router.get("/chambers")
async def list_chambers():
    """
    Список сконфигурированных камер — фронтенд использует это,
    чтобы нарисовать нужное число панелей без хардкода.
    """
    return [
        {
            "chamber_id": chamber_id,
            "name": settings.chamber_name(chamber_id),
            "ip": ip,
            "has_humidity": settings.HUMIDITY_REGISTER_INDEX >= 0,
            "has_target_temperature": settings.TARGET_TEMPERATURE_REGISTER_INDEX >= 0,
            "has_target_humidity": settings.TARGET_HUMIDITY_REGISTER_INDEX >= 0,
        }
        for chamber_id, ip in settings.chambers_dict.items()
    ]


@router.get("/history/{chamber_id}")
async def get_history(
    chamber_id: int,
    limit: int | None = Query(default=None, ge=1, le=50000),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
):
    """
    История измерений для графика.

    Без date_from/date_to — последние N точек (GRAPH_POINTS или limit).
    С периодом — только этот срез из БД; при большом объёме точки
    прореживаются по корзинам (минимум/максимум/разрывы сохраняются),
    чтобы браузер не завис. Полный срез без прореживания — через /api/export.
    """
    ensure_chamber(chamber_id)
    _check_range(date_from, date_to)

    ranged = date_from is not None or date_to is not None

    async with async_session() as session:
        if not ranged:
            result = await session.execute(
                select(Measurement)
                .where(Measurement.chamber_id == chamber_id)
                .order_by(Measurement.recorded_at.desc())
                .limit(limit or settings.GRAPH_POINTS)
            )
            rows = list(reversed(result.scalars().all()))
            total = len(rows)
        else:
            filters = _range_filters(chamber_id, date_from, date_to)

            total = await session.scalar(
                select(func.count()).select_from(Measurement).where(*filters)
            ) or 0

            stmt = select(Measurement).where(*filters).order_by(Measurement.recorded_at.asc())

            if total <= MAX_CHART_POINTS:
                rows = list((await session.execute(stmt)).scalars().all())
            else:
                # В каждой корзине до 3 точек (min, max, разрыв)
                sampler = _BucketSampler(math.ceil(total / (MAX_CHART_POINTS // 3)))
                stream = await session.stream_scalars(stmt.execution_options(yield_per=5000))
                async for row in stream:
                    sampler.add(row)
                rows = sampler.finish()

    return {
        "points": [_serialize(row) for row in rows],
        "total": total,
        "returned": len(rows),
        "downsampled": len(rows) < total,
    }


def _export_columns(rows: list[Measurement]) -> list[str]:
    """Колонки влажности/целевых значений добавляются, только если есть данные."""
    columns = ["temperature"]
    for field in ("humidity", "target_temperature", "target_humidity"):
        if any(getattr(r, field) is not None for r in rows):
            columns.append(field)
    return columns


EXPORT_HEADERS = {
    "temperature": "Temperature",
    "humidity": "Humidity",
    "target_temperature": "TargetTemperature",
    "target_humidity": "TargetHumidity",
}


def dataframe_response(df: pd.DataFrame, fmt: str, filename: str, sheet: str) -> FileResponse:
    """
    Сохраняет DataFrame во временный файл и отдаёт его; файл удаляется
    после отправки (раньше выгрузки копились в EXPORT_DIR бесконечно).
    """
    path = settings.EXPORT_DIR / f"{uuid4().hex}.{fmt}"

    if fmt == "csv":
        # utf-8-sig — чтобы Excel сразу правильно показывал кириллицу
        df.to_csv(path, index=False, encoding="utf-8-sig")
        media_type = "text/csv"
    else:
        df.to_excel(path, index=False, sheet_name=sheet)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    return FileResponse(
        path=path,
        media_type=media_type,
        filename=filename,
        background=BackgroundTask(os.remove, path),
    )


def period_suffix(date_from: datetime | None, date_to: datetime | None) -> str:
    def fmt(dt: datetime | None, default: str) -> str:
        return as_utc(dt).astimezone(settings.tz).strftime("%Y%m%d-%H%M") if dt else default

    if date_from is None and date_to is None:
        return "all"
    return f"{fmt(date_from, 'start')}_{fmt(date_to, 'now')}"


@router.get("/export/{chamber_id}")
async def export_chamber(
    chamber_id: int,
    format: str = Query(default="csv", pattern="^(csv|xlsx)$"),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
):
    """
    Выгрузка измерений камеры за период (или за всё время) в CSV/Excel —
    по требованию, без прореживания. Время — в часовом поясе TIMEZONE.
    """
    ensure_chamber(chamber_id)
    _check_range(date_from, date_to)

    async with async_session() as session:
        result = await session.execute(
            select(Measurement)
            .where(*_range_filters(chamber_id, date_from, date_to))
            .order_by(Measurement.recorded_at.asc())
        )
        rows = result.scalars().all()

    if not rows:
        raise HTTPException(status_code=404, detail="Нет данных за выбранный период")

    if format == "xlsx" and len(rows) > XLSX_MAX_ROWS:
        raise HTTPException(
            status_code=400,
            detail=f"В срезе {len(rows)} строк — это больше лимита Excel. Выберите период короче или CSV.",
        )

    columns = _export_columns(rows)
    tz = settings.tz
    records = []

    for row in rows:
        local = as_utc(row.recorded_at).astimezone(tz)
        record = {"Date": local.strftime("%Y-%m-%d"), "Time": local.strftime("%H:%M:%S")}
        for field in columns:
            value = getattr(row, field)
            record[EXPORT_HEADERS[field]] = None if value is None else round(value, 2)
        record["Online"] = row.online
        records.append(record)

    return dataframe_response(
        pd.DataFrame(records),
        format,
        f"climatica_chamber_{chamber_id}_{period_suffix(date_from, date_to)}.{format}",
        sheet="Measurements",
    )


# ---------------------------------------------------------------------------
# Импорт ранее сохранённого CSV/Excel для показа на графике (в БД не пишется)
# ---------------------------------------------------------------------------

_COLUMN_ALIASES = {
    "date": {"date", "дата"},
    "time": {"time", "время"},
    "datetime": {"datetime", "recorded_at", "recordedat", "timestamp", "датавремя", "дата/время"},
    "temperature": {"temperature", "temp", "c", "температура", "t"},
    "humidity": {"humidity", "hum", "rh", "влажность"},
    "target_temperature": {"targettemperature", "target_temperature", "settemperature", "целеваятемпература", "уставкатемпературы"},
    "target_humidity": {"targethumidity", "target_humidity", "sethumidity", "целеваявлажность", "уставкавлажности"},
    "online": {"online", "онлайн", "связь"},
}


def _normalize(name) -> str:
    # "Температура, °C" / "Temperature (C)" -> "температура" / "temperature"
    key = re.split(r"[,(\[]", str(name))[0].strip().lower()
    for ch in ("°", "%", ",", "(", ")", " ", "-"):
        key = key.replace(ch, "")
    return key


def _colmap(df: pd.DataFrame) -> dict[str, str]:
    """Точное сопоставление нормализованных названий колонок (без поиска подстрок)."""
    mapping: dict[str, str] = {}
    for col in df.columns:
        key = _normalize(col)
        for field, aliases in _COLUMN_ALIASES.items():
            normalized = {_normalize(a) for a in aliases}
            if key in normalized and field not in mapping:
                mapping[field] = col
    return mapping


def _to_float(value) -> float | None:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _parse_online(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "online", "да", "истина"}
    if value is None or pd.isna(value):
        return True
    return bool(value)


def _row_timestamp(row, mapping: dict[str, str]):
    if "datetime" in mapping:
        return pd.to_datetime(row[mapping["datetime"]], errors="coerce")

    if "date" in mapping and "time" in mapping:
        date_ts = pd.to_datetime(row[mapping["date"]], errors="coerce", dayfirst=False)
        if pd.isna(date_ts):
            return None
        return pd.to_datetime(f"{date_ts.strftime('%Y-%m-%d')} {row[mapping['time']]}", errors="coerce")

    if "date" in mapping:
        return pd.to_datetime(row[mapping["date"]], errors="coerce")

    return None


def parse_imported(df: pd.DataFrame) -> list[dict]:
    df = df.dropna(how="all")
    mapping = _colmap(df)

    if "temperature" not in mapping:
        raise HTTPException(status_code=400, detail="В файле нет колонки Temperature / Температура")

    if not {"datetime", "date"} & mapping.keys():
        raise HTTPException(status_code=400, detail="В файле нет колонок с датой/временем (Date+Time или datetime)")

    tz = settings.tz
    points: list[dict] = []

    for _, row in df.iterrows():
        ts = _row_timestamp(row, mapping)
        if ts is None or pd.isna(ts):
            continue

        ts = ts.to_pydatetime()
        # Время без пояса считаем временем TIMEZONE — так пишет наш собственный экспорт
        ts = ts.replace(tzinfo=tz) if ts.tzinfo is None else ts

        online = _parse_online(row[mapping["online"]]) if "online" in mapping else True
        values = {f: _to_float(row[mapping[f]]) if f in mapping else None for f in VALUE_FIELDS}

        if online and values["temperature"] is None:
            continue

        points.append({**values, "online": online, "recorded_at": ts.isoformat()})

    if not points:
        raise HTTPException(
            status_code=400,
            detail="Не удалось прочитать ни одной точки (нужны Date+Time или datetime и Temperature)",
        )

    points.sort(key=lambda p: p["recorded_at"])
    return points


def _downsample_dicts(points: list[dict], max_points: int) -> list[dict]:
    n = len(points)
    if n <= max_points:
        return points
    step = n / max_points
    picked = [points[min(n - 1, int(i * step))] for i in range(max_points)]
    picked[-1] = points[-1]
    return picked


@router.post("/import")
async def import_file(file: UploadFile = File(...)):
    """
    Разбор ранее сохранённого CSV/Excel и возврат точек для графика.
    В БД ничего не пишется — только отображение в интерфейсе.
    """
    name = (file.filename or "import").lower()
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Файл пустой")

    try:
        if name.endswith(".csv"):
            text = raw.decode("utf-8-sig", errors="replace")
            sep = ";" if text.count(";") > text.count(",") else ","
            df = pd.read_csv(io.StringIO(text), sep=sep)
        elif name.endswith(".xlsx"):
            df = pd.read_excel(io.BytesIO(raw))
        else:
            raise HTTPException(status_code=400, detail="Поддерживаются только .csv и .xlsx")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Не удалось прочитать файл: {exc}") from exc

    points = parse_imported(df)
    sampled = _downsample_dicts(points, MAX_CHART_POINTS)
    return {
        "filename": file.filename,
        "points": sampled,
        "total": len(points),
        "returned": len(sampled),
        "downsampled": len(sampled) < len(points),
        "columns": sorted(k for k in VALUE_FIELDS if any(p[k] is not None for p in points)),
    }
