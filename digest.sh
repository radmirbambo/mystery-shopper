#!/usr/bin/env bash
# Утренний дайджест от Луизы (launchd com.mystery-shopper.digest, будни 09:00): кто не перезвонил за сутки,
# сводка по замерам. Руками: ./digest.sh
set -uo pipefail
cd "$(dirname "$0")"
set -a; source local.env; set +a
TEXT=$(server/.venv/bin/python - <<'EOF'
import os, httpx
from datetime import datetime, timezone, timedelta
MSK = timezone(timedelta(hours=3))
c = httpx.Client(base_url=os.environ["MS_BACKEND"], headers={"X-Token": os.environ["MS_TOKEN"]}, timeout=30)
rep = c.get("report").json()
s, rows = rep["summary"], rep["rows"]
if not s["submitted"]:
    print("Доброе утро. Заявок пока не было — первая волна пойдёт в 12:00.")
    raise SystemExit
day = [r for r in rows if r["submitted_at"] and not r["first_callback"] and r["waiting_minutes"] and r["waiting_minutes"] >= 24 * 60]
slow = [r for r in rows if r["callback_minutes"] is not None and r["callback_minutes"] > 30]
fast = [r for r in rows if r["callback_minutes"] is not None and r["callback_minutes"] <= 5]
out = [f"Доброе утро. Замеров: {s['submitted']}, перезвонили {s['called_back']}, медиана {s['median_minutes'] or '—'} мин, "
       f"в 5 минут уложились {s['within_5_min']}."]
if day:
    out.append(f"Не перезвонили больше суток ({len(day)}) — самые тёплые лиды:\n" + "\n".join(f"• {r['clinic']}" for r in day[:15]))
if slow:
    out.append(f"Перезвонили дольше 30 мин ({len(slow)}):\n" + "\n".join(f"• {r['clinic']} — {r['callback_minutes']:.0f} мин" for r in slow[:10]))
if fast:
    out.append("Быстрые (до 5 мин): " + ", ".join(r["clinic"] for r in fast[:10]))
out.append("Карточка любого: /lead Название. Полный отчёт: /report")
print("\n\n".join(out))
EOF
)
./run.sh notify "$TEXT" >/dev/null 2>&1 && echo "digest sent" || echo "digest failed"
