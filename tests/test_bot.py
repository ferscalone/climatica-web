"""HTTP-приём алертов ботом (без настоящего Telegram)."""

from aiohttp.test_utils import TestClient, TestServer

from bot.config import BotSettings
from bot.main import AlertBot


async def make_client(**overrides):
    settings = BotSettings(_env_file=None, TELEGRAM_BOT_TOKEN="", **overrides)
    bot = AlertBot(settings)
    client = TestClient(TestServer(bot.make_web_app()))
    await client.start_server()
    return bot, client


async def test_send_dry_run_and_health():
    bot, client = await make_client()
    try:
        r = await client.post("/internal/send", json={"id": 1, "alert_type": "chamber_offline", "message": "тест"})
        assert r.status == 200
        assert (await r.json())["dry_run"] is True

        health = await (await client.get("/internal/health")).json()
        assert health["received"] == 1
        assert health["telegram_configured"] is False
    finally:
        await client.close()


async def test_send_validation_and_token():
    bot, client = await make_client(INTERNAL_API_TOKEN="s3cret")
    try:
        assert (await client.post("/internal/send", json={"message": "x"})).status == 403
        headers = {"X-Internal-Token": "s3cret"}
        assert (await client.post("/internal/send", data="not json", headers=headers)).status == 400
        assert (await client.post("/internal/send", json={"no": "message"}, headers=headers)).status == 400
        assert (await client.post("/internal/send", json={"message": "x"}, headers=headers)).status == 200
    finally:
        await client.close()


async def test_send_fails_when_no_recipients_so_backend_retries():
    bot, client = await make_client()

    class FakeApp:
        class bot:
            @staticmethod
            async def send_message(**kw):
                raise AssertionError("не должно вызываться")

    bot.application = FakeApp()  # токен "есть", но TELEGRAM_CHAT_IDS пуст
    try:
        r = await client.post("/internal/send", json={"message": "x"})
        assert r.status == 502
    finally:
        await client.close()


async def test_send_to_all_recipients():
    bot, client = await make_client(TELEGRAM_CHAT_IDS="1,2")
    sent = []

    class FakeApp:
        class bot:
            @staticmethod
            async def send_message(chat_id, text, parse_mode):
                sent.append((chat_id, text))

    bot.application = FakeApp()
    try:
        r = await client.post("/internal/send", json={"alert_type": "threshold_breach", "message": "жарко"})
        assert (await r.json())["sent"] == 2
        assert [c for c, _ in sent] == [1, 2]
        assert "Выход за границы" in sent[0][1]
    finally:
        await client.close()
