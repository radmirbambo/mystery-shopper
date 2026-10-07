"""
Отсев кандидатов: сухой прогон поиска формы из formbot по каждому сайту из data/candidates.csv.
Ставит status: ok | captcha | no_form | mask_fail | dead, флаг implant (слово «имплант» на главной).
Пишет результат построчно — можно прерывать и перезапускать, уже отсеянные пропускаются.

    ./run.sh triage            # все без статуса
    ./run.sh triage --limit 10 # дымовой тест
    ./run.sh triage --redo     # заново всех
    ./run.sh triage --only no_form,dead   # перепроверить после улучшений formbot
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "formbot"))
import fill_forms as ff  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from common import CANDIDATES_CSV, CAND_FIELDS, read_csv, write_csv  # noqa: E402

MSK = timezone(timedelta(hours=3))
ff.SHOTS = Path(__file__).resolve().parent / "screenshots"
ff.SHOTS.mkdir(exist_ok=True)


def classify(status: str, note: str) -> str:
    if status == "dry":
        return "ok"
    if "captcha" in note:
        return "captcha"
    if "phone mask" in note:
        return "mask_fail"
    if "form not found" in note:
        return "no_form"
    return "dead"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--only", default="", help="перепроверить только эти статусы, например no_form,dead")
    args = ap.parse_args()
    rows = read_csv(CANDIDATES_CSV)
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    todo = [r for r in rows if (r["status"] in only) if only] if only else [r for r in rows if args.redo or not r["status"]]
    if "dead" in only:
        ff.GOTO_TIMEOUT = max(ff.GOTO_TIMEOUT, 45000)
    if args.limit:
        todo = todo[: args.limit]
    print(f"к отсеву: {len(todo)} из {len(rows)}", flush=True)
    dry_args = SimpleNamespace(dry=True, auto=True, headless=not args.headed)
    done = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(
            locale="ru-RU", viewport={"width": 1280, "height": 900},
            user_agent=ff.USER_AGENT, ignore_https_errors=True,
        )
        for r in todo:
            page = ctx.new_page()
            page.set_default_timeout(15000)
            clinic = {"id": "c_" + r["domain"], "name": r["name"], "site": r["site"]}
            t0 = time.time()
            try:
                status, note = ff.process_site(page, clinic, dry_args)
            except Exception as e:
                status, note = "error", repr(e)[:160]
            try:
                r["implant"] = "1" if "имплант" in page.content().lower() else "0"
            except Exception:
                r["implant"] = ""
            page.close()
            r["status"] = classify(status, note)
            r["note"] = note[:200]
            r["triaged_at"] = datetime.now(MSK).isoformat(timespec="minutes")
            done += 1
            print(f'[{done}/{len(todo)}] {r["domain"]} → {r["status"]} ({note[:80]}) {time.time()-t0:.0f}s', flush=True)
            write_csv(CANDIDATES_CSV, rows, CAND_FIELDS)
        browser.close()
    from collections import Counter
    print("итог:", dict(Counter(r["status"] for r in rows if r["status"])))


if __name__ == "__main__":
    main()
