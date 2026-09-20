import json
import logging

from fastapi import WebSocket

logger = logging.getLogger("climatica.broadcaster")


class Broadcaster:
    """
    Хранит активные WebSocket-подключения и рассылает им обновления.
    Заменяет Qt-сигналы (worker.updated / worker.error) из оригинального проекта:
    там сигнал напрямую обновлял GUI-виджет, здесь — все подключённые браузеры.
    """

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)
        logger.info("WS client connected, total=%d", len(self._connections))

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)
        logger.info("WS client disconnected, total=%d", len(self._connections))

    async def broadcast(self, message: dict) -> None:
        if not self._connections:
            return

        payload = json.dumps(message, ensure_ascii=False)

        stale: list[WebSocket] = []

        for connection in self._connections:
            try:
                await connection.send_text(payload)
            except Exception:
                stale.append(connection)

        for connection in stale:
            self.disconnect(connection)


broadcaster = Broadcaster()
