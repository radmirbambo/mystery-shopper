"""
Переносит очередных ok-кандидатов в data/clinics.csv (новые id, авто-алиасы), перезапускает бэкенд,
печатает id для formbot. Сначала те, у кого на сайте есть имплантация.

    ./run.sh promote --n 30
    ./run.sh promote --n 30 --dry   # только показать, кого возьмёт
"""
from __future__ import annotations

import argparse
import subprocess
import os

from common import (CANDIDATES_CSV, CAND_FIELDS, CLINICS_CSV, CLINIC_FIELDS, make_aliases, read_csv, write_csv)


def pick(cands: list[dict], n: int) -> list[dict]:
    ready = [c for c in cands if c["status"] == "ok" and not c["clinic_id"]]
    ready.sort(key=lambda c: (c["implant"] != "1", -int(c["points"] or 0), c["domain"]))
    return ready[:n]


def to_clinic(c: dict, new_id: int) -> dict:
    return {"id": str(new_id), "name": c["name"], "site": c["site"], "phone": c["phone"],
            "phones_extra": c["phones_extra"], "priority": "1" if c["implant"] == "1" else "2",
            "aliases": make_aliases(c["name"], c["domain"])}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--ids-file", default="", help="записать id новых клиник через запятую в файл (для daily.sh)")
    args = ap.parse_args()
    cands = read_csv(CANDIDATES_CSV)
    clinics = read_csv(CLINICS_CSV)
    chosen = pick(cands, args.n)
    if not chosen:
        print("нет готовых кандидатов со статусом ok — запусти отсев")
        if args.ids_file:
            open(args.ids_file, "w").write("")
        return
    next_id = max(int(r["id"]) for r in clinics) + 1 if clinics else 1
    new_rows = []
    for c in chosen:
        row = to_clinic(c, next_id)
        c["clinic_id"] = str(next_id)
        new_rows.append(row)
        print(f'{row["id"]:>4} {row["name"]} — {row["site"]} {"[имплантация]" if c["implant"] == "1" else ""}')
        next_id += 1
    ids = ",".join(r["id"] for r in new_rows)
    left = sum(1 for c in cands if c["status"] == "ok" and not c["clinic_id"])
    if args.dry:
        print(f"\n(dry) взял бы {len(new_rows)}, осталось бы ok-кандидатов: {left}")
        return
    write_csv(CLINICS_CSV, clinics + new_rows, CLINIC_FIELDS)
    write_csv(CANDIDATES_CSV, cands, CAND_FIELDS)
    # бэкенд читает clinics.csv при старте — перезапускаем
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.mystery-shopper.backend"], check=False)
    if args.ids_file:
        open(args.ids_file, "w").write(ids)
    print(f"\nдобавлено {len(new_rows)}, осталось ok-кандидатов: {left}")
    print(f"запуск: ./run.sh formbot --ids {ids}")


if __name__ == "__main__":
    main()
