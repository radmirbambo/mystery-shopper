"""Отправить владельцу бота сообщение от имени Луизы: ./run.sh notify "текст" (или stdin)."""
import json
import os
import sys
from pathlib import Path

import httpx

text = " ".join(sys.argv[1:]) or sys.stdin.read()
state = json.loads((Path(__file__).resolve().parent / "state.json").read_text())
owner = state.get("owner_id")
token = os.environ.get("MS_TG_TOKEN", "")
if not (owner and token and text.strip()):
    sys.exit("нет владельца/токена/текста")
httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
           json={"chat_id": owner, "text": text[:4000]}, timeout=30).raise_for_status()
