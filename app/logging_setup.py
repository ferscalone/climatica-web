import logging
import logging.handlers
from datetime import datetime

from app.config import settings

# Отдельный журнал событий и действий (алерты, изменения порогов, старт/стоп).
# Пишется в logs/journal.log — одна строка на событие, всегда с датой и временем.
JOURNAL_LOGGER = "climatica.journal"
JOURNAL_FILE = "journal.log"
APP_LOG_FILE = "climatica.log"

_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 20


class _LocalTimeFormatter(logging.Formatter):
    """Время в журнале — в часовом поясе TIMEZONE, с указанием смещения."""

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=settings.tz)
        return dt.strftime("%Y-%m-%d %H:%M:%S%z")


def setup_logging() -> None:
    root = logging.getLogger()

    if getattr(root, "_climatica_configured", False):
        return

    root.setLevel(settings.LOG_LEVEL)

    fmt = _LocalTimeFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    app_file = logging.handlers.RotatingFileHandler(
        settings.LOG_DIR / APP_LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    app_file.setFormatter(fmt)
    root.addHandler(app_file)

    journal = logging.getLogger(JOURNAL_LOGGER)
    journal.setLevel(logging.INFO)
    journal_file = logging.handlers.RotatingFileHandler(
        settings.LOG_DIR / JOURNAL_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    journal_file.setFormatter(_LocalTimeFormatter("%(asctime)s | %(message)s"))
    journal.addHandler(journal_file)

    root._climatica_configured = True  # type: ignore[attr-defined]


def journal_files() -> list:
    """Файлы журнала от самого старого к самому новому (с учётом ротации)."""
    base = settings.LOG_DIR / JOURNAL_FILE
    rotated = [settings.LOG_DIR / f"{JOURNAL_FILE}.{i}" for i in range(_BACKUP_COUNT, 0, -1)]
    return [p for p in [*rotated, base] if p.exists()]


journal = logging.getLogger(JOURNAL_LOGGER)
