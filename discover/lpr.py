"""
Поиск ЛПР клиники: обход сайта (о клинике, контакты, команда, лицензии, политика ПДн) → ИНН/ОГРН, почты, телефоны,
люди с должностями (регулярки + модель) → DaData по ИНН (юрлицо, руководитель) → POST /lpr в бэкенд.

    ./run.sh lpr --ids 6,7          # конкретные клиники
    ./run.sh lpr --measured         # все, по кому есть замер (заявка оставлена)
Env: OPENROUTER_API_KEY (модель для имён/должностей), DADATA_TOKEN (необязательно; без него — только сайт).
"""
from __future__ import annotations

import argparse
import html as htmlmod
import json
import os
import re
import sys
from urllib.parse import urljoin, urlparse

import httpx

from common import CLINICS_CSV, read_csv

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "data"))
from llm_guard import llm_available, note_429  # noqa: E402

BACKEND = os.environ.get("MS_BACKEND", "http://127.0.0.1:8000/").rstrip("/") + "/"
MS_TOKEN = os.environ.get("MS_TOKEN", "change-me")
OPENROUTER_KEY = os.environ.get("MS_LLM_KEY") or os.environ.get("OPENROUTER_API_KEY", "") or "local"
LLM_MODEL = os.environ.get("MS_LLM_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
LLM_BASE = os.environ.get("MS_LLM_BASE", "https://openrouter.ai/api/v1").rstrip("/")
DADATA_TOKEN = os.environ.get("DADATA_TOKEN", "")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
PAGE_HINTS = ["about", "o-klinike", "o-nas", "kontakt", "contact", "team", "komanda", "vrachi", "doctors", "specialist",
              "rukovod", "licen", "policy", "politika", "privacy", "personal", "rekvizit", "documents", "dokument"]
ROLE_WORDS = ["главный врач", "главврач", "директор", "генеральный директор", "управляющ", "руководител",
              "заведующ", "основател", "владелец", "учредител", "медицинский директор", "исполнительный директор"]
BAD_EMAIL = ("sentry", "example", "wixpress", "png", "jpg", "svg", "webp", "gif", "noreply", "no-reply",
             "rospotrebnadzor", "roskomnadzor", "gov.ru", "mos.ru", "nalog.ru", "rkn.", "@yandex-team", "support@")


# ---------- чистые функции (тесты) ----------

def strip_html(h: str) -> str:
    h = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", htmlmod.unescape(h)).strip()


def find_inn(text: str) -> str:
    m = re.search(r"ИНН\D{0,12}(\d{10}|\d{12})(?!\d)", text)
    return m.group(1) if m else ""


def find_ogrn(text: str) -> str:
    m = re.search(r"ОГРН(?:ИП)?\D{0,12}(\d{13}|\d{15})(?!\d)", text)
    return m.group(1) if m else ""


def find_emails(text: str) -> list[str]:
    out = []
    for e in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text):
        e = e.strip(".").lower()
        if any(b in e for b in BAD_EMAIL) or e in out:
            continue
        out.append(e)
    return out[:6]


def role_snippets(text: str, width: int = 90) -> list[str]:
    """Кусочки текста вокруг слов-должностей — только их отдаём модели."""
    out = []
    low = text.lower()
    for w in ROLE_WORDS:
        for m in re.finditer(re.escape(w), low):
            s = text[max(0, m.start() - width): m.end() + width]
            if s not in out:
                out.append(s)
            if len(out) >= 25:
                return out
    return out


def pick_links(base: str, h: str) -> list[str]:
    links = []
    for href in re.findall(r'href=["\']([^"\']+)["\']', h, re.I):
        u = urljoin(base, href.split("#")[0])
        if urlparse(u).netloc != urlparse(base).netloc:
            continue
        path = urlparse(u).path.lower()
        if any(k in path for k in PAGE_HINTS) and u not in links:
            links.append(u)
    return links[:10]


_NAME = r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?"
_ROLE_RE = re.compile(
    r"(?P<role>[Гг]лавный врач|[Гг]лавврач|[Гг]енеральный директор|[Мм]едицинский директор|[Ии]сполнительный директор|"
    r"[Дд]иректор|[Уу]правляющий|[Рр]уководитель клиники|[Оо]снователь|[Вв]ладелец)"
    r"(?: клиники| центра| сети)?\s*[:—–-]?\s*(?P<name>" + _NAME + r"(?:\s+" + _NAME + r"){1,2})")
_STOP = {"Клиники", "Клиника", "Центра", "Центр", "Стоматологии", "Стоматология", "Москвы", "Москва", "Сети", "Сеть",
         "Ооо", "Отделения", "Филиала", "Врачи", "Врач", "Стоматолог", "Стоматологи", "Наши", "Наш", "Специалисты",
         "Приём", "Прием", "Запись", "Отзывы", "Услуги", "Цены", "Контакты", "Лицензии", "Кандидат", "Доктор",
         "Терапевт", "Хирург", "Ортодонт", "Ортопед", "Имплантолог", "Гигиенист", "Пародонтолог", "Детский", "Высшей",
         "Первой", "Категории", "Стаж", "Опыт", "Образование", "Химки", "Мытищи", "Одинцово", "Отделение", "Филиал",
         "Подробнее", "Записаться", "Читать", "Далее", "Все", "Ещё", "Еще", "Беговая", "Улица", "Проспект", "Метро"}
_ROLE_FIRST_BAD = ("Терапевт", "Хирург", "Ортодонт", "Ортопед", "Имплантолог")


def regex_people(text: str) -> list[dict]:
    """Люди по шаблону «Главный врач — Иванов Иван Иванович» без модели. Запасной путь при лимите LLM."""
    out, seen = [], set()
    for m in _ROLE_RE.finditer(text):
        words = m.group("name").split()
        if len(words) < 2 or any(w in _STOP for w in words):
            continue
        name = " ".join(words)
        if name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "role": m.group("role").lower()})
    return out[:8]


_OBLIQUE = ("у", "ю", "е", "ем", "ой", "ым", "ым", "ого", "его", "ому", "ему")


def nominative_like(name: str) -> bool:
    """Отсекаем ФИО в косвенном падеже («Эдуарду Самвеловиче», «Иванова Ивана»): фамилия/имя должны быть в именительном."""
    w = name.split()
    if len(w) < 2:
        return False
    first = w[0].lower()
    if first.endswith(("ова", "ева", "ина", "ая", "ская")):   # женские фамилии в именительном
        return True
    return not first.endswith(_OBLIQUE)


def parse_people(text: str) -> list[dict]:
    objs = re.findall(r"\[\s*\{.*?\}\s*\]", text or "", re.S) or re.findall(r"\[\s*\]", text or "")
    for cand in reversed(objs):
        try:
            d = json.loads(cand)
            if isinstance(d, list):
                return [x for x in d if isinstance(x, dict) and x.get("name") and nominative_like(str(x["name"]))
                        and str(x["name"]).split()[0] not in _STOP][:8]
        except json.JSONDecodeError:
            continue
    return []


# ---------- шаги ----------

def crawl(site: str) -> tuple[str, list[str]]:
    base = site if site.startswith("http") else "https://" + site
    seen, texts = [], []
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=20, verify=False) as c:
        try:
            r = c.get(base)
            base = str(r.url)
            home = r.text
        except Exception:
            return "", []
        seen.append(base)
        texts.append(strip_html(home))
        for u in pick_links(base, home):
            try:
                texts.append(strip_html(c.get(u).text))
                seen.append(u)
            except Exception:
                continue
    return "\n".join(texts), seen


def llm_people(snippets: list[str], clinic: str) -> list[dict]:
    if not OPENROUTER_KEY or not snippets or not llm_available():
        return []
    prompt = ("Найди в тексте руководителей стоматологии «" + clinic + "» (главный врач, директор, управляющий, "
              "основатель) и их ФИО.\n"
              "Пример. Текст: «Главный врач клиники — Сидоров Пётр Ильич. Директор: Кузнецова Мария.» "
              "Ответ: [{\"name\": \"Сидоров Пётр Ильич\", \"role\": \"главный врач\"}, {\"name\": \"Кузнецова Мария\", \"role\": \"директор\"}]\n"
              "Если руководителей нет — []. Теперь текст:\n" + "\n".join(snippets) + "\nОтвет (только JSON-массив):")
    try:
        r = httpx.post(LLM_BASE + "/chat/completions",
                       headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
                       json={"model": LLM_MODEL, "max_tokens": 700, "temperature": 0,
                             "messages": [{"role": "user", "content": prompt}]}, timeout=90)
        if r.status_code == 429 or (r.status_code == 200 and "error" in r.json() and r.json()["error"].get("code") == 429):
            note_429()
            print("   llm: 429, пауза 15 мин")
            return []
        return parse_people(r.raise_for_status().json()["choices"][0]["message"]["content"] or "")
    except Exception as e:
        print("   llm error:", repr(e)[:100])
        return []


def dadata(inn: str) -> dict:
    if not DADATA_TOKEN or not inn:
        return {}
    try:
        r = httpx.post("https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party",
                       headers={"Authorization": f"Token {DADATA_TOKEN}", "Content-Type": "application/json"},
                       json={"query": inn, "count": 1}, timeout=20)
        s = (r.raise_for_status().json().get("suggestions") or [{}])[0].get("data") or {}
    except Exception as e:
        print("   dadata error:", repr(e)[:100])
        return {}
    mgmt = s.get("management") or {}
    return {"legal_name": (s.get("name") or {}).get("full_with_opf") or "",
            "director": mgmt.get("name") or "", "director_post": mgmt.get("post") or "",
            "address": (s.get("address") or {}).get("value") or "", "status": (s.get("state") or {}).get("status") or "",
            "ogrn": s.get("ogrn") or ""}


_LEGAL_RE = re.compile(r"\b(ООО|АО|ЗАО|ПАО|ИП)\s*[«\"„]\s*([^»\"“”\n]{2,60}?)\s*[»\"“”]", re.I)


def find_legal_names(text: str) -> list[str]:
    """«ООО «Ника»» из футера/политики ПДн — юрлицо часто не совпадает с брендом."""
    out = []
    for opf, nm in _LEGAL_RE.findall(text or ""):
        full = f"{opf.upper()} {nm.strip()}"
        if full not in out and not re.search(r"район|город|москв", nm, re.I):
            out.append(full)
    return out[:5]


def dadata_by_legal(legal: str) -> str:
    """ИНН по точному юрлицу «ООО Ника»: берём действующую компанию с тем же названием."""
    if not DADATA_TOKEN or not legal:
        return ""
    want = re.sub(r"\W", "", legal.split(" ", 1)[1]).lower()
    try:
        r = httpx.post("https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party",
                       headers={"Authorization": f"Token {DADATA_TOKEN}", "Content-Type": "application/json"},
                       json={"query": legal, "count": 10, "locations": [{"kladr_id": "77"}, {"kladr_id": "50"}]}, timeout=20)
        for s in r.raise_for_status().json().get("suggestions") or []:
            d = s.get("data") or {}
            short = re.sub(r"\W", "", ((d.get("name") or {}).get("short_with_opf") or s.get("value") or "").split(" ", 1)[-1]).lower()
            if short == want and (d.get("state") or {}).get("status") == "ACTIVE":
                return d.get("inn", "")
    except Exception as e:
        print("   dadata legal error:", repr(e)[:100])
    return ""


def dadata_by_name(name: str, domain: str = "") -> str:
    """ИНН по названию, если на сайте его нет: DaData suggest/party, берём совпадение с названием или доменом."""
    if not DADATA_TOKEN or not name:
        return ""
    q = re.sub(r"[«»\"()]", " ", name)
    try:
        r = httpx.post("https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party",
                       headers={"Authorization": f"Token {DADATA_TOKEN}", "Content-Type": "application/json"},
                       json={"query": q + " стоматология", "count": 5, "locations": [{"kladr_id": "77"}, {"kladr_id": "50"}]},
                       timeout=20)
        sugg = r.raise_for_status().json().get("suggestions") or []
    except Exception as e:
        print("   dadata suggest error:", repr(e)[:100])
        return ""
    core = {w.lower() for w in re.findall(r"[\w-]{4,}", q) if w.lower() not in ("стоматология", "клиника", "центр", "семейной", "семейная")}
    root = domain.split(".")[0].lower() if domain else ""
    for s in sugg:
        d = s.get("data") or {}
        if (d.get("state") or {}).get("status") != "ACTIVE":
            continue
        full = (s.get("value") or "").lower()
        if any(w in full for w in core) or (root and root in full):
            return d.get("inn", "")
    return ""


def build(clinic: dict) -> dict:
    text, pages = crawl(clinic["site"])
    inn, ogrn = find_inn(text), find_ogrn(text)
    if not inn:
        for legal in find_legal_names(text):
            inn = dadata_by_legal(legal)
            if inn:
                break
    if not inn:
        inn = dadata_by_name(clinic["name"], clinic.get("site", ""))
    people = llm_people(role_snippets(text), clinic["name"])
    seen = {p["name"] for p in people}
    people += [p for p in regex_people(text) if p["name"] not in seen]   # регулярки дополняют модель
    chief = next((p["name"] for p in people if "врач" in (p.get("role") or "").lower()), "")
    dd = dadata(inn)
    director = dd.get("director") or next((p["name"] for p in people if "директор" in (p.get("role") or "").lower()
                                           or "руковод" in (p.get("role") or "").lower()), "")
    phones = re.findall(r"(?:\+7|8)[\s(]*\d{3}[\s)]*\d{3}[\s-]*\d{2}[\s-]*\d{2}", text)
    dd = {k: (v or "") for k, v in dd.items()}   # DaData может вернуть null — бэкенд ждёт строки
    return {"clinic_id": int(clinic["id"]), "inn": inn, "ogrn": dd.get("ogrn") or ogrn,
            "legal_name": dd.get("legal_name", ""), "director": director, "director_post": dd.get("director_post", ""),
            "chief_doctor": chief, "people": "; ".join(f'{p["name"]} — {p.get("role", "")}' for p in people),
            "emails": ", ".join(find_emails(text)), "phones": ", ".join(dict.fromkeys(p.strip() for p in phones[:4])),
            "address": dd.get("address", ""), "status": dd.get("status", ""), "sources": " ".join(pages)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="")
    ap.add_argument("--measured", action="store_true")
    ap.add_argument("--dry", action="store_true", help="не писать в бэкенд")
    args = ap.parse_args()
    clinics = [c for c in read_csv(CLINICS_CSV) if c["site"].strip()]
    if args.ids:
        want = {x.strip() for x in args.ids.split(",")}
        clinics = [c for c in clinics if c["id"] in want]
    elif args.measured:
        with httpx.Client(base_url=BACKEND, headers={"X-Token": MS_TOKEN}, timeout=30) as c:
            rows = c.get("report").raise_for_status().json()["rows"]
        measured = {str(r["id"]) for r in rows if r["submitted_at"]}
        clinics = [c for c in clinics if c["id"] in measured]
    print(f"клиник: {len(clinics)}; dadata: {'да' if DADATA_TOKEN else 'нет ключа'}", flush=True)
    for c in clinics:
        card = build(c)
        print(f'[{c["id"]}] {c["name"]}: ИНН {card["inn"] or "—"}; руководитель {card["director"] or "—"} '
              f'{card["director_post"]}; главврач {card["chief_doctor"] or "—"}; почты {card["emails"] or "—"}', flush=True)
        if not args.dry:
            httpx.post(BACKEND + "lpr", headers={"X-Token": MS_TOKEN}, json=card, timeout=30).raise_for_status()


if __name__ == "__main__":
    sys.exit(main())
