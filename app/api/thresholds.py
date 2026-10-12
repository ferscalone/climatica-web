import asyncio
import ipaddress
import socket

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import settings
from app.services.thresholds import ActorInfo, ThresholdError, threshold_service

router = APIRouter(prefix="/api")


class ThresholdUpdateIn(BaseModel):
    min_value: float | None = None
    max_value: float | None = None
    enabled: bool = True
    # Необязательные поля формы: кто меняет и зачем
    actor_name: str | None = Field(default=None, max_length=128)
    comment: str | None = Field(default=None, max_length=2000)


def client_ip(request: Request) -> str | None:
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def resolve_host(ip: str | None) -> str | None:
    """
    Имя компьютера по IP через обратный DNS. Имя пользователя Windows браузер
    не отдаёт, а вот имя машины в корпоративной сети часто резолвится.
    """
    if not ip:
        return None
    try:
        if ipaddress.ip_address(ip).is_loopback:
            # Запрос с самого сервера; обратный DNS для 127.0.0.1 часто даёт мусор из hosts
            return socket.gethostname()
    except ValueError:
        return None
    try:
        name, _, _ = await asyncio.wait_for(asyncio.to_thread(socket.gethostbyaddr, ip), timeout=1.5)
        return name
    except Exception:
        return None


@router.get("/thresholds")
async def list_thresholds(chamber_id: int | None = None):
    return threshold_service.list(chamber_id)


@router.put("/thresholds/{chamber_id}/{parameter}")
async def set_threshold(chamber_id: int, parameter: str, body: ThresholdUpdateIn, request: Request):
    try:
        threshold_service.validate(chamber_id, parameter, body.min_value, body.max_value)
    except ThresholdError as exc:
        status = 404 if "не сконфигурирована" in str(exc) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc

    ip = client_ip(request)
    actor = ActorInfo(
        name=(body.actor_name or "").strip() or None,
        ip=ip,
        host=await resolve_host(ip),
        user_agent=request.headers.get("user-agent"),
        comment=(body.comment or "").strip() or None,
    )

    try:
        return await threshold_service.update(
            chamber_id, parameter, body.min_value, body.max_value, body.enabled, actor
        )
    except ThresholdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
