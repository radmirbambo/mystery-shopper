"""Тесты чистых функций бота: разбор сообщения и выбор кандидата. Запуск: pytest telegram/"""
from datetime import datetime, timedelta, timezone

import pytest

from bot import MSK, decide, parse_message

BASE = datetime(2026, 9, 9, 19, 12, tzinfo=MSK)


def test_plain_name_uses_message_time():
    m = parse_message("Легенда", BASE)
    assert m.query == "легенда"
    assert m.kind == "call"
    assert m.ts == BASE


def test_explicit_time_overrides_message_time():
    m = parse_message("Легенда 14:35", BASE)
    assert m.query == "легенда"
    assert m.ts == BASE.replace(hour=14, minute=35, second=0, microsecond=0)


def test_explicit_time_with_dot():
    m = parse_message("Водники 9.05", BASE)
    assert m.ts.hour == 9 and m.ts.minute == 5


def test_whatsapp_keyword():
    for word in ("wa", "вотсап", "ватсап", "whatsapp", "WA"):
        m = parse_message(f"Легенда {word}", BASE)
        assert m.kind == "whatsapp", word
        assert m.query == "легенда"


def test_sms_keyword():
    m = parse_message("Легенда смс 18:00", BASE)
    assert m.kind == "sms"
    assert m.query == "легенда"
    assert m.ts.hour == 18


def test_unknown_marker():
    m = parse_message("?", BASE)
    assert m.unknown is True
    assert m.ts == BASE


def test_choice_digit():
    m = parse_message("2", BASE)
    assert m.choice == 2
    assert m.query == ""


def test_command():
    m = parse_message("/pending", BASE)
    assert m.command == "pending"


def test_message_time_converted_to_msk():
    utc = datetime(2026, 9, 9, 16, 12, tzinfo=timezone.utc)
    m = parse_message("Легенда", utc)
    assert m.ts.tzinfo == MSK and m.ts.hour == 19


# ---- decide: авто-выбор или уточнение ----

def test_decide_auto_when_confident():
    cands = [{"id": 6, "name": "Легенда", "score": 95, "pending": True},
             {"id": 7, "name": "КДК Дент", "score": 40, "pending": True}]
    assert decide(cands) == 6


def test_decide_asks_when_close_scores():
    cands = [{"id": 6, "name": "Легенда", "score": 90, "pending": True},
             {"id": 7, "name": "Легенда+", "score": 88, "pending": True}]
    assert decide(cands) is None


def test_decide_asks_when_low_score():
    cands = [{"id": 6, "name": "Легенда", "score": 70, "pending": True}]
    assert decide(cands) is None


def test_decide_none_when_empty():
    assert decide([]) is None


# ---- новые форматы (09.09 вечер) ----

def test_time_with_space_and_v_prefix():
    assert parse_message("Легенда 21 53", BASE).ts.strftime("%H:%M") == "21:53"
    assert parse_message("Легенда в 21:53", BASE).ts.strftime("%H:%M") == "21:53"
    assert parse_message("Легенда 21-53", BASE).ts.strftime("%H:%M") == "21:53"


def test_number_in_name_is_not_time():
    m = parse_message("лазерная стоматолоогия номер 1", BASE)
    assert m.query == "лазерная стоматолоогия номер 1"
    assert m.has_time is False


def test_filler_words_and_time_only_message():
    m = parse_message("позвонила сейчас 21 53", BASE)
    assert m.query == ""
    assert m.has_time and m.ts.strftime("%H:%M") == "21:53"


def test_filler_words_keep_clinic_name():
    m = parse_message("сейчас звонили из Легенды", BASE)
    assert m.query == "легенды"


def test_confirm_words():
    for w in ("да", "Да!", "ага", "+", "новая"):
        assert parse_message(w, BASE).confirm is True, w


def test_pretty_name():
    from bot import pretty_name
    assert pretty_name("лазерная  стоматология номер 1") == "Лазерная стоматология номер 1"


def test_done_parsing():
    assert parse_message("готово 1 3", BASE).done == [1, 3]
    assert parse_message("Готово", BASE).done == []
    assert parse_message("оставил 2", BASE).done == [2]
    assert parse_message("готовость клиника", BASE).done is None


def test_negative_and_wave_and_not_a_clinic():
    assert parse_message("нет", BASE).negative is True
    assert parse_message("Отмени", BASE).negative is True
    assert parse_message("давай на 13:00 первую волну перенесем", BASE).wave_at == "13:00"
    assert parse_message("волну отмени", BASE).wave_at == ""
    assert parse_message("давай запусти заявки", BASE).query == ""
    assert parse_message("Кариесу.нет", BASE).query == "кариесу.нет"


def test_raw_keeps_generic_words_for_new_clinic_name():
    m = parse_message("сейчас звонили из Лазерной стоматологии номер 1, wa", BASE)
    assert m.query == "лазерной номер 1"
    assert m.raw == "сейчас звонили из Лазерной стоматологии номер 1"
    assert m.kind == "whatsapp"
    m2 = parse_message("Лазерная стоматология номер 1", BASE)
    assert m2.raw == "Лазерная стоматология номер 1"


def test_phone_in_message_and_suspicious_words():
    m = parse_message("+7 495 183 84 25 что-то около новадент позвонила мне", BASE)
    assert m.phone == "4951838425"
    assert m.query == "новадент"
    assert m.has_time is False
    m2 = parse_message("давай запусти заявки", BASE)
    assert m2.query == "" and m2.suspicious is True
    m3 = parse_message("что-то около Легенды", BASE)
    assert m3.query == "легенды"
