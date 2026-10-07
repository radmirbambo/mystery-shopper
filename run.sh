#!/usr/bin/env bash
# Запуск компонентов локального стека с env из local.env.
#   ./run.sh backend   — FastAPI на 127.0.0.1:8000
#   ./run.sh bot       — telegram-бот
#   ./run.sh formbot [--dry]  — заявки на сайты
#   ./run.sh discover | triage [--limit N] | promote --n 30  — поиск и отбор новых клиник (см. discover/)
# launchd вызывает первые два (см. launchd/*.plist), formbot запускаешь руками.
set -euo pipefail
cd "$(dirname "$0")"
[ -f local.env ] || { echo "нет local.env — см. local.env.example"; exit 1; }
set -a; source local.env; set +a
case "${1:-}" in
  backend) exec server/.venv/bin/uvicorn app:app --app-dir server --host 127.0.0.1 --port 8000 ;;
  bot)     exec telegram/.venv/bin/python telegram/bot.py ;;
  formbot) shift; cd formbot && exec .venv/bin/python fill_forms.py "$@" ;;
  discover) exec formbot/.venv/bin/python discover/fetch_osm.py ;;
  triage)  shift; exec formbot/.venv/bin/python discover/triage.py "$@" ;;
  promote) shift; exec formbot/.venv/bin/python discover/promote.py "$@" ;;
  notify)  shift; exec telegram/.venv/bin/python telegram/notify.py "$@" ;;
  call)    shift; cd calls && exec .venv/bin/python pipeline.py "$@" ;;
  llm)     exec calls/.venv/bin/python -m mlx_lm.server --model "${MS_LOCAL_LLM:-calls/models/qwen2.5-3b-instruct-4bit}" --host 127.0.0.1 --port 8080 ;;
  watch-calls) cd calls && exec .venv/bin/python watch_notes.py ;;
  lpr)     shift; exec formbot/.venv/bin/python discover/lpr.py "$@" ;;
  daily)   shift; exec ./daily.sh "$@" ;;
  *) echo "usage: ./run.sh backend|bot|formbot|discover|triage|promote|notify|daily|call|watch-calls|lpr|llm [args]"; exit 1 ;;
esac
