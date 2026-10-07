"""Общее для discover/: пути, нормализация доменов и телефонов, алиасы, чтение/запись candidates.csv."""
from __future__ import annotations

import csv
import re
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DATA = ROOT.parent / "data"
CLINICS_CSV = DATA / "clinics.csv"
CANDIDATES_CSV = DATA / "candidates.csv"

CAND_FIELDS = ["domain", "name", "site", "phone", "phones_extra", "address", "points", "osm_ids",
               "status", "note", "implant", "triaged_at", "clinic_id"]
CLINIC_FIELDS = ["id", "name", "site", "phone", "phones_extra", "priority", "aliases"]

GENERIC_WORDS = {"стоматология", "стоматологическая", "стоматологический", "стоматологии", "клиника", "центр",
                 "медицинский", "медицинская", "семейная", "семейный", "ооо", "зао", "ао", "ип", "на", "и",
                 "dental", "clinic", "стом", "зубная", "зубной", "кабинет", "поликлиника", "им."}
SKIP_DOMAINS = {"vk.com", "instagram.com", "facebook.com", "t.me", "yandex.ru", "2gis.ru", "zoon.ru",
                "prodoctorov.ru", "taplink.cc", "wa.me", "ok.ru"}


def normalize_domain(url: str) -> str:
    """'https://www.Site.ru/path' → 'site.ru'; пусто, если это соцсеть/агрегатор или мусор."""
    u = (url or "").strip()
    if not u:
        return ""
    if "://" not in u:
        u = "http://" + u
    host = urlparse(u).netloc.lower().split("@")[-1].split(":")[0]
    host = host.removeprefix("www.")
    if not host or "." not in host or host in SKIP_DOMAINS or any(host.endswith("." + s) for s in SKIP_DOMAINS):
        return ""
    return host


def normalize_phone(p: str) -> str:
    """Любой формат → '+7 XXX XXX-XX-XX' для российских номеров, иначе пусто."""
    digits = re.sub(r"\D", "", p or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return ""
    return f"+{digits[0]} {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


def make_aliases(name: str, domain: str) -> str:
    """Короткие варианты названия для нечёткого поиска: без родовых слов, плюс корень домена."""
    clean = re.sub(r"[«»\"'()№]", " ", (name or "").lower())
    words = [w.strip(" ,.-") for w in clean.split()]
    core = [w for w in words if w and w not in GENERIC_WORDS]
    out: list[str] = []
    if core and core != words:
        out.append(" ".join(core))
    root = domain.split(".")[0] if domain else ""
    if root and len(root) >= 4 and root not in out and root != clean.strip():
        out.append(root)
    return "; ".join(out)


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    tmp.replace(path)


def known_domains() -> set[str]:
    return {normalize_domain(r["site"]) for r in read_csv(CLINICS_CSV) if r.get("site")} - {""}
