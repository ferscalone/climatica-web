"""Модульные тесты без сервера: конфиг, Modbus-разбор, логика порогов, бот."""

import struct

import pytest

from app.config import Settings, settings
from app.modbus.client import FridgeClient
from app.modbus.crc import crc16
from app.services.thresholds import evaluate
from tools.fake_chamber import build_response


# ---------------------------------------------------------------- config

@pytest.mark.parametrize("raw", [
    "1:192.168.1.106,2:192.168.1.252,3:192.168.0.45",
    "1:192.168.1.106;2:192.168.1.252;3:192.168.0.45",
    " 1 : 192.168.1.106 ; 2:192.168.1.252,3:192.168.0.45; ",
])
def test_chambers_parsing_accepts_comma_and_semicolon(raw):
    assert Settings(CHAMBERS=raw).chambers_dict == {
        1: "192.168.1.106", 2: "192.168.1.252", 3: "192.168.0.45",
    }


def test_chambers_parsing_rejects_garbage():
    with pytest.raises(ValueError):
        Settings(CHAMBERS="1-192.168.1.106").chambers_dict


def test_default_chambers_parse():
    # В первой версии дефолт был через ";", а парсер резал по "," — приложение падало без .env
    assert len(Settings(_env_file=None, CHAMBERS=Settings.model_fields["CHAMBERS"].default).chambers_dict) == 3


# ---------------------------------------------------------------- modbus

def test_request_packet_matches_original_protocol():
    packet = FridgeClient(3, "x").build_request()
    assert packet[:2] == bytes([3, 0x03])
    assert struct.unpack(">HH", packet[2:6]) == (settings.START_REGISTER, settings.REGISTER_COUNT)
    assert struct.unpack("<H", packet[6:])[0] == crc16(packet[:6])


def test_parse_response_roundtrip():
    response = build_response(1, 18, [21.5, 55.25, 20.0, 50.0])
    registers = FridgeClient._parse_registers(response)
    assert len(registers) == 18
    assert registers[:4] == [2150, 5525, 2000, 5000]


def test_negative_temperature():
    registers = FridgeClient._parse_registers(build_response(1, 4, [-18.4]))
    assert registers[0] == -1840


@pytest.mark.parametrize("data, message", [
    (b"\x01\x03", "короткий"),
    (build_response(1, 4, [1.0])[:-3] + build_response(1, 4, [1.0])[-2:], "CRC"),
])
def test_parse_rejects_broken_packets(data, message):
    with pytest.raises(RuntimeError, match=message):
        FridgeClient._parse_registers(data)


def test_parse_rejects_truncated_packet_with_valid_crc():
    body = bytearray([1, 0x03, 36, 0, 1])  # заявлено 36 байт, пришло 2
    body += struct.pack("<H", crc16(body))
    with pytest.raises(RuntimeError, match="Обрезанный"):
        FridgeClient._parse_registers(bytes(body))


def test_modbus_exception_response():
    body = bytearray([1, 0x83, 2])
    body += struct.pack("<H", crc16(body))
    with pytest.raises(RuntimeError, match="exception"):
        FridgeClient._parse_registers(bytes(body))


def test_registers_to_values_uses_configured_indexes(monkeypatch):
    monkeypatch.setattr(settings, "HUMIDITY_REGISTER_INDEX", 1)
    monkeypatch.setattr(settings, "TARGET_TEMPERATURE_REGISTER_INDEX", 2)
    monkeypatch.setattr(settings, "TARGET_HUMIDITY_REGISTER_INDEX", 99)  # за пределами ответа
    values = FridgeClient.registers_to_values([2150, 5525, 2000])
    assert values == {
        "temperature": 21.5,
        "humidity": 55.25,
        "target_temperature": 20.0,
        "target_humidity": None,
    }


# ---------------------------------------------------------------- thresholds

T = {"min_value": 2.0, "max_value": 8.0, "enabled": True}


def test_evaluate_basic():
    assert evaluate(5, T, None) is None
    assert evaluate(8.1, T, None) == "above"
    assert evaluate(1.9, T, None) == "below"
    assert evaluate(100, {**T, "enabled": False}, None) is None
    assert evaluate(100, None, None) is None


def test_evaluate_hysteresis():
    # THRESHOLD_HYSTERESIS=0.5 в conftest: из "above" выходим только ниже 7.5
    assert evaluate(7.8, T, "above") == "above"
    assert evaluate(7.4, T, "above") is None
    assert evaluate(2.3, T, "below") == "below"
    assert evaluate(2.6, T, "below") is None


def test_evaluate_one_sided():
    only_max = {"min_value": None, "max_value": 8.0, "enabled": True}
    assert evaluate(-50, only_max, None) is None
    assert evaluate(9, only_max, None) == "above"


# ---------------------------------------------------------------- bot

def test_bot_format_and_chat_ids():
    from bot.config import BotSettings
    from bot.main import AlertBot

    s = BotSettings(_env_file=None, TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_IDS="123; -100500")
    assert s.chat_ids == [123, -100500]

    bot = AlertBot(s)
    text = bot.format_alert({
        "alert_type": "chamber_offline",
        "message": "Камера 1 <b>не</b> отвечает",
        "created_at": "2026-09-27T21:00:00+00:00",
    })
    assert "🔴 Камера отключилась" in text
    assert "&lt;b&gt;не&lt;/b&gt;" in text        # HTML из сообщения экранирован
    assert "28.09.2026 00:00:00" in text           # время в TIMEZONE (Москва)

    unknown = bot.format_alert({"alert_type": "future_type", "message": "x"})
    assert "Уведомление Climatica" in unknown
