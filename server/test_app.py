"""Тесты бэкенда на временной базе: MS_DB_PATH/MS_CLINICS_CSV выставляются до импорта app. Запуск: .venv/bin/pytest -q server/"""
import csv
import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TMP = Path(tempfile.mkdtemp())
CSV = TMP / "clinics.csv"
with open(CSV, "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    w.writerow(["id", "name", "site", "phone", "phones_extra", "priority", "aliases"])
    w.writerow([1, "Стоматологический комплекс «Легенда»", "sk-legenda.ru", "+7 495 111-11-11", "", 1, "легенда"])
    w.writerow([2, "КДК Дент", "kdkdent.ru", "+7 495 222-22-22", "", 1, "кдк"])
os.environ["MS_DB_PATH"] = str(TMP / "test.db")
os.environ["MS_CLINICS_CSV"] = str(CSV)
os.environ["MS_TOKEN"] = "t"
sys.path.insert(0, str(Path(__file__).resolve().parent))
app_mod = importlib.import_module("app")
client = TestClient(app_mod.app)
H = {"X-Token": "t"}


def test_token_required():
    assert client.get("/report").status_code == 401


def test_submission_callback_delta_and_pending():
    assert client.post("/form-submitted", json={"clinic_id": 1, "ts": "2026-09-10T12:00:00+03:00"}, headers=H).status_code == 200
    pend = client.get("/pending", headers=H).json()
    assert [r["id"] for r in pend] == [1]
    client.post("/manual", json={"clinic_id": 1, "kind": "call", "ts": "2026-09-10T12:07:00+03:00"}, headers=H)
    row = next(r for r in client.get("/report", headers=H).json()["rows"] if r["id"] == 1)
    assert row["callback_minutes"] == 7.0 and row["kind"] == "call"
    assert client.get("/pending", headers=H).json() == []


def test_match_stemming_and_pending_bonus():
    client.post("/form-submitted", json={"clinic_id": 2, "ts": "2026-09-10T12:01:00+03:00"}, headers=H)
    c = client.get("/match", params={"q": "легенды"}, headers=H).json()
    assert c[0]["id"] == 1 and c[0]["score"] >= 85
    c = client.get("/match", params={"q": "кдк"}, headers=H).json()
    assert c[0]["id"] == 2 and c[0]["pending"] is True


def test_add_clinic_writes_csv_and_dedups():
    r = client.post("/clinics", json={"name": "Лазерная стоматология номер 1"}, headers=H).json()
    assert r["id"] == 3 and r["existed"] is False
    again = client.post("/clinics", json={"name": "лазерная стоматология номер 1"}, headers=H).json()
    assert again["id"] == 3 and again["existed"] is True
    rows = list(csv.DictReader(open(CSV, encoding="utf-8")))
    assert rows[-1]["name"] == "Лазерная стоматология номер 1" and rows[-1]["aliases"] == "лазерная номер 1"


def test_transcript_matches_by_time_and_lead_card():
    r = client.post("/transcript", headers=H, json={
        "ts": "2026-09-10T12:12:00+03:00", "text": "алло", "audio": "x.m4a",
        "analysis": {"admin_name": "Мария", "score": 8, "booked": "да", "quotes": ["Добрый день"], "summary": "ок"}}).json()
    assert r["clinic_id"] == 1
    assert client.post("/transcript", headers=H, json={"ts": "2026-09-10T15:00:00+03:00", "text": "x"}).status_code == 404
    client.post("/lpr", headers=H, json={"clinic_id": 1, "inn": "7701", "director": "Иванов Иван", "emails": "a@b.ru"})
    lead = client.get("/lead/1", headers=H).json()
    assert lead["lpr"]["director"] == "Иванов Иван" and lead["analysis"]["admin_name"] == "Мария"
    assert lead["measure"]["callback_minutes"] == 7.0
    row = next(r for r in client.get("/report", headers=H).json()["rows"] if r["id"] == 1)
    assert row["score"] == 8 and row["director"] == "Иванов Иван"


def test_lpr_merge_keeps_old_values():
    client.post("/lpr", headers=H, json={"clinic_id": 1, "inn": "", "director": "", "emails": "", "chief_doctor": "Сидоров П.И."})
    l = client.get("/lead/1", headers=H).json()["lpr"]
    assert l["director"] == "Иванов Иван" and l["inn"] == "7701" and l["chief_doctor"] == "Сидоров П.И."


def test_callbacks_listing():
    rows = client.get("/callbacks", params={"since": "2026-09-10T00:00:00+03:00"}, headers=H).json()
    assert rows and rows[0]["clinic"].startswith("Стоматологический") and rows[0]["analyzed"] == 1


def test_match_translit_and_generic_words():
    client.post("/clinics", json={"name": "Valident", "site": "valident.ru"}, headers=H)
    c = client.get("/match", params={"q": "валидент"}, headers=H).json()
    assert c[0]["name"] == "Valident" and c[0]["score"] >= 85
    c = client.get("/match", params={"q": "стоматология легенда"}, headers=H).json()
    assert c[0]["id"] == 1


def test_unknown_callback_then_attach():
    client.post("/manual", json={"clinic_id": None, "kind": "call", "ts": "2026-09-10T14:50:00+03:00", "clinic_said": "центр эстетической"}, headers=H)
    rep = client.get("/report", headers=H).json()
    assert len(rep["unmatched"]) == 1 and rep["unmatched"][0]["clinic_said"] == "центр эстетической"
    r = client.post("/manual", json={"clinic_id": 2, "kind": "call", "ts": "2026-09-10T14:51:00+03:00"}, headers=H).json()
    assert r.get("attached")
    rep = client.get("/report", headers=H).json()
    assert rep["unmatched"] == []
    row = next(x for x in rep["rows"] if x["id"] == 2)
    assert row["first_callback"].startswith("2026-09-10T14:50")
