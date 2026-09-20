# Climatica Web

Веб-версия проекта [Climatica](https://github.com/ferscalone/Climatica) (мониторинг
климатических камер по протоколу Modbus RTU over UDP). Оригинал был написан на
PySide6 (десктопное приложение); здесь та же логика опроса камер перенесена
на FastAPI + WebSocket + PostgreSQL, с простым веб-интерфейсом вместо Qt GUI.

## Что изменилось относительно оригинала

| Было (PySide6)                          | Стало (FastAPI)                                   |
| ---------------------------------------- | -------------------------------------------------- |
| `QThread` на каждую камеру                | `asyncio.Task` на каждую камеру                     |
| Qt `Signal` → обновление GUI-виджета      | `broadcaster.broadcast()` → всем WebSocket-клиентам |
| Блокирующий `socket` (UDP)                | `asyncio.DatagramProtocol` (неблокирующий UDP)      |
| CSV/XLSX дозапись на каждое измерение     | PostgreSQL как источник правды                      |
| —                                          | CSV/XLSX — генерация по запросу (`/api/export`)     |
| pyqtgraph                                  | Chart.js в браузере                                  |

Протокол обмена с камерами (сборка Modbus-пакета, CRC16, разбор ответа)
перенесён **без изменений** — это самая ценная часть оригинального проекта,
результат реверс-инжиниринга, который не имеет смысла переписывать.

## Структура проекта

```
app/
├── main.py                 # точка входа FastAPI, lifespan (старт/стоп поллеров)
├── config.py                # настройки (камеры, БД, интервал опроса) из .env
├── modbus/
│   ├── crc.py                # CRC16 — перенесено 1:1 из оригинала
│   └── client.py             # асинхронный Modbus RTU over UDP клиент
├── services/
│   ├── poller.py              # аналог FridgeWorker — опрос камеры в цикле
│   └── broadcaster.py         # менеджер WebSocket-подключений
├── models/
│   └── db.py                   # SQLAlchemy модель Measurement + async engine
├── api/
│   ├── ws.py                    # GET /ws — живой поток измерений
│   └── export.py                # /api/chambers, /api/history, /api/export
└── static/
    └── index.html                 # веб-интерфейс (Chart.js + WebSocket)
```

## Настройка камер

Список камер задаётся переменной `CHAMBERS` в формате `id:ip,id:ip,id:ip`
(см. `.env.example` и `docker-compose.yml`). Значения по умолчанию соответствуют
оригинальному `config.py`:

```
CHAMBERS=1:192.168.1.106,2:192.168.1.252,3:192.168.0.45
```

Остальные параметры протокола (`UDP_PORT`, `START_REGISTER`, `REGISTER_COUNT`,
`POLL_INTERVAL_MS`) также настраиваются через переменные окружения — см.
`app/config.py` и `.env.example`.

## Запуск через Docker

```bash
docker compose up --build
```

Поднимутся два контейнера:
- `db` — PostgreSQL 16, данные хранятся в volume `climatica_pgdata`;
- `backend` — FastAPI-приложение на порту `8000`.

Открыть интерфейс: **http://localhost:8000**

### Важно про сеть камер

Камеры общаются по UDP в локальной сети предприятия. По умолчанию используется
обычная Docker bridge-сеть — этого достаточно, если Docker-хост физически
находится в той же подсети, что и камеры (UDP пакеты выходят через хост).

Если камеры не отвечают из контейнера, проверьте:

```bash
docker compose exec backend python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b'test', ('192.168.1.106', 5001))"
```

Если пакет не доходит — раскомментируйте `network_mode: "host"` в
`docker-compose.yml` для сервиса `backend` (даёт контейнеру сетевой стек хоста
напрямую, без NAT).

## Запуск без Docker (локальная разработка)

```bash
pip install -r requirements.txt

# нужен доступный PostgreSQL, см. DATABASE_URL в .env
cp .env.example .env

uvicorn app.main:app --reload
```

## API

| Метод | Путь                              | Описание                                             |
| ----- | ---------------------------------- | ----------------------------------------------------- |
| GET   | `/`                                  | Веб-интерфейс                                          |
| GET   | `/ws`                                 | WebSocket, живой поток измерений всех камер            |
| GET   | `/api/chambers`                        | Список сконфигурированных камер                        |
| GET   | `/api/history/{chamber_id}`             | Последние N измерений (для отрисовки графика при загрузке) |
| GET   | `/api/export/{chamber_id}?format=csv`    | Экспорт истории в CSV                                   |
| GET   | `/api/export/{chamber_id}?format=xlsx`    | Экспорт истории в Excel                                  |

Экспорт поддерживает фильтр по датам: `?date_from=2026-09-01&date_to=2026-09-09`.

## Формат сообщений WebSocket

```json
{
  "type": "measurement",
  "chamber_id": 1,
  "ip": "192.168.1.106",
  "temperature": 4.32,
  "online": true,
  "recorded_at": "2026-09-09T12:00:00+00:00"
}
```

При потере связи с камерой (`online: false`) поле `temperature` равно `null`,
добавляется поле `error` с текстом исключения.

## Дальнейшие шаги (не входят в MVP)

- Alembic-миграции вместо `create_all` при старте
- Аутентификация для веб-интерфейса
- Настройка алертов при выходе температуры за диапазон (было в планах и у оригинала)
- Публикация по MQTT
