from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Настройки приложения. Значения по умолчанию соответствуют
    исходному проекту Climatica; всё можно переопределить через .env
    или переменные окружения.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # --- Камеры ---
    # Формат: "1:192.168.1.106,2:192.168.1.252,3:192.168.0.45"
    CHAMBERS: str = "1:192.168.1.106;2:192.168.1.252;3:192.168.0.45"
    UDP_PORT: int = 5001
    START_REGISTER: int = 0x0065
    REGISTER_COUNT: int = 18

    POLL_INTERVAL_MS: int = 1000
    SOCKET_TIMEOUT: float = 2.0

    # Сколько последних точек отдавать по умолчанию через REST/при подключении по WS
    GRAPH_POINTS: int = 600

    # --- База данных ---
    DATABASE_URL: str = (
        "postgresql+asyncpg://climatica:climatica@db:5432/climatica"
    )

    # --- Экспорт ---
    EXPORT_DIR: Path = Path("/tmp/climatica_exports")

    @property
    def chambers_dict(self) -> dict[int, str]:
        """
        Разбирает строку CHAMBERS в {id_камеры: ip}.
        """
        result: dict[int, str] = {}

        for item in self.CHAMBERS.split(","):
            item = item.strip()

            if not item:
                continue

            chamber_id, ip = item.split(":")

            result[int(chamber_id)] = ip.strip()

        return result


settings = Settings()
settings.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
