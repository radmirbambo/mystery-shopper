"""
Собирает стоматологии Москвы из OpenStreetMap (Overpass API) в data/candidates.csv, по одной строке на домен.
Сети (много точек с одним сайтом) — одна строка, points = число точек. Домены из clinics.csv пропускаются.
Повторный запуск дописывает новые домены и не трогает status/note уже отсеянных.

    ./run.sh discover
"""
from __future__ import annotations

import sys

import httpx

from common import (CANDIDATES_CSV, CAND_FIELDS, known_domains, normalize_domain, normalize_phone,
                    read_csv, write_csv)

OVERPASS = "https://overpass-api.de/api/interpreter"
QUERY = """
[out:json][timeout:180];
area["name"="Москва"]["admin_level"="4"]->.msk;
(
  nwr["amenity"="dentist"](area.msk);
  nwr["healthcare"="dentist"](area.msk);
);
out center tags;
"""


def fetch() -> list[dict]:
    r = httpx.post(OVERPASS, content=QUERY.encode(), headers={"User-Agent": "mystery-shopper/1.0"}, timeout=240)
    return r.raise_for_status().json()["elements"]


def group_by_domain(elements: list[dict]) -> dict[str, dict]:
    groups: dict[str, dict] = {}
    seen: set[tuple] = set()
    for e in elements:
        key = (e["type"], e["id"])
        if key in seen:
            continue
        seen.add(key)
        t = e.get("tags", {})
        domain = normalize_domain(t.get("website") or t.get("contact:website") or t.get("url") or "")
        if not domain:
            continue
        phone = normalize_phone(t.get("phone") or t.get("contact:phone") or "")
        addr = " ".join(x for x in (t.get("addr:street"), t.get("addr:housenumber")) if x)
        g = groups.setdefault(domain, {"domain": domain, "names": [], "phones": [], "addresses": [], "osm_ids": []})
        if t.get("name"):
            g["names"].append(t["name"])
        if phone:
            g["phones"].append(phone)
        if addr:
            g["addresses"].append(addr)
        g["osm_ids"].append(f'{e["type"][0]}{e["id"]}')
    out = {}
    for d, g in groups.items():
        # самое частое название группы
        name = max(set(g["names"]), key=g["names"].count) if g["names"] else d
        phones = list(dict.fromkeys(g["phones"]))
        out[d] = {
            "domain": d, "name": name, "site": d, "phone": phones[0] if phones else "",
            "phones_extra": ";".join(phones[1:5]), "address": g["addresses"][0] if g["addresses"] else "",
            "points": str(len(g["osm_ids"])), "osm_ids": ";".join(g["osm_ids"][:10]),
            "status": "", "note": "", "implant": "", "triaged_at": "", "clinic_id": "",
        }
    return out


def main() -> None:
    print("запрашиваю Overpass…", flush=True)
    elements = fetch()
    fresh = group_by_domain(elements)
    known = known_domains()
    existing = {r["domain"]: r for r in read_csv(CANDIDATES_CSV)}
    added = 0
    for d, row in fresh.items():
        if d in known:
            continue
        if d in existing:
            # обновляем только справочные поля, статус отсева сохраняем
            for k in ("name", "phone", "phones_extra", "address", "points", "osm_ids"):
                existing[d][k] = row[k]
        else:
            existing[d] = row
            added += 1
    rows = sorted(existing.values(), key=lambda r: (-int(r["points"] or 0), r["domain"]))
    write_csv(CANDIDATES_CSV, rows, CAND_FIELDS)
    print(f"объектов в OSM: {len(elements)}, доменов: {len(fresh)}, уже в clinics.csv: {len(known & set(fresh))}, "
          f"новых добавлено: {added}, всего кандидатов: {len(rows)}")


if __name__ == "__main__":
    sys.exit(main())
