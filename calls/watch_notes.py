"""
Наблюдатель за записями звонков iPhone: iOS кладёт их в Заметки → iCloud → контейнер Заметок на Маке.
Новые аудиофайлы прогоняем через pipeline.process, клинику бэкенд подбирает по времени звонка.
Запускается launchd каждые 2 минуты (launchd/com.mystery-shopper.calls.plist) или руками: ./run.sh watch-calls
Состояние (уже обработанные файлы) — calls/seen.json.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline import MSK, process, notify

NOTES = Path.home() / "Library" / "Group Containers" / "group.com.apple.notes"
SEEN = Path(__file__).resolve().parent / "seen.json"
AUDIO_EXT = {".m4a", ".caf", ".mp3", ".wav", ".aac"}
MIN_AGE_SEC = 20        # файл ещё может докачиваться из iCloud
MAX_AGE_DAYS = 7        # старые записи не трогаем


def new_files() -> list[Path]:
    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    now = time.time()
    out = []
    for p in NOTES.rglob("*"):
        if p.suffix.lower() not in AUDIO_EXT or str(p) in seen:
            continue
        age = now - p.stat().st_mtime
        if age < MIN_AGE_SEC or age > MAX_AGE_DAYS * 86400:
            continue
        out.append(p)
    return sorted(out, key=lambda p: p.stat().st_mtime)


def mark(p: Path) -> None:
    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    seen.add(str(p))
    SEEN.write_text(json.dumps(sorted(seen)))


def main() -> None:
    files = new_files()
    if not files:
        return
    for p in files:
        ts = datetime.fromtimestamp(p.stat().st_mtime, tz=MSK).isoformat()
        print(f"{p.name} ({ts})", flush=True)
        try:
            res = process(p, ts=ts)
            print("  →", res["clinic"], flush=True)
        except Exception as e:
            print("  error:", repr(e)[:200], flush=True)
            if "no matching callback" in repr(e):
                notify(f"Нашла запись звонка от {ts[11:16]}, но не поняла, чья она. Напиши название клиники со временем, "
                       f"например «Легенда {ts[11:16]}», и перешли мне эту запись.")
        mark(p)


if __name__ == "__main__":
    sys.exit(main())
