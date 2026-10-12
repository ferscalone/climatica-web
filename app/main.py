import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.logging_setup import journal, setup_logging

setup_logging()

from app.api import export, journal as journal_api, thresholds, ws  # noqa: E402
from app.models.db import init_db  # noqa: E402
from app.services.alerts import alert_service  # noqa: E402
from app.services.poller import poller_manager  # noqa: E402
from app.services.thresholds import threshold_service  # noqa: E402

logger = logging.getLogger("climatica")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    # Порядок важен: пороги должны быть в кэше, а пул алертов запущен
    # до того, как поллеры начнут проверять измерения и публиковать события.
    await init_db()
    await threshold_service.load()
    alert_service.start()
    poller_manager.start_all()
    journal.info("SYSTEM started, chambers=%s", settings.chambers_dict)

    yield

    # --- Shutdown ---
    await poller_manager.stop_all()
    journal.info("SYSTEM stopped")
    await alert_service.stop()


app = FastAPI(title="Climatica Web", lifespan=lifespan)

app.include_router(ws.router)
app.include_router(export.router)
app.include_router(thresholds.router)
app.include_router(journal_api.router)

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
