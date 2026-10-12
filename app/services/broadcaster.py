import asyncio
import json
import logging

from fastapi import WebSocket

logger = logging.getLogger("climatica.broadcaster")

_SEND_TIMEOUT = 5.0


class Broadcaster:
    """
    Хранит активные WebSocket-подключения и рассылает им обновления.
    Заменяет Qt-сигналы (worker.updated / worker.error) из оригинального проекта:
    там сигнал напрямую обновлял GUI-виджет, здесь — все подключённые браузеры.
    """

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    @property
    def count(self) -> int:
        return len(self._connections)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)
        logger.info("WS client connected, total=%d", len(self._connections))

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self._connections:
            self._connections.discard(websocket)
            logger.info("WS client disconnected, total=%d", len(self._connections))

    async def broadcast(self, message: dict) -> None:
        if not self._connections:
            return

        payload = json.dumps(message, ensure_ascii=False)

        # Снимок множества: во время await может подключиться/отключиться клиент,
        # итерация по живому set тогда упала бы с "Set changed size during iteration".
        connections = list(self._connections)

        results = await asyncio.gather(
            *(asyncio.wait_for(c.send_text(payload), _SEND_TIMEOUT) for c in connections),
            return_exceptions=True,
        )

        for connection, result in zip(connections, results):
            if isinstance(result, BaseException):
                self.disconnect(connection)


broadcaster = Broadcaster()
