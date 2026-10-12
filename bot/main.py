"""
Telegram-бот Climatica — отдельный процесс/контейнер, работает нон-стоп.

1. HTTP-сервер (aiohttp) на BOT_HTTP_PORT принимает алерты от backend:
   POST /internal/send  -> рассылка по фиксированному списку TELEGRAM_CHAT_IDS
   GET  /internal/health
2. Telegram polling (бот сам ходит в api.telegram.org — входящие порты и
   публичный HTTPS не нужны, это работает из закрытой сети).
   Команды только на просмотр: /status, /thresholds, /help, /start.
   Менять пороги через бота нельзя — только через сайт.

Запуск без Docker (из корня проекта):  python -m bot.main
"""

import asyncio
import html
import logging
import signal
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from aiohttp import web
from telegram import BotCommand, Update
from telegram.error import RetryAfter, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

from bot.config import BotSettings, bot_settings

logger = logging.getLogger("climatica.bot")

ALERT_TITLES = {
    "chamber_offline": "🔴 Камера отключилась",
    "chamber_online": "🟢 Камера снова на связи",
    "threshold_breach": "⚠️ Выход за границы",
    "threshold_recovery": "✅ Значение в норме",
    "threshold_changed": "🛠 Изменены границы",
    "delivery_failed": "📭 Ошибка доставки",
    "system": "ℹ️ Система",
}
DEFAULT_TITLE = "ℹ️ Уведомление Climatica"

PARAMETER_NAMES = {"temperature": "температура", "humidity": "влажность"}

TELEGRAM_MAX_LEN = 4096


class AlertBot:
    def __init__(self, settings: BotSettings) -> None:
        self.settings = settings
        self.tz = ZoneInfo(settings.TIMEZONE)
        self.started_at = datetime.now(self.tz)
        self.received = 0
        self.sent = 0
        self.application: Application | None = None

        if settings.TELEGRAM_BOT_TOKEN:
            builder = Application.builder().token(settings.TELEGRAM_BOT_TOKEN)
            if settings.TELEGRAM_PROXY_URL:
                builder = builder.request(HTTPXRequest(proxy=settings.TELEGRAM_PROXY_URL)).get_updates_request(
                    HTTPXRequest(proxy=settings.TELEGRAM_PROXY_URL)
                )
            self.application = builder.build()
            for name, handler in (
                ("start", self.cmd_start),
                ("help", self.cmd_help),
                ("status", self.cmd_status),
                ("thresholds", self.cmd_thresholds),
            ):
                self.application.add_handler(CommandHandler(name, handler))

    # ------------------------------------------------------------------ format

    def _local_time(self, iso: str | None) -> str:
        if not iso:
            return datetime.now(self.tz).strftime("%d.%m.%Y %H:%M:%S")
        try:
            return datetime.fromisoformat(iso).astimezone(self.tz).strftime("%d.%m.%Y %H:%M:%S")
        except ValueError:
            return iso

    def format_alert(self, event: dict) -> str:
        title = ALERT_TITLES.get(event.get("alert_type", ""), DEFAULT_TITLE)
        lines = [f"<b>{html.escape(title)}</b>", html.escape(str(event.get("message", "")))]
        lines.append(f"<i>{self._local_time(event.get('created_at'))}</i>")
        text = "\n".join(lines)
        return text[: TELEGRAM_MAX_LEN - 10]

    # --------------------------------------------------------------- delivery

    async def broadcast(self, text: str) -> tuple[int, list[str]]:
        """Возвращает (сколько отправлено, список ошибок)."""
        if self.application is None:
            logger.warning("TELEGRAM_BOT_TOKEN не задан — сообщение НЕ отправлено (сухой режим):\n%s", text)
            return 0, []

        chat_ids = self.settings.chat_ids
        if not chat_ids:
            logger.warning("TELEGRAM_CHAT_IDS пуст — некому отправлять сообщение:\n%s", text)
            return 0, ["TELEGRAM_CHAT_IDS пуст"]

        sent, errors = 0, []
        for chat_id in chat_ids:
            for attempt in (1, 2):
                try:
                    await self.application.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
                    sent += 1
                    break
                except RetryAfter as exc:
                    if attempt == 2:
                        errors.append(f"{chat_id}: flood control")
                        break
                    await asyncio.sleep(float(getattr(exc, "retry_after", 1)))
                except TelegramError as exc:
                    errors.append(f"{chat_id}: {exc}")
                    logger.error("Не удалось отправить в чат %s: %s", chat_id, exc)
                    break

        self.sent += sent
        return sent, errors

    # ------------------------------------------------------------ HTTP server

    def _authorized_request(self, request: web.Request) -> bool:
        token = self.settings.INTERNAL_API_TOKEN
        return not token or request.headers.get("X-Internal-Token") == token

    async def handle_send(self, request: web.Request) -> web.Response:
        if not self._authorized_request(request):
            return web.json_response({"error": "forbidden"}, status=403)

        try:
            event = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)

        if not isinstance(event, dict) or "message" not in event:
            return web.json_response({"error": "message is required"}, status=400)

        self.received += 1
        logger.info("Алерт #%s (%s) получен от backend", event.get("id"), event.get("alert_type"))

        sent, errors = await self.broadcast(self.format_alert(event))

        if errors and sent == 0:
            # 502 — backend оставит событие недоставленным и повторит попытку позже
            return web.json_response({"sent": 0, "errors": errors}, status=502)

        return web.json_response({"sent": sent, "errors": errors, "dry_run": self.application is None})

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "telegram_configured": self.application is not None,
            "recipients": len(self.settings.chat_ids),
            "received": self.received,
            "sent": self.sent,
            "started_at": self.started_at.isoformat(),
        })

    def make_web_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/internal/send", self.handle_send)
        app.router.add_get("/internal/health", self.handle_health)
        return app

    # --------------------------------------------------------------- commands

    def _is_allowed(self, update: Update) -> bool:
        return update.effective_chat is not None and update.effective_chat.id in self.settings.chat_ids

    async def _reply(self, update: Update, text: str) -> None:
        if update.effective_message is not None:
            await update.effective_message.reply_text(text, parse_mode="HTML")

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id if update.effective_chat else "?"
        if self._is_allowed(update):
            await self._reply(update, f"Бот Climatica. Этот чат (<code>{chat_id}</code>) получает уведомления.\n/help — команды")
        else:
            await self._reply(
                update,
                f"Бот Climatica. Ваш chat_id: <code>{chat_id}</code>.\n"
                "Чтобы получать уведомления, администратор должен добавить его в TELEGRAM_CHAT_IDS.",
            )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            return
        await self._reply(
            update,
            "/status — состояние камер и сервера\n"
            "/thresholds — текущие граничные значения\n\n"
            "Изменить границы можно только на сайте.",
        )

    async def _backend_get(self, path: str):
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(self.settings.BACKEND_URL.rstrip("/") + path)
            response.raise_for_status()
            return response.json()

    async def status_text(self) -> str:
        try:
            health = await self._backend_get("/api/health")
        except Exception as exc:
            return f"🔴 Сервер Climatica недоступен: {html.escape(str(exc))}"

        lines = ["<b>🟢 Сервер Climatica работает</b>"]
        for ch in health.get("chambers", []):
            if ch.get("online") is True:
                icon, state = "🟢", "на связи"
            elif ch.get("online") is False:
                icon, state = "🔴", "нет связи"
            else:
                icon, state = "⚪", "опрашивается…"
            name = html.escape(ch.get("name") or f"Камера {ch['chamber_id']}")
            lines.append(f"{icon} <b>{name}</b> ({html.escape(ch['ip'])}): {state}")

            # При потере связи показываем последнее известное значение — с пометкой, когда оно было
            if ch.get("temperature") is not None:
                values = [f"🌡 {ch['temperature']:.2f} °C"]
                if ch.get("target_temperature") is not None:
                    values.append(f"уставка {ch['target_temperature']:.1f} °C")
                if ch.get("humidity") is not None:
                    values.append(f"💧 {ch['humidity']:.1f} %")
                if ch.get("target_humidity") is not None:
                    values.append(f"уставка {ch['target_humidity']:.1f} %")
                stale = ""
                if ch.get("online") is not True and ch.get("last_seen"):
                    stale = f" (последние данные: {self._local_time(ch['last_seen'])})"
                lines.append("     " + ", ".join(values) + stale)

            breaches = [
                PARAMETER_NAMES.get(p, p) + (" выше нормы" if v == "above" else " ниже нормы")
                for p, v in (ch.get("breaches") or {}).items() if v
            ]
            if breaches and ch.get("online") is True:
                lines.append("     ⚠️ " + ", ".join(breaches))
        lines.append(f"Открытых вкладок сайта: {health.get('ws_clients', 0)}")
        return "\n".join(lines)

    async def thresholds_text(self) -> str:
        try:
            items = await self._backend_get("/api/thresholds")
        except Exception as exc:
            return f"🔴 Не удалось получить границы: {html.escape(str(exc))}"

        if not items:
            return "Границы не заданы."

        def fmt(v):
            return "—" if v is None else f"{v:g}"

        lines = ["<b>Граничные значения</b>"]
        for t in items:
            state = "" if t.get("enabled", True) else " (проверка выключена)"
            lines.append(
                f"{html.escape(t.get('chamber_name') or 'Камера ' + str(t['chamber_id']))}, "
                f"{html.escape(t.get('label', t['parameter']))}: "
                f"{fmt(t['min_value'])} … {fmt(t['max_value'])} {html.escape(t.get('unit', ''))}{state}"
            )
        return "\n".join(lines)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._is_allowed(update):
            await self._reply(update, await self.status_text())

    async def cmd_thresholds(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._is_allowed(update):
            await self._reply(update, await self.thresholds_text())

    # -------------------------------------------------------------------- run

    async def run(self) -> None:
        runner = web.AppRunner(self.make_web_app())
        await runner.setup()
        site = web.TCPSite(runner, self.settings.BOT_HTTP_HOST, self.settings.BOT_HTTP_PORT)
        await site.start()
        logger.info("HTTP-приём алертов: http://%s:%s/internal/send", self.settings.BOT_HTTP_HOST, self.settings.BOT_HTTP_PORT)

        if self.application is None:
            logger.warning("TELEGRAM_BOT_TOKEN не задан — бот работает в сухом режиме (только лог)")
        else:
            if not self.settings.chat_ids:
                logger.warning("TELEGRAM_CHAT_IDS пуст — уведомления некому отправлять. Узнать chat_id: написать боту /start")
            await self.application.initialize()
            await self.application.start()
            await self.application.updater.start_polling(drop_pending_updates=True)
            try:
                await self.application.bot.set_my_commands([
                    BotCommand("status", "Состояние камер"),
                    BotCommand("thresholds", "Граничные значения"),
                    BotCommand("help", "Справка"),
                ])
            except TelegramError as exc:
                logger.warning("Не удалось зарегистрировать команды: %s", exc)
            logger.info("Telegram polling запущен, получателей: %d", len(self.settings.chat_ids))

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass  # Windows: остановка по Ctrl+C через KeyboardInterrupt

        try:
            await stop.wait()
        finally:
            if self.application is not None:
                await self.application.updater.stop()
                await self.application.stop()
                await self.application.shutdown()
            await runner.cleanup()
            logger.info("Бот остановлен")


def main() -> None:
    logging.basicConfig(
        level=bot_settings.LOG_LEVEL,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # httpx логирует каждый getUpdates — слишком шумно
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        asyncio.run(AlertBot(bot_settings).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
