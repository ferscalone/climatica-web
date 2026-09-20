import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.broadcaster import broadcaster

logger = logging.getLogger("climatica.ws")

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """
    Клиент подключается сюда и получает поток сообщений вида:

    {
        "type": "measurement",
        "chamber_id": 1,
        "ip": "192.168.1.106",
        "temperature": 4.32,
        "online": true,
        "recorded_at": "2026-09-09T12:00:00+00:00"
    }

    Начальную историю для графиков фронтенд получает отдельно через
    GET /api/history/{chamber_id} — WS отдаёт только "живые" обновления.
    """
    await broadcaster.connect(websocket)

    try:
        while True:
            # Держим соединение открытым; входящие сообщения от клиента
            # сейчас не используются, но чтение нужно для детекции разрыва.
            await websocket.receive_text()

    except WebSocketDisconnect:
        broadcaster.disconnect(websocket)
