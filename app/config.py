from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Настройки приложения. Значения по умолчанию соответствуют
    исходному проекту Climatica; всё можно переопределить через .env
    или переменные окружения.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Камеры ---
    # Формат: "1:192.168.1.106,2:192.168.1.252,3:192.168.0.45"
    # (разделитель между камерами — запятая или точка с запятой)
    CHAMBERS: str = "1:192.168.1.106,2:192.168.1.252,3:192.168.0.45"
    # Названия камер: "id:название;id:название" (разделитель — точка с запятой).
    # Камера без названия показывается как "Камера N".
    CHAMBER_NAMES: str = (
        "1:Terchy MHX-408NK 2021;2:Terchy MHX-408NK 2023;3:Terchy MHX-225NK"
    )
    UDP_PORT: int = 5001
    START_REGISTER: int = 0x0065
    REGISTER_COUNT: int = 18

    # Какие регистры (индекс внутри прочитанного блока, начиная с 0) что означают.
    # -1 = параметр не читается. Когда станут известны регистры влажности и
    # целевых значений — достаточно прописать индексы здесь/в .env, код менять не нужно.
    TEMPERATURE_REGISTER_INDEX: int = 0
    HUMIDITY_REGISTER_INDEX: int = -1
    # Уставка температуры — регистр 102, т.е. второй в блоке, который читается со 101-го
    TARGET_TEMPERATURE_REGISTER_INDEX: int = 1
    TARGET_HUMIDITY_REGISTER_INDEX: int = -1
    # Значение в регистре = реальное значение * REGISTER_SCALE (2150 -> 21.50)
    REGISTER_SCALE: float = 100.0

    POLL_INTERVAL_MS: int = 1000
    SOCKET_TIMEOUT: float = 2.0

    # Камера считается отключившейся только после N неудачных опросов подряд —
    # защита от ложных алертов из-за одного потерянного UDP-пакета.
    OFFLINE_AFTER_FAILURES: int = 3

    # Сколько последних точек отдавать по умолчанию через REST/при подключении по WS
    GRAPH_POINTS: int = 600

    # Часовой пояс для экспорта, журналов и сообщений бота (в БД время хранится в UTC)
    TIMEZONE: str = "Europe/Moscow"

    # --- База данных ---
    DATABASE_URL: str = (
        "postgresql+asyncpg://climatica:climatica@db:5432/climatica"
    )

    # --- Экспорт ---
    EXPORT_DIR: Path = Path("/tmp/climatica_exports")

    # --- Логи и журнал ---
    LOG_DIR: Path = Path("logs")
    LOG_LEVEL: str = "INFO"

    # --- Пороги по умолчанию (создаются при первом запуске для каждой камеры) ---
    DEFAULT_TEMPERATURE_MIN: float = 2.0
    DEFAULT_TEMPERATURE_MAX: float = 8.0
    DEFAULT_HUMIDITY_MIN: float = 30.0
    DEFAULT_HUMIDITY_MAX: float = 70.0
    # Гистерезис: чтобы значение, дрожащее на границе, не слало breach/recovery
    # каждую секунду, возврат в норму засчитывается только с запасом.
    THRESHOLD_HYSTERESIS: float = 0.2

    # --- Алерты / Telegram-бот ---
    # Пустая строка = доставка в Telegram отключена (алерты всё равно пишутся в журнал
    # и показываются в браузере).
    TELEGRAM_BOT_INTERNAL_URL: str = "http://telegram-bot:8090"
    # Общий секрет между backend и ботом (заголовок X-Internal-Token). Пусто = без проверки.
    INTERNAL_API_TOKEN: str = ""
    ALERT_DELIVERY_TIMEOUT: float = 5.0
    ALERT_RETRY_INTERVAL: float = 30.0
    ALERT_MAX_ATTEMPTS: int = 10

    # Брать IP клиента из X-Forwarded-For (включать только за reverse-proxy, например nginx)
    TRUST_PROXY_HEADERS: bool = False

    @property
    def chambers_dict(self) -> dict[int, str]:
        """
        Разбирает строку CHAMBERS в {id_камеры: ip}.
        """
        result: dict[int, str] = {}

        for item in self.CHAMBERS.replace(";", ",").split(","):
            item = item.strip()

            if not item:
                continue

            chamber_id, sep, ip = item.partition(":")

            if not sep or not ip.strip():
                raise ValueError(f"Неверная запись камеры в CHAMBERS: {item!r} (ожидается id:ip)")

            result[int(chamber_id)] = ip.strip()

        return result

    @property
    def chamber_names(self) -> dict[int, str]:
        result: dict[int, str] = {}

        for item in self.CHAMBER_NAMES.split(";"):
            chamber_id, sep, name = item.partition(":")

            if sep and chamber_id.strip().isdigit() and name.strip():
                result[int(chamber_id)] = name.strip()

        return result

    def chamber_name(self, chamber_id: int | None) -> str:
        if chamber_id is None:
            return ""
        return self.chamber_names.get(chamber_id) or f"Камера {chamber_id}"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.TIMEZONE)

    @property
    def humidity_enabled(self) -> bool:
        return self.HUMIDITY_REGISTER_INDEX >= 0


settings = Settings()
settings.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
