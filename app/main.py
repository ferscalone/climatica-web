import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import export, ws
from app.models.db import init_db
from app.services.poller import poller_manager

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    await init_db()
    poller_manager.start_all()

    yield

    # --- Shutdown ---
    await poller_manager.stop_all()


app = FastAPI(title="Climatica Web", lifespan=lifespan)

app.include_router(ws.router)
app.include_router(export.router)

app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
