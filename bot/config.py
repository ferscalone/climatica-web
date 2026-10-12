from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# .env ищется рядом с этим файлом (bot/.env), независимо от того, откуда запущен процесс
_ENV_FILE = Path(__file__).resolve().parent / ".env"


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # Токен от @BotFather. Пусто = "сухой" режим: алерты принимаются и пишутся в лог, но не отправляются.
    TELEGRAM_BOT_TOKEN: str = ""
    # Фиксированный список получателей: "123456789,-1001234567890" (запятая или точка с запятой)
    TELEGRAM_CHAT_IDS: str = ""
    # Необязательный прокси для выхода в api.telegram.org, например "http://proxy.local:3128"
    TELEGRAM_PROXY_URL: str = ""

    # Внутренний HTTP-сервер, на который backend шлёт алерты
    BOT_HTTP_HOST: str = "0.0.0.0"
    BOT_HTTP_PORT: int = 8090
    # Должен совпадать с INTERNAL_API_TOKEN у backend. Пусто = без проверки.
    INTERNAL_API_TOKEN: str = ""

    # Адрес backend для команд /status и /thresholds
    BACKEND_URL: str = "http://backend:8000"

    TIMEZONE: str = "Europe/Moscow"
    LOG_LEVEL: str = "INFO"

    @property
    def chat_ids(self) -> list[int]:
        result = []
        for item in self.TELEGRAM_CHAT_IDS.replace(";", ",").split(","):
            item = item.strip()
            if item:
                result.append(int(item))
        return result


bot_settings = BotSettings()
