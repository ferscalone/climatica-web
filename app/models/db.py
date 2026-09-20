from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Index
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings


class Base(DeclarativeBase):
    pass


class Measurement(Base):
    """
    Одно измерение температуры одной камеры.
    Аналог строки в CSV/Excel из оригинального проекта.
    """

    __tablename__ = "measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    chamber_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chamber_ip: Mapped[str] = mapped_column(String(64), nullable=False)

    temperature: Mapped[float] = mapped_column(Float, nullable=False)
    online: Mapped[bool] = mapped_column(default=True, nullable=False)

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("ix_measurements_chamber_time", "chamber_id", "recorded_at"),
    )


engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)

async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """
    Создаёт таблицы, если их ещё нет.
    Для продакшена лучше использовать Alembic-миграции, но для MVP
    достаточно create_all при старте приложения.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
