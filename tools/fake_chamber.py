"""
Эмулятор климатических камер (Modbus RTU over UDP) — для проверки без железа.

Отвечает на запрос чтения регистров (функция 0x03) так же, как настоящая камера:
регистр 0 (101) — температура * 100, регистр 1 (102) — уставка температуры * 100,
регистр 2 — влажность * 100, регистр 3 — уставка влажности * 100
(где влажность у настоящих камер — пока неизвестно, в эмуляторе место условное).
Все камеры слушают один порт; какую камеру спрашивают, видно по slave id.

Запуск (из корня проекта):
    python tools/fake_chamber.py --port 5001
    python tools/fake_chamber.py --port 5001 --offline 2      # камера 2 молчит
    python tools/fake_chamber.py --port 5001 --spike 1        # камера 1 периодически перегревается

Backend тогда запускать с CHAMBERS=1:127.0.0.1,2:127.0.0.1,3:127.0.0.1 и UDP_PORT=5001
(и, чтобы увидеть влажность, HUMIDITY_REGISTER_INDEX=2, TARGET_HUMIDITY_REGISTER_INDEX=3).

Во время работы можно вводить команды в консоли:
    off N  — камера N перестаёт отвечать      on N — снова отвечает
    set N T — зафиксировать температуру камеры N (например: set 1 12.5)
    auto N  — вернуть камеру N к синусоиде
"""

import argparse
import asyncio
import math
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modbus.crc import crc16  # noqa: E402


class State:
    def __init__(self, offline: set[int], spike: set[int]) -> None:
        self.offline = set(offline)
        self.spike = set(spike)
        self.fixed: dict[int, float] = {}

    def values(self, slave: int) -> list[float]:
        t = time.time()
        if slave in self.fixed:
            temp = self.fixed[slave]
        else:
            temp = 5.0 + slave * 0.5 + 1.5 * math.sin(t / 30 + slave)
            if slave in self.spike and int(t) % 60 < 15:
                temp += 6.0
        humidity = 50 + 8 * math.sin(t / 45 + slave)
        return [temp, 5.0 + slave * 0.5, humidity, 50.0]


def build_response(slave: int, count: int, values: list[float]) -> bytes:
    registers = [int(round(v * 100)) for v in values] + [0] * max(0, count - len(values))
    registers = registers[:count]
    body = bytearray([slave, 0x03, count * 2])
    for reg in registers:
        body += struct.pack(">h", max(-32768, min(32767, reg)))
    body += struct.pack("<H", crc16(body))
    return bytes(body)


class Protocol(asyncio.DatagramProtocol):
    def __init__(self, state: State) -> None:
        self.state = state

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) != 8 or crc16(data[:-2]) != struct.unpack("<H", data[-2:])[0]:
            return
        slave, func = data[0], data[1]
        if func != 0x03 or slave in self.state.offline:
            return
        count = struct.unpack(">H", data[4:6])[0]
        self.transport.sendto(build_response(slave, count, self.state.values(slave)), addr)


def console(state: State) -> None:
    for line in sys.stdin:
        parts = line.split()
        try:
            if parts[0] == "off":
                state.offline.add(int(parts[1]))
            elif parts[0] == "on":
                state.offline.discard(int(parts[1]))
            elif parts[0] == "set":
                state.fixed[int(parts[1])] = float(parts[2])
            elif parts[0] == "auto":
                state.fixed.pop(int(parts[1]), None)
            print(f"offline={sorted(state.offline)} fixed={state.fixed}", flush=True)
        except (IndexError, ValueError):
            print("команды: off N | on N | set N T | auto N", flush=True)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--offline", type=int, nargs="*", default=[])
    parser.add_argument("--spike", type=int, nargs="*", default=[])
    args = parser.parse_args()

    state = State(set(args.offline), set(args.spike))
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: Protocol(state), local_addr=(args.host, args.port))
    print(f"Эмулятор камер слушает udp://{args.host}:{args.port}", flush=True)

    if sys.stdin and sys.stdin.isatty():
        threading.Thread(target=console, args=(state,), daemon=True).start()

    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
