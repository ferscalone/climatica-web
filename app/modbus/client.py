import asyncio
import struct

from app.config import settings
from app.modbus.crc import crc16


class _DatagramClientProtocol(asyncio.DatagramProtocol):
    """
    Минимальный протокол для одного запрос/ответ обмена по UDP.
    """

    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None
        self.response: bytes | None = None
        self.exception: Exception | None = None
        self.received = asyncio.Event()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr) -> None:
        self.response = data
        self.received.set()

    def error_received(self, exc: Exception) -> None:
        self.exception = exc
        self.received.set()


class FridgeClient:
    """
    Асинхронный аналог оригинального FridgeClient (Modbus RTU over UDP).

    Протокол, структура пакета и разбор ответа идентичны оригиналу —
    изменён только транспорт (asyncio вместо блокирующего socket),
    что позволяет опрашивать несколько камер параллельно в одном event loop.
    """

    def __init__(self, slave_id: int, ip: str):
        self.slave = slave_id
        self.ip = ip

    def build_request(self) -> bytes:
        packet = bytearray()

        packet.append(self.slave)
        packet.append(0x03)

        packet += struct.pack(">H", settings.START_REGISTER)
        packet += struct.pack(">H", settings.REGISTER_COUNT)

        crc = crc16(packet)
        packet += struct.pack("<H", crc)

        return bytes(packet)

    @staticmethod
    def _parse_registers(data: bytes) -> list[int]:
        # slave + func + count + CRC(2) — меньше 5 байт быть не может
        if len(data) < 5:
            raise RuntimeError(f"Слишком короткий ответ ({len(data)} байт)")

        crc_recv = struct.unpack("<H", data[-2:])[0]
        crc_calc = crc16(data[:-2])

        if crc_recv != crc_calc:
            raise RuntimeError("CRC error")

        if data[1] & 0x80:
            raise RuntimeError(f"Modbus exception, код {data[2]}")

        if data[1] != 0x03:
            raise RuntimeError("Invalid function")

        count = data[2]

        if len(data) < 3 + count + 2:
            raise RuntimeError(
                f"Обрезанный ответ: заявлено {count} байт данных, получено {len(data) - 5}"
            )

        registers = []
        offset = 3

        for _ in range(count // 2):
            value = struct.unpack(">h", data[offset:offset + 2])[0]
            registers.append(value)
            offset += 2

        return registers

    async def read_registers(self) -> list[int]:
        loop = asyncio.get_running_loop()

        transport, protocol = await loop.create_datagram_endpoint(
            _DatagramClientProtocol,
            remote_addr=(self.ip, settings.UDP_PORT),
        )

        try:
            packet = self.build_request()
            transport.sendto(packet)

            try:
                await asyncio.wait_for(
                    protocol.received.wait(),
                    timeout=settings.SOCKET_TIMEOUT,
                )
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"Таймаут опроса камеры {self.slave} ({self.ip})"
                )

            if protocol.exception is not None:
                raise protocol.exception

            if protocol.response is None:
                raise RuntimeError("Пустой ответ от камеры")

            return self._parse_registers(protocol.response)

        finally:
            transport.close()

    @staticmethod
    def registers_to_values(registers: list[int]) -> dict[str, float | None]:
        """
        Переводит сырые регистры в значения по индексам из настроек.
        Параметры с индексом -1 (или за пределами ответа) возвращаются как None.
        """

        def pick(index: int) -> float | None:
            if index < 0 or index >= len(registers):
                return None
            return registers[index] / settings.REGISTER_SCALE

        temperature = pick(settings.TEMPERATURE_REGISTER_INDEX)

        if temperature is None:
            raise RuntimeError("В ответе нет регистра температуры")

        return {
            "temperature": temperature,
            "humidity": pick(settings.HUMIDITY_REGISTER_INDEX),
            "target_temperature": pick(settings.TARGET_TEMPERATURE_REGISTER_INDEX),
            "target_humidity": pick(settings.TARGET_HUMIDITY_REGISTER_INDEX),
        }

    async def read_values(self) -> dict[str, float | None]:
        return self.registers_to_values(await self.read_registers())

    async def read_temperature(self) -> float:
        return (await self.read_values())["temperature"]  # type: ignore[return-value]
