#!/usr/bin/env bash
# Ежедневная волна без участия человека: берёт очередных N ok-кандидатов, оставляет заявки, отчитывается Луизой.
# Запускает launchd (launchd/com.mystery-shopper.daily.plist) по будням в 12:00, либо руками: ./daily.sh [N]
# Пауза: touch data/PAUSE (или /pause Луизе). Снять: rm data/PAUSE (или /resume).
set -uo pipefail
cd "$(dirname "$0")"
set -a; source local.env; set +a
export PYTHONUNBUFFERED=1   # живой лог formbot
N="${1:-${MS_WAVE_SIZE:-25}}"
mkdir -p logs
LOG="logs/wave_$(date +%Y-%m-%d_%H%M).log"
notify() { if [ -n "${MS_QUIET:-}" ]; then echo "[notify] $1" >> "$LOG"; else ./run.sh notify "$1" >/dev/null 2>&1 || true; fi; }

if [ -f data/PAUSE ]; then echo "PAUSE — волна пропущена" | tee -a "$LOG"; exit 0; fi
# перенос на сегодня («волну на 13:00» Луизе): ждём до указанного времени
if [ -f data/WAVE_AT ]; then
  AT=$(cat data/WAVE_AT); TARGET=$(date -j -f "%Y-%m-%d %H:%M" "$(date +%Y-%m-%d) $AT" +%s 2>/dev/null || echo 0)
  NOW=$(date +%s)
  if [ "$TARGET" -gt "$NOW" ]; then echo "ждём до $AT" >> "$LOG"; sleep $((TARGET - NOW)); fi
  rm -f data/WAVE_AT
fi

# 1. пополнить список (если идёт отсев — он переписывает candidates.csv, promote пропускаем)
if [ -n "${MS_WAVE_IDS:-}" ]; then
  echo "MS_WAVE_IDS задан — promote пропущен" >> "$LOG"; echo "$MS_WAVE_IDS" > data/wave_ids.txt
elif pgrep -f discover/triage.py >/dev/null; then
  echo "triage идёт — promote пропущен" >> "$LOG"; : > data/wave_ids.txt
else
  ./run.sh promote --n "$N" --ids-file data/wave_ids.txt >> "$LOG" 2>&1
fi
IDS=$(cat data/wave_ids.txt)
# проверенные вручную сайты из первоначального списка: formbot сам пропустит те, где заявка уже была
if [ -f data/vetted_ids.txt ]; then IDS="$(cat data/vetted_ids.txt | tr -d '[:space:]')${IDS:+,$IDS}"; fi
if [ -z "$IDS" ]; then
  notify "Волна не запущена: кончились проверенные кандидаты. Нужен новый отсев (./run.sh discover && ./run.sh triage)."
  exit 0
fi
notify "Запускаю волну: $(echo "$IDS" | tr ',' '\n' | wc -l | tr -d ' ') клиник, id $IDS. Жди звонков на ${MS_PHONE:-твой номер}."

# 2. заявки (MS_DRY=1 — репетиция без отправки)
START_TS=$(date +%s)
# caffeinate: Мак не уснёт, пока идёт волна (в 13:00 10.09 волна «зависла» именно из-за сна)
caffeinate -i ./run.sh formbot --auto --headless ${MS_DRY:+--dry} --ids "$IDS" >> "$LOG" 2>&1
RC=$?

# 3. отчёт по этой волне из formbot_log.csv
SUMMARY=$(python3 - "$START_TS" <<'EOF'
import csv, sys
from collections import Counter
from datetime import datetime, timezone
start = datetime.fromtimestamp(int(sys.argv[1]), tz=timezone.utc)
rows = [r for r in csv.DictReader(open("formbot/formbot_log.csv", encoding="utf-8"))
        if datetime.fromisoformat(r["ts"]) >= start]
c = Counter(r["status"] for r in rows)
sent = [r["clinic"] for r in rows if r["status"].startswith("submitted") or r["status"] == "dry"]
skipped = [f'{r["clinic"]} ({r["note"][:40]})' for r in rows if r["status"] in ("skipped", "error")]
out = [f"Волна закончена: отправлено {len(sent)}, пропущено {len(skipped)} из {len(rows)}."]
if sent: out.append("Ждём звонков от: " + ", ".join(sent))
if skipped: out.append("Не вышло: " + "; ".join(skipped[:10]))
print("\n".join(out))
EOF
)
notify "$SUMMARY"
[ "$RC" -ne 0 ] && notify "formbot завершился с ошибкой (код $RC), смотри $LOG"

# 4. карточки ЛПР по клиникам этой волны (сайт + DaData, если есть ключ) — фоном, не держим волну
[ -z "${MS_DRY:-}" ] && nohup ./run.sh lpr --ids "$IDS" >> "$LOG" 2>&1 &
exit 0
