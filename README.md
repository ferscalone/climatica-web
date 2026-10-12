# Climatica Web

Веб-версия проекта [Climatica](https://github.com/ferscalone/Climatica) — мониторинг
климатических камер по протоколу Modbus RTU over UDP. Оригинал был десктопным
приложением на PySide6; здесь та же логика опроса перенесена на
FastAPI + WebSocket + PostgreSQL, с веб-интерфейсом и Telegram-ботом для уведомлений.

Подробно про внутреннее устройство, потоки данных и известные ограничения —
в [CLIMATICA_ARCHITECTURE.md](CLIMATICA_ARCHITECTURE.md).

## Возможности

- **Живые графики** всех камер (WebSocket): масштаб колесом, сдвиг перетаскиванием,
  Shift + рамка — выделить участок, двойной клик — сброс; подсказка по точке при наведении.
  Периоды без связи закрашены красным, граничные значения — пунктиром.
- **Просмотр периода из БД** («с … по …» или быстрые кнопки: час/6 часов/сутки/неделя/30 дней).
  Большие срезы прореживаются на сервере с сохранением пиков и разрывов связи.
- **Выгрузка измерений за период** в CSV/Excel (без прореживания, время в `TIMEZONE`).
- **Импорт** ранее сохранённого CSV/Excel для показа на графике (в БД ничего не пишется).
- **Граничные значения** (пороги) по каждой камере — меняются только на сайте, с
  необязательными полями «имя» и «комментарий»; IP и имя компьютера (обратный DNS)
  определяются автоматически.
- **Алерты**: камера отключилась / снова на связи, выход за границы / возврат в норму,
  изменение границ. Уходят в пул уведомлений → журнал + браузер + Telegram.
- **Telegram-бот** — отдельный сервис, работает нон-стоп; команды только на просмотр:
  `/status`, `/thresholds`.
- **Браузерные уведомления**: всплывающие сообщения внутри страницы (работают всегда)
  и системные уведомления (только по https или на localhost — ограничение браузеров).
- **Журнал**: все события и действия пишутся в БД и в файл `logs/journal.log`
  с датой и временем. На сайте — просмотр за период, выгрузка в CSV/Excel и
  скачивание копии всего журнала.
- **Задел под влажность и уставки**: как только станут известны регистры, достаточно
  прописать их индексы в `.env` — появятся значения, линии на графике и пороги влажности.

## Быстрый старт (Docker)

```bash
cp .env.example .env        # поправить CHAMBERS, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS
docker compose up --build -d
```

Поднимутся три контейнера:

| Сервис         | Что делает                                                        |
| -------------- | ----------------------------------------------------------------- |
| `db`           | PostgreSQL 16, данные в volume `climatica_pgdata`                 |
| `backend`      | FastAPI: опрос камер, API, сайт на порту **8000**, журнал в `./logs` |
| `telegram-bot` | Приём алертов от backend и рассылка в Telegram                    |

Интерфейс: **http://localhost:8000** (или `http://<ip-сервера>:8000` из сети).

Логи: `docker compose logs -f backend` / `docker compose logs -f telegram-bot`.
Файловый журнал лежит на хосте в `./logs/journal.log`.

### Настройка Telegram-бота

1. Создать бота у [@BotFather](https://t.me/BotFather), токен → `TELEGRAM_BOT_TOKEN` в `.env`.
2. `docker compose up -d`, написать своему боту `/start` — он ответит вашим `chat_id`.
3. Вписать `chat_id` (через запятую, можно групповые чаты) в `TELEGRAM_CHAT_IDS`,
   выполнить `docker compose up -d` ещё раз.

Команды бота работают только для чатов из `TELEGRAM_CHAT_IDS`.
Без токена бот запускается в «сухом» режиме: алерты принимает и пишет в свой лог,
но никуда не отправляет — удобно, чтобы сначала проверить систему целиком.

Боту нужен выход в интернет к `api.telegram.org` (исходящее соединение, входящие
порты не нужны). Если интернет только через прокси — `TELEGRAM_PROXY_URL`.

### Важно про сеть камер

Камеры общаются по UDP в локальной сети предприятия. Обычной bridge-сети Docker
достаточно, если Docker-хост в той же подсети, что и камеры. Если камеры не отвечают
из контейнера — см. комментарий про `network_mode: "host"` в `docker-compose.yml`
(там же — что ещё поменять, чтобы backend продолжал видеть бота и базу).

## Настройка (`.env`)

Все параметры с комментариями — в [.env.example](.env.example). Основные:

| Переменная | По умолчанию | Смысл |
| --- | --- | --- |
| `CHAMBERS` | `1:192.168.1.106,2:…` | Камеры `id:ip` через `,` или `;` |
| `START_REGISTER`, `REGISTER_COUNT` | `101`, `18` | Какой блок регистров читать |
| `TEMPERATURE_REGISTER_INDEX` | `0` | Индекс температуры внутри блока (регистр 101) |
| `TARGET_TEMPERATURE_REGISTER_INDEX` | `1` | Уставка температуры (регистр 102) |
| `CHAMBER_NAMES` | `1:Terchy MHX-408NK 2021;…` | Названия камер `id:название` через `;` |
| `HUMIDITY_REGISTER_INDEX`, `TARGET_HUMIDITY_REGISTER_INDEX` | `-1` | Влажность и её уставка (`-1` = не читать) |
| `OFFLINE_AFTER_FAILURES` | `3` | Сколько неудачных опросов подряд = «отключилась» |
| `DEFAULT_TEMPERATURE_MIN/MAX` | `2` / `8` | Пороги для новых камер |
| `THRESHOLD_HYSTERESIS` | `0.2` | Запас для возврата «в норму» (против дребезга) |
| `TIMEZONE` | `Europe/Moscow` | Время в выгрузках, журнале и сообщениях бота |
| `INTERNAL_API_TOKEN` | пусто | Общий секрет backend ↔ бот |

## Запуск без Docker (разработка)

```bash
python -m venv .venv && .venv\Scripts\activate        # Windows
pip install -r requirements.txt -r bot/requirements.txt -r requirements-dev.txt

# вариант без PostgreSQL — SQLite-файл:
set DATABASE_URL=sqlite+aiosqlite:///./dev.db
set TELEGRAM_BOT_INTERNAL_URL=http://localhost:8090
uvicorn app.main:app --reload

# в другом окне, из корня проекта:
set BACKEND_URL=http://localhost:8000
python -m bot.main
```

### Проверка без реальных камер

`tools/fake_chamber.py` — эмулятор камер (отвечает на Modbus-запросы как настоящая камера):

```bash
python tools/fake_chamber.py --port 15001 --spike 1 --offline 3
```

Backend при этом запускать с `CHAMBERS=1:127.0.0.1,2:127.0.0.1,3:127.0.0.1` и `UDP_PORT=15001`
(для влажности — `HUMIDITY_REGISTER_INDEX=2`, `TARGET_HUMIDITY_REGISTER_INDEX=3`). Камера 1 будет раз в минуту «перегреваться»,
камера 3 — молчать. В консоли эмулятора: `off 2`, `on 2`, `set 1 12.5`, `auto 1`.

### Тесты

```bash
pytest
```

38 тестов: разбор Modbus-пакетов, парсинг конфига, гистерезис порогов, переходы
online/offline и норма/вне нормы (без спама), история с прореживанием, выгрузка ↔ импорт
CSV/XLSX, валидация порогов, аудит, пул алертов с повторной доставкой, WebSocket,
журналы, HTTP-приём алертов ботом.

## API

| Метод | Путь | Описание |
| --- | --- | --- |
| GET | `/` | Веб-интерфейс |
| WS | `/ws` | Поток `measurement` и `alert` |
| GET | `/api/chambers` | Камеры и какие параметры у них читаются |
| GET | `/api/history/{id}` | Последние N точек или `?date_from=…&date_to=…` (с прореживанием) |
| GET | `/api/export/{id}?format=csv\|xlsx&date_from=…&date_to=…` | Выгрузка измерений |
| POST | `/api/import` | CSV/XLSX → точки для графика (в БД не пишется) |
| GET | `/api/thresholds` | Текущие граничные значения |
| PUT | `/api/thresholds/{id}/{temperature\|humidity}` | Изменить границы (`min_value`, `max_value`, `enabled`, `actor_name`, `comment`) |
| GET | `/api/alerts` | Пул уведомлений (фильтры: период, `chamber_id`, `alert_type`) |
| GET | `/api/alerts/export?format=…` | Выгрузка уведомлений за период |
| GET | `/api/audit-log` | Журнал изменений границ |
| GET | `/api/audit-log/export?format=…` | Выгрузка журнала изменений за период |
| GET | `/api/journal/download` | Копия всего файлового журнала |
| GET | `/api/health` | Состояние сервера и камер (использует бот для `/status`) |

Даты — ISO 8601 (`2026-09-01T00:00:00+03:00` или `2026-09-01`).

## Структура проекта

```
app/
├── main.py              # FastAPI, lifespan: БД → пороги → пул алертов → поллеры
├── config.py            # настройки из .env
├── logging_setup.py     # логи и файловый журнал (logs/journal.log, ротация)
├── modbus/              # CRC16 (1:1 из оригинала) и асинхронный UDP-клиент
├── models/db.py         # таблицы measurements, thresholds, alert_events, audit_log
├── services/
│   ├── poller.py        # опрос камеры, детектор переходов состояния
│   ├── thresholds.py    # пороги: кэш, изменение с аудитом, гистерезис
│   ├── alerts.py        # пул уведомлений: очередь → БД/журнал → браузер → Telegram, retry
│   └── broadcaster.py   # рассылка по WebSocket
├── api/                 # export.py, thresholds.py, journal.py, ws.py
└── static/              # index.html + vendor/ (Chart.js и плагины локально, без CDN)
bot/                     # Telegram-бот (отдельный контейнер)
tools/fake_chamber.py    # эмулятор камер
tests/                   # pytest
docker/                  # Dockerfile (backend), Dockerfile.bot
```

## Дальнейшие шаги

- HTTPS (nginx + сертификат mkcert) — нужен для системных уведомлений браузера по сети
- Alembic-миграции вместо `create_all` + ручных `ALTER` при старте
- Аутентификация на сайте (сейчас «кто изменил» — только самоназвание + IP)
- Очистка/архивация старых измерений (при опросе раз в секунду ~86 тыс. строк в сутки на камеру)
