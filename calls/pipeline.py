"""
Конвейер записи звонка: аудио → транскрипт (Whisper локально) → разбор моделью → бэкенд → Луиза.

    ./run.sh call path/to/audio.m4a [--clinic-id N] [--ts ISO]

Без --clinic-id бэкенд сопоставит запись с ближайшим перезвоном по времени (±20 мин).
Env: OPENROUTER_API_KEY, MS_LLM_MODEL, MS_WHISPER_MODEL (по умолчанию mlx-community/whisper-large-v3-turbo).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
from llm_guard import llm_available, note_429  # noqa: E402

MSK = timezone(timedelta(hours=3))
ROOT = Path(__file__).resolve().parent
STORE = ROOT.parent / "data" / "recordings"
BACKEND = os.environ.get("MS_BACKEND", "http://127.0.0.1:8000/").rstrip("/") + "/"
MS_TOKEN = os.environ.get("MS_TOKEN", "change-me")
OPENROUTER_KEY = os.environ.get("MS_LLM_KEY") or os.environ.get("OPENROUTER_API_KEY", "") or "local"
LLM_MODEL = os.environ.get("MS_LLM_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
LLM_BASE = os.environ.get("MS_LLM_BASE", "https://openrouter.ai/api/v1").rstrip("/")
WHISPER_MODEL = os.environ.get("MS_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")


# ---------- чистые функции (тесты) ----------

def parse_analysis(text: str) -> Optional[dict]:
    """Последний JSON-объект из ответа модели; нормализуем поля."""
    objs = re.findall(r"\{[^{}]*\}", text or "", re.S)   # плоские объекты; модель может сначала порассуждать — берём последний валидный
    for cand in reversed(objs):
        try:
            d = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        out = {
            "admin_name": (d.get("admin_name") or "") or None,
            "offered": d.get("offered") or "",
            "booked": {True: "да", False: "нет"}.get(d.get("booked"), str(d.get("booked") or "")),
            "tone": d.get("tone") or "",
            "score": _num(d.get("score")),
            "quotes": [q for q in (d.get("quotes") or []) if isinstance(q, str)][:3],
            "summary": d.get("summary") or "",
        }
        return out
    return None


def _num(v) -> Optional[float]:
    try:
        return max(0.0, min(10.0, float(v)))
    except (TypeError, ValueError):
        return None


def describe(a: dict, clinic: str) -> str:
    """Короткий разбор для Луизы."""
    parts = [f"Разбор звонка — {clinic}."]
    if a.get("admin_name"):
        parts.append(f"Администратор: {a['admin_name']}.")
    if a.get("score") is not None:
        parts.append(f"Оценка {a['score']:.0f}/10.")
    if a.get("booked"):
        parts.append(f"Записали на приём: {a['booked']}.")
    if a.get("summary"):
        parts.append(a["summary"])
    if a.get("quotes"):
        parts.append("Цитаты: " + " | ".join(f"«{q}»" for q in a["quotes"]))
    return " ".join(parts)


# ---------- шаги ----------

def to_wav(src: Path) -> Path:
    STORE.mkdir(parents=True, exist_ok=True)
    dst = STORE / (src.stem + ".wav")
    if not dst.exists():
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "16000", str(dst)], check=True)
    return dst


def transcribe(audio: Path) -> str:
    import mlx_whisper  # тяжёлый импорт — только здесь
    res = mlx_whisper.transcribe(str(audio), path_or_hf_repo=WHISPER_MODEL, language="ru",
                                 initial_prompt="Телефонный разговор пациента и администратора стоматологии.")
    return (res.get("text") or "").strip()


_PROMPT = (
    "Ниже транскрипт телефонного разговора: администратор стоматологии перезванивает пациенту по заявке "
    "«консультация по имплантации» с сайта. Оцени работу администратора. Верни ТОЛЬКО JSON, начни ответ с {:\n"
    '{"admin_name": имя администратора или null, "offered": что предложили (кратко), '
    '"booked": true|false — пытались ли записать на конкретное время, "tone": "вежливый|нейтральный|сухой|грубый", '
    '"score": 0-10 (скорость реакции, выявление потребности, предложение записи, вежливость), '
    '"quotes": [до 3 коротких цитат администратора, характерных для разговора], '
    '"summary": 1-2 предложения для письма руководителю клиники, без оценочных ярлыков}\n\nТранскрипт:\n'
)


def analyze(text: str) -> Optional[dict]:
    if not OPENROUTER_KEY or not text.strip() or not llm_available():
        return None
    try:
        r = httpx.post(LLM_BASE + "/chat/completions",
                       headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
                       json={"model": LLM_MODEL, "max_tokens": 900, "temperature": 0,
                             "messages": [{"role": "user", "content": _PROMPT + text[:12000]}]}, timeout=90)
        if r.status_code == 429 or (r.status_code == 200 and "error" in r.json() and r.json()["error"].get("code") == 429):
            note_429()
            print("llm: 429, пауза 15 мин", file=sys.stderr)
            return None
        return parse_analysis(r.raise_for_status().json()["choices"][0]["message"]["content"] or "")
    except Exception as e:
        print("llm error:", repr(e)[:120], file=sys.stderr)
        return None


def push(text: str, analysis: Optional[dict], audio: str, clinic_id: Optional[int], ts: Optional[str]) -> dict:
    r = httpx.post(BACKEND + "transcript", headers={"X-Token": MS_TOKEN}, timeout=30,
                   json={"clinic_id": clinic_id, "ts": ts, "text": text, "analysis": analysis, "audio": audio})
    r.raise_for_status()
    return r.json()


def notify(text: str) -> None:
    subprocess.run([str(ROOT.parent / "run.sh"), "notify", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def process(audio: Path, clinic_id: Optional[int] = None, ts: Optional[str] = None, quiet: bool = False) -> dict:
    if ts is None:
        ts = datetime.fromtimestamp(audio.stat().st_mtime, tz=MSK).isoformat()
    wav = to_wav(audio)
    text = transcribe(wav)
    analysis = analyze(text)
    res = push(text, analysis, str(audio), clinic_id, ts)
    clinic = res.get("clinic_name") or "клиника не определена"
    msg = describe(analysis, clinic) if analysis else f"Транскрипт записан ({clinic}), разбор моделью не удался."
    if not quiet:
        notify(msg)
    return {"clinic": clinic, "text": text, "analysis": analysis, "message": msg}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--clinic-id", type=int)
    ap.add_argument("--ts")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    res = process(Path(a.audio), a.clinic_id, a.ts, a.quiet)
    print(res["message"])
    print("---\n" + res["text"][:1500])


if __name__ == "__main__":
    main()
