import enum
import logging
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from app.config import settings

logger = logging.getLogger("climatica.db")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite (в тестах) возвращает naive datetime — в БД всё хранится в UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class Base(DeclarativeBase):
    pass


class ThresholdParameter(str, enum.Enum):
    """
    Параметры, для которых можно задать граничные значения.
    Новый параметр = новое значение enum, схема БД не меняется.
    """

    TEMPERATURE = "temperature"
    HUMIDITY = "humidity"


PARAMETER_LABELS = {
    ThresholdParameter.TEMPERATURE.value: ("температура", "°C"),
    ThresholdParameter.HUMIDITY.value: ("влажность", "%"),
}


class AlertType(str, enum.Enum):
    CHAMBER_OFFLINE = "chamber_offline"
    CHAMBER_ONLINE = "chamber_online"
    THRESHOLD_BREACH = "threshold_breach"
    THRESHOLD_RECOVERY = "threshold_recovery"
    THRESHOLD_CHANGED = "threshold_changed"
    DELIVERY_FAILED = "delivery_failed"
    SYSTEM = "system"


class Measurement(Base):
    """
    Одно измерение одной камеры.
    Аналог строки в CSV/Excel из оригинального проекта.
    При отсутствии связи тоже пишется строка (online=False, значения NULL) —
    так разрывы связи видны на графике и в выгрузке.
    """

    __tablename__ = "measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    chamber_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chamber_ip: Mapped[str] = mapped_column(String(64), nullable=False)

    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_humidity: Mapped[float | None] = mapped_column(Float, nullable=True)
    online: Mapped[bool] = mapped_column(default=True, nullable=False)

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("ix_measurements_chamber_time", "chamber_id", "recorded_at"),
    )


class Threshold(Base):
    """Граничные значения параметра для одной камеры."""

    __tablename__ = "thresholds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chamber_id: Mapped[int] = mapped_column(Integer, nullable=False)
    parameter: Mapped[str] = mapped_column(String(32), nullable=False)
    min_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        UniqueConstraint("chamber_id", "parameter", name="uq_threshold_chamber_parameter"),
    )


class AlertEvent(Base):
    """
    Пул уведомлений: каждое событие, которое нужно разослать адресатам.
    delivered/attempts — состояние доставки во внешние каналы (Telegram).
    """

    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    alert_type: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    chamber_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    delivered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_alert_events_created", "created_at"),
        Index("ix_alert_events_pending", "delivered", "created_at"),
    )


class AuditLog(Base):
    """Журнал действий пользователей (сейчас — изменения порогов)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    chamber_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parameter: Mapped[str | None] = mapped_column(String(32), nullable=True)

    actor_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)

    old_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_audit_log_created", "created_at"),)


if settings.DATABASE_URL.startswith("sqlite"):
    # SQLite используется только в тестах/локальной отладке; без пула соединения
    # не переносятся между event loop'ами (TestClient запускает свой loop)
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
else:
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)

async_session = async_sessionmaker(engine, expire_on_commit=False)


# create_all не меняет уже существующие таблицы. Базы, созданные первой версией
# MVP (только temperature NOT NULL), доводим до актуальной схемы этими командами.
_POSTGRES_UPGRADES = [
    "ALTER TABLE measurements ALTER COLUMN temperature DROP NOT NULL",
    "ALTER TABLE measurements ADD COLUMN IF NOT EXISTS humidity DOUBLE PRECISION",
    "ALTER TABLE measurements ADD COLUMN IF NOT EXISTS target_temperature DOUBLE PRECISION",
    "ALTER TABLE measurements ADD COLUMN IF NOT EXISTS target_humidity DOUBLE PRECISION",
]


async def init_db() -> None:
    """
    Создаёт таблицы, если их ещё нет, и дотягивает схему старых баз.
    Для продакшена лучше использовать Alembic-миграции, но для MVP
    этого достаточно.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        if conn.dialect.name == "postgresql":
            for statement in _POSTGRES_UPGRADES:
                await conn.execute(text(statement))


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
