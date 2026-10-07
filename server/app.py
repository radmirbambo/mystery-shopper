"""
Mystery-shopper backend.

Хранит две вещи: когда мы оставили заявку в клинике и когда клиника перезвонила.
Матчит входящий звонок с клиникой по Caller ID (все номера из clinics.csv),
а если номер незнакомый — по названию, которое агент услышал в разговоре.

Запуск:
    pip install -r requirements.txt
    export MS_TOKEN=любая_длинная_строка      # тот же токен вписать в scenario.js и fill_forms.py
    uvicorn app:app --host 0.0.0.0 --port 8000

Эндпоинты:
    POST /form-submitted      {clinic_id, ts?}                          — заявка оставлена (зовёт formbot или ты руками)
    POST /call/start          {call_id, caller, ts?}                    — Voximplant при входящем; ответ: {matched, clinic_name}
    POST /call/end            {call_id, transcript, clinic_said, duration_sec} — Voximplant по окончании
    POST /manual              {clinic_id, kind: "call"|"whatsapp"|"sms", ts?} — если перезвонили/написали мимо агента
    GET  /report              — JSON: по каждой клинике время заявки, время перезвона, дельта в минутах
    GET  /report.xlsx         — то же в Excel
    GET  /table?token=…       — живая HTML-таблица для браузера
    GET  /pending             — кто ещё не перезвонил (и сколько минут прошло)
    GET  /match?q=название    — top-N похожих клиник для telegram-бота: [{id, name, score, pending}]
    POST /clinics             {name, site?, phone?} — новая клиника на лету (перезвонила та, кого нет в списке)
    GET  /callbacks?since=ISO — перезвоны для сопоставления с записями звонков; DELETE /callbacks/{id} — откат
    POST /transcript          {callback_id | clinic_id | ts, text, analysis?, audio} — транскрипт и разбор записи
    POST /lpr                 {clinic_id, ...} — карточка ЛПР (discover/lpr.py);  GET /lead/{clinic_id} — карточка лида
"""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from rapidfuzz import fuzz, process

MSK = timezone(timedelta(hours=3))
ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ["MS_DB_PATH"]) if os.environ.get("MS_DB_PATH") else ROOT / "shopper.db"
CLINICS_CSV = Path(os.environ["MS_CLINICS_CSV"]) if os.environ.get("MS_CLINICS_CSV") else ROOT.parent / "data" / "clinics.csv"
TOKEN = os.environ.get("MS_TOKEN", "change-me")

app = FastAPI(title="mystery-shopper")


# ---------- helpers ----------

def now_msk() -> datetime:
    return datetime.now(MSK)


def parse_ts(ts: Optional[str]) -> datetime:
    """ISO-строка или пусто → datetime в МСК."""
    if not ts:
        return now_msk()
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)
    return dt.astimezone(MSK)


def norm_phone(p: str) -> str:
    """'+7 (495) 123-45-67' → '4951234567' (последние 10 цифр)."""
    digits = re.sub(r"\D", "", p or "")
    return digits[-10:] if len(digits) >= 10 else digits


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS clinics (
                id INTEGER PRIMARY KEY, name TEXT, site TEXT, priority INTEGER, aliases TEXT
            );
            CREATE TABLE IF NOT EXISTS clinic_phones (
                clinic_id INTEGER, phone TEXT
            );
            CREATE TABLE IF NOT EXISTS submissions (
                clinic_id INTEGER PRIMARY KEY, submitted_at TEXT, source TEXT
            );
            CREATE TABLE IF NOT EXISTS callbacks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id TEXT UNIQUE,
                clinic_id INTEGER,          -- NULL если не смогли сопоставить
                caller TEXT,
                started_at TEXT,
                kind TEXT,                  -- call | whatsapp | sms
                matched_by TEXT,            -- callerid | name | manual | none
                transcript TEXT,
                clinic_said TEXT,
                duration_sec INTEGER
            );
            CREATE TABLE IF NOT EXISTS analyses (
                callback_id INTEGER PRIMARY KEY,
                audio TEXT, admin_name TEXT, offered TEXT, booked TEXT, tone TEXT,
                score REAL, quotes TEXT, summary TEXT, raw TEXT, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS lpr (
                clinic_id INTEGER PRIMARY KEY,
                inn TEXT, ogrn TEXT, legal_name TEXT, director TEXT, director_post TEXT,
                chief_doctor TEXT, people TEXT, emails TEXT, phones TEXT, address TEXT,
                status TEXT, sources TEXT, updated_at TEXT
            );
            """
        )
        # (пере)загружаем клиники из CSV — CSV главный
        con.execute("DELETE FROM clinics")
        con.execute("DELETE FROM clinic_phones")
        with open(CLINICS_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                cid = int(row["id"])
                con.execute(
                    "INSERT INTO clinics VALUES (?,?,?,?,?)",
                    (cid, row["name"], row["site"], int(row["priority"] or 3), row.get("aliases", "")),
                )
                phones = [row["phone"]] + row["phones_extra"].split(";")
                for p in phones:
                    n = norm_phone(p)
                    if n:
                        con.execute("INSERT INTO clinic_phones VALUES (?,?)", (cid, n))


init_db()


def check(token: Optional[str]) -> None:
    if token != TOKEN:
        raise HTTPException(401, "bad token")


def match_by_phone(caller: str) -> Optional[int]:
    n = norm_phone(caller)
    if not n:
        return None
    with db() as con:
        row = con.execute(
            "SELECT clinic_id FROM clinic_phones WHERE phone=?", (n,)
        ).fetchone()
        if row:
            return row["clinic_id"]
        # некоторые клиники звонят с соседнего номера того же пула — совпадение первых 7 из 10
        row = con.execute(
            "SELECT clinic_id FROM clinic_phones WHERE substr(phone,1,7)=?", (n[:7],)
        ).fetchone()
        return row["clinic_id"] if row else None


def match_by_name(said: str) -> tuple[Optional[int], int]:
    """Нечёткое сравнение услышанного названия с названиями клиник. Возвращает (id, score)."""
    if not said:
        return None, 0
    # сравниваем и с названием, и с кириллическими алиасами (ASR отдаёт кириллицу, имена часто латиницей)
    with db() as con:
        variants: dict[str, int] = {}
        for r in con.execute("SELECT id, name, aliases FROM clinics"):
            variants[r["name"].lower()] = r["id"]
            for a in (r["aliases"] or "").split(";"):
                if a.strip():
                    variants[a.strip().lower()] = r["id"]
    # ждём только те, кому мы оставляли заявку, но кто ещё не перезвонил — сужаем пространство
    with db() as con:
        pending = {
            r["clinic_id"]
            for r in con.execute(
                "SELECT s.clinic_id FROM submissions s "
                "LEFT JOIN callbacks c ON c.clinic_id=s.clinic_id WHERE c.id IS NULL"
            )
        }
    pool = {k: v for k, v in variants.items() if v in pending} or variants
    best = process.extractOne(said.lower(), list(pool.keys()), scorer=fuzz.token_set_ratio)
    if not best:
        return None, 0
    variant, score, _ = best
    cid = pool[variant]
    return (cid, int(score)) if score >= 60 else (None, int(score))


def clinic_name(cid: Optional[int]) -> Optional[str]:
    if cid is None:
        return None
    with db() as con:
        r = con.execute("SELECT name FROM clinics WHERE id=?", (cid,)).fetchone()
        return r["name"] if r else None


# ---------- schemas ----------

class FormSubmitted(BaseModel):
    clinic_id: int
    ts: Optional[str] = None
    source: str = "formbot"


class CallStart(BaseModel):
    call_id: str
    caller: str
    ts: Optional[str] = None


class CallEnd(BaseModel):
    call_id: str
    transcript: str = ""
    clinic_said: str = ""
    duration_sec: int = 0


class Manual(BaseModel):
    clinic_id: Optional[int] = None   # None — звонок был, клиника не определена (clinic_said — как представились)
    kind: str = "call"
    ts: Optional[str] = None
    note: str = ""
    clinic_said: str = ""


# ---------- endpoints ----------

@app.post("/form-submitted")
def form_submitted(body: FormSubmitted, x_token: Optional[str] = Header(None)):
    check(x_token)
    ts = parse_ts(body.ts).isoformat()
    with db() as con:
        exists = con.execute(
            "SELECT 1 FROM submissions WHERE clinic_id=?", (body.clinic_id,)
        ).fetchone()
        if exists:
            raise HTTPException(409, "already submitted — одна заявка на клинику")
        con.execute(
            "INSERT INTO submissions VALUES (?,?,?)", (body.clinic_id, ts, body.source)
        )
    return {"ok": True, "clinic": clinic_name(body.clinic_id), "submitted_at": ts}


@app.get("/submitted/{clinic_id}")
def is_submitted(clinic_id: int, x_token: Optional[str] = Header(None)):
    check(x_token)
    with db() as con:
        r = con.execute("SELECT submitted_at FROM submissions WHERE clinic_id=?", (clinic_id,)).fetchone()
    return {"submitted": bool(r), "submitted_at": r["submitted_at"] if r else None}


@app.post("/call/start")
def call_start(body: CallStart, x_token: Optional[str] = Header(None)):
    check(x_token)
    ts = parse_ts(body.ts).isoformat()
    cid = match_by_phone(body.caller)
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO callbacks (call_id, clinic_id, caller, started_at, kind, matched_by) "
            "VALUES (?,?,?,?,?,?)",
            (body.call_id, cid, body.caller, ts, "call", "callerid" if cid else "none"),
        )
    return {"matched": cid is not None, "clinic_id": cid, "clinic_name": clinic_name(cid)}


@app.post("/call/end")
def call_end(body: CallEnd, x_token: Optional[str] = Header(None)):
    check(x_token)
    with db() as con:
        row = con.execute("SELECT * FROM callbacks WHERE call_id=?", (body.call_id,)).fetchone()
        if not row:
            raise HTTPException(404, "unknown call_id")
        cid, matched_by = row["clinic_id"], row["matched_by"]
        if cid is None and body.clinic_said:
            cid, score = match_by_name(body.clinic_said)
            matched_by = f"name:{score}" if cid else "none"
        con.execute(
            "UPDATE callbacks SET clinic_id=?, matched_by=?, transcript=?, clinic_said=?, duration_sec=? "
            "WHERE call_id=?",
            (cid, matched_by, body.transcript, body.clinic_said, body.duration_sec, body.call_id),
        )
    return {"clinic_id": cid, "clinic_name": clinic_name(cid), "matched_by": matched_by}


@app.post("/manual")
def manual(body: Manual, x_token: Optional[str] = Header(None)):
    check(x_token)
    t0 = parse_ts(body.ts)
    ts = t0.isoformat()
    with db() as con:
        if body.clinic_id is not None:
            # «Название 14:50» после «?» — привязываем уже записанный неопознанный звонок (±3 мин), а не плодим второй
            orphan = con.execute(
                "SELECT id FROM callbacks WHERE clinic_id IS NULL AND started_at BETWEEN ? AND ? ORDER BY started_at LIMIT 1",
                ((t0 - timedelta(minutes=3)).isoformat(), (t0 + timedelta(minutes=3)).isoformat())).fetchone()
            if orphan:
                con.execute("UPDATE callbacks SET clinic_id=?, kind=?, matched_by='manual' WHERE id=?",
                            (body.clinic_id, body.kind, orphan["id"]))
                return {"ok": True, "attached": orphan["id"]}
        con.execute(
            "INSERT INTO callbacks (call_id, clinic_id, caller, started_at, kind, matched_by, transcript, clinic_said) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"manual-{body.clinic_id}-{ts}", body.clinic_id, "", ts, body.kind, "manual" if body.clinic_id else "none",
             body.note, body.clinic_said),
        )
    return {"ok": True}


@app.get("/match")
def match(q: str = "", n: int = 3, phone: str = "", x_token: Optional[str] = Header(None)):
    """Кандидаты по нечёткому названию (или по номеру, с которого звонили). Сначала те, кому оставили заявку."""
    check(x_token)
    if phone:
        cid = match_by_phone(phone)
        if cid:
            return [{"id": cid, "name": clinic_name(cid), "score": 100, "pending": True, "by": "phone"}]
    q = (q or "").strip().lower()
    if not q:
        return []
    with db() as con:
        variants: dict[str, int] = {}
        for r in con.execute("SELECT id, name, aliases FROM clinics"):
            variants[r["name"].lower()] = r["id"]
            for a in (r["aliases"] or "").split(";"):
                if a.strip():
                    variants[a.strip().lower()] = r["id"]
        pending = {
            r["clinic_id"]
            for r in con.execute(
                "SELECT s.clinic_id FROM submissions s "
                "LEFT JOIN callbacks c ON c.clinic_id=s.clinic_id WHERE c.id IS NULL"
            )
        }
    # грубый стемминг: «лазерной» / «лазерная» / «легенды» / «легенда» — первые 5 букв каждого слова;
    # родовые слова («стоматология», «клиника», «центр») не участвуют — иначе «Стоматология №1» совпадает со всеми
    generic = {"стома", "клини", "центр", "denta", "clini", "стом", "медиц", "семей", "город", "москв", "dent"}

    def stem(s: str) -> str:
        toks = [w[:5] for w in re.findall(r"\w+", s.lower())]
        core = [w for w in toks if w not in generic]
        return " ".join(core or toks)

    # латинские названия (Valident, Dantistoff) сравниваем и в кириллической транслитерации: «валидент» → Valident
    lat = {"shch": "щ", "yo": "ё", "zh": "ж", "kh": "х", "ts": "ц", "ch": "ч", "sh": "ш", "yu": "ю", "ya": "я", "ph": "ф",
           "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "е", "z": "з", "i": "и", "j": "й", "k": "к", "l": "л",
           "m": "м", "n": "н", "o": "о", "p": "п", "r": "р", "s": "с", "t": "т", "u": "у", "f": "ф", "h": "х", "c": "к",
           "y": "и", "x": "кс", "w": "в", "q": "к"}

    def translit(s: str) -> str:
        out, i, s = [], 0, s.lower()
        while i < len(s):
            for ln in (4, 2, 1):
                chunk = s[i:i + ln]
                if chunk in lat:
                    out.append(lat[chunk]); i += ln; break
            else:
                out.append(s[i]); i += 1
        return "".join(out)

    for v in list(variants):
        if re.search(r"[a-z]", v):
            variants.setdefault(translit(v), variants[v])
    stemmed: dict[str, str] = {}
    for v in variants:
        stemmed.setdefault(stem(v), v)
    best: dict[int, int] = {}
    for sv, score, _ in process.extract(stem(q), list(stemmed.keys()), scorer=fuzz.token_set_ratio, limit=None):
        cid = variants[stemmed[sv]]
        if int(score) > best.get(cid, 0):
            best[cid] = int(score)
    # кому оставили заявку и кто ещё не перезвонил — выше при равных баллах: звонят почти всегда они
    top = sorted(best.items(), key=lambda kv: (-(kv[1] + (8 if kv[0] in pending else 0)), kv[0]))[:n]
    return [{"id": cid, "name": clinic_name(cid), "score": min(s, 100), "pending": cid in pending}
            for cid, s in top]


class NewClinic(BaseModel):
    name: str
    site: str = ""
    phone: str = ""


def _aliases(name: str) -> str:
    generic = {"стоматология", "стоматологическая", "стоматологический", "клиника", "центр", "медицинский",
               "семейная", "семейный", "ооо", "зао", "на", "и", "dental", "clinic"}
    words = [w.strip(" ,.-«»\"'()") for w in name.lower().split()]
    core = [w for w in words if w and w not in generic]
    return " ".join(core) if core and core != words else ""


@app.post("/clinics")
def add_clinic(body: NewClinic, x_token: Optional[str] = Header(None)):
    """Добавляет клинику в clinics.csv и в БД без перезапуска. Возвращает {id, name}."""
    check(x_token)
    name = " ".join(body.name.split()).strip()
    if not name:
        raise HTTPException(400, "empty name")
    with db() as con:
        # lower() в SQLite не знает кириллицу — сравниваем в Python
        dup = next((r for r in con.execute("SELECT id, name FROM clinics") if r["name"].lower() == name.lower()), None)
        if dup:
            return {"id": dup["id"], "name": dup["name"], "existed": True}
        cid = (con.execute("SELECT MAX(id) AS m FROM clinics").fetchone()["m"] or 0) + 1
        con.execute("INSERT INTO clinics VALUES (?,?,?,?,?)", (cid, name, body.site, 2, _aliases(name)))
        n = norm_phone(body.phone)
        if n:
            con.execute("INSERT INTO clinic_phones VALUES (?,?)", (cid, n))
    with open(CLINICS_CSV, "a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow([cid, name, body.site, body.phone, "", 2, _aliases(name)])
    return {"id": cid, "name": name, "existed": False}


class Transcript(BaseModel):
    callback_id: Optional[int] = None
    clinic_id: Optional[int] = None
    ts: Optional[str] = None          # время звонка, если callback_id неизвестен: ищем ближайший перезвон ±20 мин
    text: str = ""
    audio: str = ""
    analysis: Optional[dict] = None


class Lpr(BaseModel):
    clinic_id: int
    inn: str = ""
    ogrn: str = ""
    legal_name: str = ""
    director: str = ""
    director_post: str = ""
    chief_doctor: str = ""
    people: str = ""
    emails: str = ""
    phones: str = ""
    address: str = ""
    status: str = ""
    sources: str = ""


@app.get("/callbacks")
def callbacks(since: Optional[str] = None, x_token: Optional[str] = Header(None)):
    check(x_token)
    since_iso = parse_ts(since).isoformat() if since else (now_msk() - timedelta(days=3)).isoformat()
    with db() as con:
        rows = con.execute(
            "SELECT cb.id, cb.clinic_id, c.name AS clinic, cb.started_at, cb.kind, "
            "(SELECT 1 FROM analyses a WHERE a.callback_id=cb.id) AS analyzed "
            "FROM callbacks cb LEFT JOIN clinics c ON c.id=cb.clinic_id WHERE cb.started_at>=? ORDER BY cb.started_at",
            (since_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.delete("/callbacks/{cb_id}")
def delete_callback(cb_id: int, x_token: Optional[str] = Header(None)):
    """Откат ошибочно записанного перезвона (Луиза: «отмени»)."""
    check(x_token)
    with db() as con:
        row = con.execute("SELECT cb.id, c.name FROM callbacks cb LEFT JOIN clinics c ON c.id=cb.clinic_id WHERE cb.id=?",
                          (cb_id,)).fetchone()
        if not row:
            raise HTTPException(404, "unknown callback")
        con.execute("DELETE FROM analyses WHERE callback_id=?", (cb_id,))
        con.execute("DELETE FROM callbacks WHERE id=?", (cb_id,))
    return {"deleted": cb_id, "clinic_name": row["name"]}


def _nearest_callback(con, clinic_id: Optional[int], ts: Optional[str]):
    """Перезвон, к которому относится запись: по клинике (последний) или по времени (ближайший ±20 мин)."""
    if clinic_id is not None:
        r = con.execute("SELECT id, started_at FROM callbacks WHERE clinic_id=? ORDER BY started_at DESC LIMIT 1",
                        (clinic_id,)).fetchone()
        if r:
            return r["id"]
    if ts:
        t0 = parse_ts(ts)
        best, best_d = None, 20 * 60
        for r in con.execute("SELECT id, started_at FROM callbacks WHERE started_at BETWEEN ? AND ?",
                             ((t0 - timedelta(minutes=20)).isoformat(), (t0 + timedelta(minutes=20)).isoformat())):
            d = abs((datetime.fromisoformat(r["started_at"]) - t0).total_seconds())
            if d < best_d:
                best, best_d = r["id"], d
        return best
    return None


@app.post("/transcript")
def transcript(body: Transcript, x_token: Optional[str] = Header(None)):
    check(x_token)
    with db() as con:
        cb_id = body.callback_id or _nearest_callback(con, body.clinic_id, body.ts)
        if cb_id is None:
            raise HTTPException(404, "no matching callback")
        con.execute("UPDATE callbacks SET transcript=? WHERE id=?", (body.text, cb_id))
        a = body.analysis or {}
        con.execute(
            "INSERT OR REPLACE INTO analyses (callback_id, audio, admin_name, offered, booked, tone, score, quotes, summary, raw, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (cb_id, body.audio, a.get("admin_name"), a.get("offered"), a.get("booked"), a.get("tone"),
             a.get("score"), "\n".join(a.get("quotes") or []), a.get("summary"), json.dumps(a, ensure_ascii=False),
             now_msk().isoformat()),
        )
        row = con.execute("SELECT cb.clinic_id, c.name FROM callbacks cb LEFT JOIN clinics c ON c.id=cb.clinic_id WHERE cb.id=?",
                          (cb_id,)).fetchone()
    return {"callback_id": cb_id, "clinic_id": row["clinic_id"], "clinic_name": row["name"]}


@app.post("/lpr")
def set_lpr(body: Lpr, x_token: Optional[str] = Header(None)):
    check(x_token)
    d = body.model_dump()
    with db() as con:
        old = con.execute("SELECT * FROM lpr WHERE clinic_id=?", (d["clinic_id"],)).fetchone()
        if old:
            # повторный сбор не затирает найденное ранее пустотой (сайт мог не ответить)
            for k, v in d.items():
                if not v and old[k]:
                    d[k] = old[k]
        d["updated_at"] = now_msk().isoformat()
        cols = ",".join(d.keys())
        con.execute(f"INSERT OR REPLACE INTO lpr ({cols}) VALUES ({','.join('?' for _ in d)})", tuple(d.values()))
    return {"ok": True}


@app.get("/lead/{clinic_id}")
def lead(clinic_id: int, x_token: Optional[str] = Header(None)):
    """Карточка лида: замер + ЛПР + разбор разговора."""
    check(x_token)
    row = next((r for r in _report_rows() if r["id"] == clinic_id), None)
    if not row:
        raise HTTPException(404, "unknown clinic")
    with db() as con:
        c = con.execute("SELECT site, aliases FROM clinics WHERE id=?", (clinic_id,)).fetchone()
        l = con.execute("SELECT * FROM lpr WHERE clinic_id=?", (clinic_id,)).fetchone()
        a = con.execute(
            "SELECT a.* FROM analyses a JOIN callbacks cb ON cb.id=a.callback_id WHERE cb.clinic_id=? ORDER BY cb.started_at LIMIT 1",
            (clinic_id,)).fetchone()
    return {"measure": row, "site": c["site"] if c else "", "lpr": dict(l) if l else None, "analysis": dict(a) if a else None}


def _report_rows() -> list[dict]:
    with db() as con:
        rows = con.execute(
            """
            SELECT c.id, c.name, c.priority, s.submitted_at,
                   (SELECT MIN(started_at) FROM callbacks cb WHERE cb.clinic_id=c.id
                      AND (s.submitted_at IS NULL OR cb.started_at >= s.submitted_at)) AS first_callback,
                   (SELECT kind FROM callbacks cb WHERE cb.clinic_id=c.id ORDER BY started_at LIMIT 1) AS kind,
                   (SELECT matched_by FROM callbacks cb WHERE cb.clinic_id=c.id ORDER BY started_at LIMIT 1) AS matched_by,
                   (SELECT transcript FROM callbacks cb WHERE cb.clinic_id=c.id ORDER BY started_at LIMIT 1) AS transcript,
                   (SELECT a.score FROM analyses a JOIN callbacks cb ON cb.id=a.callback_id WHERE cb.clinic_id=c.id ORDER BY cb.started_at LIMIT 1) AS score,
                   (SELECT a.admin_name FROM analyses a JOIN callbacks cb ON cb.id=a.callback_id WHERE cb.clinic_id=c.id ORDER BY cb.started_at LIMIT 1) AS admin_name,
                   (SELECT a.summary FROM analyses a JOIN callbacks cb ON cb.id=a.callback_id WHERE cb.clinic_id=c.id ORDER BY cb.started_at LIMIT 1) AS summary,
                   (SELECT l.director FROM lpr l WHERE l.clinic_id=c.id) AS director,
                   (SELECT l.emails FROM lpr l WHERE l.clinic_id=c.id) AS emails
            FROM clinics c LEFT JOIN submissions s ON s.clinic_id=c.id
            ORDER BY c.id
            """
        ).fetchall()
    out = []
    for r in rows:
        sub = datetime.fromisoformat(r["submitted_at"]) if r["submitted_at"] else None
        cb = datetime.fromisoformat(r["first_callback"]) if r["first_callback"] else None
        delta = round((cb - sub).total_seconds() / 60, 1) if sub and cb else None
        waiting = round((now_msk() - sub).total_seconds() / 60, 1) if sub and not cb else None
        out.append(
            {
                "id": r["id"], "clinic": r["name"], "priority": r["priority"],
                "submitted_at": r["submitted_at"], "first_callback": r["first_callback"],
                "callback_minutes": delta, "waiting_minutes": waiting,
                "kind": r["kind"], "matched_by": r["matched_by"], "transcript": r["transcript"],
                "score": r["score"], "admin_name": r["admin_name"], "summary": r["summary"],
                "director": r["director"], "emails": r["emails"],
            }
        )
    return out


@app.get("/report")
def report(x_token: Optional[str] = Header(None)):
    check(x_token)
    rows = _report_rows()
    submitted = [r for r in rows if r["submitted_at"]]
    called = [r for r in submitted if r["first_callback"]]
    fast = [r for r in called if r["callback_minutes"] is not None and r["callback_minutes"] <= 5]
    with db() as con:
        unknown = [dict(r) for r in con.execute(
            "SELECT id, started_at, kind, clinic_said FROM callbacks WHERE clinic_id IS NULL ORDER BY started_at")]
    return {
        "unmatched": unknown,
        "summary": {
            "submitted": len(submitted),
            "called_back": len(called),
            "not_called_back": len(submitted) - len(called),
            "within_5_min": len(fast),
            "median_minutes": sorted(r["callback_minutes"] for r in called)[len(called) // 2] if called else None,
        },
        "rows": rows,
    }


@app.get("/table")
def table(token: Optional[str] = None):
    """Живая таблица в браузере: http://127.0.0.1:8000/table?token=MS_TOKEN (обновляется раз в минуту)."""
    from fastapi.responses import HTMLResponse
    check(token)
    rows = [r for r in _report_rows() if r["submitted_at"] or r["first_callback"]]
    rep = report(token)["summary"]

    def esc(v):
        return "" if v is None else str(v).replace("&", "&amp;").replace("<", "&lt;")

    def tm(iso):
        return datetime.fromisoformat(iso).strftime("%d.%m %H:%M") if iso else ""

    trs = []
    for r in rows:
        d = r["callback_minutes"]
        cls = "" if d is None else ("fast" if d <= 5 else "ok" if d <= 30 else "slow")
        delta = "" if d is None else f"{d:.0f} мин"
        wait = "" if r["waiting_minutes"] is None else f"ждём {r['waiting_minutes']:.0f} мин"
        trs.append(f"<tr class='{cls}'><td>{r['id']}</td><td>{esc(r['clinic'])}</td><td>{tm(r['submitted_at'])}</td>"
                   f"<td>{tm(r['first_callback'])}</td><td><b>{delta}</b>{wait}</td><td>{esc(r['kind'])}</td>"
                   f"<td>{esc(r['score'])}</td><td>{esc(r['admin_name'])}</td><td>{esc(r['director'])}</td>"
                   f"<td class='s'>{esc(r['summary'])}</td></tr>")
    html = f"""<!doctype html><html lang=ru><head><meta charset=utf-8><meta http-equiv=refresh content=60>
<title>Тайный покупатель — замеры</title><style>
body{{font:14px -apple-system,system-ui,sans-serif;margin:24px;color:#222}} table{{border-collapse:collapse;width:100%}}
th,td{{border-bottom:1px solid #e5e5e5;padding:6px 8px;text-align:left;vertical-align:top}} th{{background:#f5f5f5;position:sticky;top:0}}
tr.fast td:nth-child(5){{color:#0a7}} tr.ok td:nth-child(5){{color:#b80}} tr.slow td:nth-child(5){{color:#c22}}
td.s{{max-width:360px;color:#555}} .sum{{margin:0 0 16px;color:#555}}
</style></head><body>
<h2>Тайный покупатель — замеры перезвона</h2>
<p class=sum>Заявок: {rep['submitted']} · перезвонили: {rep['called_back']} · не перезвонили: {rep['not_called_back']}
 · в 5 минут: {rep['within_5_min']} · медиана: {rep['median_minutes'] or '—'} мин · обновлено {now_msk().strftime('%H:%M')}</p>
<table><thead><tr><th>№</th><th>Клиника</th><th>Заявка</th><th>Перезвон</th><th>Дельта</th><th>Канал</th>
<th>Оценка</th><th>Администратор</th><th>Руководитель</th><th>Разбор</th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan=10>Пока пусто — первая волна ещё не прошла.</td></tr>'}</tbody></table>
</body></html>"""
    return HTMLResponse(html)


@app.get("/pending")
def pending(x_token: Optional[str] = Header(None)):
    check(x_token)
    return [r for r in _report_rows() if r["submitted_at"] and not r["first_callback"]]


@app.get("/report.xlsx")
def report_xlsx(x_token: Optional[str] = Header(None)):
    check(x_token)
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Перезвон"
    head = ["№", "Клиника", "Приоритет", "Заявка оставлена", "Первый перезвон",
            "Перезвонили через, мин", "Ждём, мин", "Канал", "Как сопоставили", "Транскрипт",
            "Оценка разговора", "Администратор", "Разбор", "Руководитель", "Почты"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(name="Arial", bold=True)
    for r in _report_rows():
        ws.append([r["id"], r["clinic"], r["priority"], r["submitted_at"], r["first_callback"],
                   r["callback_minutes"], r["waiting_minutes"], r["kind"], r["matched_by"], r["transcript"],
                   r["score"], r["admin_name"], r["summary"], r["director"], r["emails"]])
    for col, w in zip("ABCDEFGHIJKLMNO", [5, 40, 9, 22, 22, 12, 10, 10, 14, 60, 10, 16, 50, 28, 30]):
        ws.column_dimensions[col].width = w
    path = ROOT / "report.xlsx"
    wb.save(path)
    return FileResponse(path, filename="report.xlsx")
