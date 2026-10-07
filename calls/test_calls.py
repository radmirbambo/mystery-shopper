"""pytest calls/  (без модели и без бэкенда — только чистые функции)"""
from pipeline import describe, parse_analysis


def test_parse_analysis_takes_last_json_and_normalizes():
    text = ('Думаю... {"draft": 1} Итог: {"admin_name": "Мария", "offered": "консультация", "booked": true, '
            '"tone": "вежливый", "score": "8", "quotes": ["Добрый день", 5, "Запишу вас"], "summary": "ok"}')
    a = parse_analysis(text)
    assert a["admin_name"] == "Мария"
    assert a["booked"] == "да"
    assert a["score"] == 8.0
    assert a["quotes"] == ["Добрый день", "Запишу вас"]


def test_parse_analysis_clamps_score_and_handles_null():
    a = parse_analysis('{"admin_name": null, "score": 14, "booked": false}')
    assert a["admin_name"] is None and a["score"] == 10.0 and a["booked"] == "нет"


def test_parse_analysis_garbage():
    assert parse_analysis("модель ничего не вернула") is None
    assert parse_analysis("") is None


def test_describe():
    s = describe({"admin_name": "Мария", "score": 7, "booked": "да", "summary": "Предложили завтра.", "quotes": ["Добрый день"]}, "Легенда")
    assert s.startswith("Разбор звонка — Легенда.")
    assert "Мария" in s and "7/10" in s and "«Добрый день»" in s
