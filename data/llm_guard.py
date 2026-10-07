"""Общий предохранитель для бесплатной модели OpenRouter: после 429 молчим COOLDOWN секунд (файл-флаг data/.llm_429)."""
import time
from pathlib import Path

FLAG = Path(__file__).resolve().parent / ".llm_429"
COOLDOWN = 15 * 60


def llm_available() -> bool:
    try:
        return time.time() - FLAG.stat().st_mtime > COOLDOWN
    except FileNotFoundError:
        return True


def note_429() -> None:
    FLAG.touch()
