"""
Telegram-бот для ручной фиксации перезвонов.

Ты отвечаешь на звонок клиники сам, потом пишешь боту название — он находит клинику,
записывает перезвон в бэкенд (POST /manual) и отвечает дельтой от заявки.

Сообщения:
    Легенда              — перезвонили сейчас (время = время сообщения)
    Легенда 14:35        — перезвонили в 14:35 (можно «14 35», «в 14.35»)
    Легенда wa           — написали в WhatsApp (ещё: вотсап, ватсап; смс/sms)
    2                    — выбор из предложенных кандидатов
    да                   — записать как новую клинику, если в списке не нашлось
    ?                    — звонок был, клинику не понял; время запишется в logs/unknown.log
    готово 1 3           — заявки из ручной пачки (/next) оставлены: фиксирую время
    /pending             — кто ещё не перезвонил
    /digest              — сводка как утром: кто не перезвонил за сутки, медленные, быстрые
    /report              — Excel-отчёт файлом
    /next                — пачка клиник, где бот не справился (капча/нет формы): ссылки, оставь заявку руками
    /wave                — запустить волну заявок прямо сейчас (обычно идёт сама по будням в 12:00)
    /pause  /resume      — остановить / возобновить ежедневные волны
    волну на 13:00       — перенести сегодняшнюю волну; «отмени» — снять вопрос или откатить последнюю запись
    /lead Легенда        — карточка лида: замер, ЛПР, разбор разговора, черновик письма
    (аудио/голосовое)    — запись звонка: транскрипт и разбор, привяжу к последнему перезвону
    /start               — первый написавший становится владельцем бота, остальным отказ

Длинные фразы («сейчас звонили из лазерной, назвались номер 1») разбирает LLM через OpenRouter,
если задан OPENROUTER_API_KEY; иначе — простой разбор.

Env: MS_TG_TOKEN, MS_BACKEND (http://127.0.0.1:8000/), MS_TOKEN, OPENROUTER_API_KEY?, MS_LLM_MODEL?
Владелец хранится в telegram/state.json. Без внешних библиотек кроме httpx: Bot API через long polling.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

MSK = timezone(timedelta(hours=3))
ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
UNKNOWN_LOG = ROOT.parent / "logs" / "unknown.log"
PAUSE_FLAG = ROOT.parent / "data" / "PAUSE"
DAILY_SH = ROOT.parent / "daily.sh"
WAVE_AT = ROOT.parent / "data" / "WAVE_AT"
sys.path.insert(0, str(ROOT.parent / "discover"))
sys.path.insert(0, str(ROOT.parent / "calls"))
sys.path.insert(0, str(ROOT.parent / "data"))
from llm_guard import llm_available, note_429  # noqa: E402
CALLS_PY = ROOT.parent / "calls" / ".venv" / "bin" / "python"
PIPELINE = ROOT.parent / "calls" / "pipeline.py"
INBOX = ROOT.parent / "data" / "recordings" / "inbox"

TG_TOKEN = os.environ.get("MS_TG_TOKEN", "")
BACKEND = os.environ.get("MS_BACKEND", "http://127.0.0.1:8000/").rstrip("/") + "/"
MS_TOKEN = os.environ.get("MS_TOKEN", "change-me")
OPENROUTER_KEY = os.environ.get("MS_LLM_KEY") or os.environ.get("OPENROUTER_API_KEY", "") or "local"
LLM_MODEL = os.environ.get("MS_LLM_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
LLM_BASE = os.environ.get("MS_LLM_BASE", "https://openrouter.ai/api/v1").rstrip("/")

AUTO_SCORE = 85      # выше — берём без вопросов
AUTO_GAP = 10        # и второй кандидат отстаёт хотя бы на столько
LLM_MIN_WORDS = 4    # фразы короче разбираем без модели

log = logging.getLogger("tgbot")


# ---------- чистые функции (покрыты тестами) ----------

@dataclass
class Parsed:
    query: str = ""
    raw: str = ""                      # название как написал пользователь (без времени/канала) — для новой клиники
    kind: str = "call"
    ts: Optional[datetime] = None
    has_time: bool = False
    choice: Optional[int] = None
    done: Optional[list[int]] = None   # «готово 1 3» → [1, 3]; «готово» → []
    confirm: bool = False
    negative: bool = False             # «нет», «отмени», «отбой» — снять вопрос / откатить последнюю запись
    suspicious: bool = False           # есть слова-просьбы: если клиника не найдена уверенно — не предлагать «новую»
    phone: str = ""                    # номер, с которого звонили, если есть в сообщении
    wave_at: Optional[str] = None      # «волну на 13:00» → "13:00"
    unknown: bool = False
    command: Optional[str] = None


_TIME_RE = re.compile(r"(?<![\d:])(?:в\s+)?([01]?\d|2[0-3])[:.\-\s]([0-5]\d)(?![\d:])")
_KINDS = {
    "whatsapp": {"wa", "вотсап", "ватсап", "whatsapp", "вацап"},
    "sms": {"смс", "sms"},
}
_FILLER = {"сейчас", "только", "что", "позвонили", "позвонила", "позвонил", "звонили", "звонила", "звонил",
           "звонок", "перезвонили", "перезвонила", "перезвонил", "из", "в", "была", "был", "были", "назвались",
           "назвалась", "это", "они", "мне", "от", "с", "клиника", "клиники", "стоматология", "стоматологии",
           "звонят", "звонит", "написали", "написала", "написал"}
_YES = {"да", "yes", "ага", "угу", "+", "новая", "новую", "добавь", "добавить", "записать", "запиши", "ок", "ok"}
_NO = {"нет", "не", "неа", "не надо", "не нужно", "отмена", "отмени", "отменить", "отбой", "-", "undo", "назад", "удали", "убери"}
_NOT_A_CLINIC = {"давай", "перенес", "перенеси", "перенесем", "перенесём", "запусти", "можно", "нужно", "надо", "сделай",
                 "покажи", "скажи", "почему", "когда", "как", "что", "волна", "волну", "волны", "заявки", "заявку", "отчет",
                 "отчёт", "пауза", "стоп"}


def parse_message(text: str, message_time: datetime) -> Parsed:
    """Разбирает текст сообщения. message_time — aware datetime (Telegram даёт UTC)."""
    base = message_time.astimezone(MSK)
    p = Parsed(ts=base)
    text = (text or "").strip()
    if text.startswith("/"):
        p.command = text[1:].split()[0].split("@")[0].lower()
        return p
    if text.lower() in ("next", "дальше", "следующие", "пачка"):
        p.command = "next"
        return p
    if text == "?":
        p.unknown = True
        return p
    if re.fullmatch(r"\d", text):
        p.choice = int(text)
        return p
    low = text.lower().strip("!. ")
    if low in _YES:
        p.confirm = True
        return p
    if low in _NO or low.startswith(("отмени", "отмена", "удали посл", "убери посл")):
        p.negative = True
        return p
    if "волн" in low:
        m = _TIME_RE.search(low)
        p.wave_at = f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""
        return p
    m = re.fullmatch(r"(?:готово|сделал|сделано|отправил|оставил)(?:[\s,:]+(.*))?", text.lower())
    if m:
        p.done = [int(x) for x in re.findall(r"\d+", m.group(1) or "")]
        return p
    # телефон в сообщении («+7 495 183 84 25 новадент»): вынимаем до разбора времени, чтобы цифры не стали временем
    pm = re.search(r"(?:\+7|8|7)?[\s(]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}", text)
    if pm and len(re.sub(r"\D", "", pm.group(0))) >= 10:
        p.phone = re.sub(r"\D", "", pm.group(0))[-10:]
        text = text[: pm.start()] + " " + text[pm.end():]
    m = _TIME_RE.search(text)
    if m:
        p.ts = base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        p.has_time = True
        text = text[: m.start()] + " " + text[m.end():]
    words, raw = [], []
    for w in text.split():
        lw = w.lower().strip(",.!«»\"'")
        kind = next((k for k, ws in _KINDS.items() if lw in ws), None)
        if kind:
            p.kind = kind
            continue
        if lw:
            raw.append(w.strip(",.!«»\"'"))
        if lw and lw not in _FILLER:
            words.append(lw)
    p.query = " ".join(w for w in words if w not in _NOT_A_CLINIC and w not in ("что-то", "около", "примерно", "вроде", "кажется")).strip()
    p.raw = " ".join(raw).strip()
    p.suspicious = any(w in _NOT_A_CLINIC for w in words)
    return p


def decide(cands: list[dict]) -> Optional[int]:
    """id клиники, если уверены; иначе None — надо спросить."""
    if not cands:
        return None
    top = cands[0]
    if top["score"] < AUTO_SCORE:
        return None
    if len(cands) > 1 and top["score"] - cands[1]["score"] < AUTO_GAP:
        return None
    return top["id"]


def pretty_name(q: str) -> str:
    """«лазерная стоматология номер 1» → «Лазерная стоматология номер 1»."""
    q = " ".join(q.split())
    return q[:1].upper() + q[1:] if q else q


# ---------- LLM-разбор длинных фраз ----------

_LLM_PROMPT = (
    "Ты помощник, который из сообщения пользователя извлекает данные о звонке стоматологической клиники. "
    "Верни ТОЛЬКО JSON без пояснений: {\"clinic\": название клиники как его назвал пользователь или null, "
    "\"time\": \"HH:MM\" если названо время, иначе null, \"kind\": \"call\"|\"whatsapp\"|\"sms\"}. "
    "Слова «сейчас», «только что» означают time=null. Сообщение: "
)


def llm_extract(text: str) -> Optional[dict]:
    if not OPENROUTER_KEY or not llm_available():
        return None
    try:
        r = httpx.post(
            LLM_BASE + "/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
            json={"model": LLM_MODEL, "max_tokens": 120, "temperature": 0,
                  "messages": [{"role": "user", "content": _LLM_PROMPT + json.dumps(text, ensure_ascii=False)}]},
            timeout=25,
        )
        if r.status_code == 429:
            note_429()
            return None
        content = r.raise_for_status().json()["choices"][0]["message"]["content"]
        for cand in reversed(re.findall(r"\{[^{}]*\}", content, re.S)):
            try:
                data = json.loads(cand)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and "clinic" in data:
                return data
    except Exception as e:  # модель — необязательная роскошь, любая ошибка = откат на простой разбор
        log.warning("llm failed: %s", e)
    return None


def enrich_with_llm(p: Parsed, text: str) -> Parsed:
    """Для длинных фраз просим модель уточнить название/время/канал; простой разбор остаётся запасным."""
    if len(text.split()) < LLM_MIN_WORDS:
        return p
    data = llm_extract(text)
    if not data:
        return p
    if data.get("clinic"):
        p.query = str(data["clinic"]).strip().lower()
    if data.get("time") and not p.has_time:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(data["time"]))
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            p.ts = p.ts.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            p.has_time = True
    if data.get("kind") in ("call", "whatsapp", "sms") and p.kind == "call":
        p.kind = data["kind"]
    return p


# ---------- состояние ----------

@dataclass
class State:
    owner_id: Optional[int] = None
    offset: int = 0
    pending: dict = field(default_factory=dict)   # chat_id -> {"cands", "kind", "ts", "query"}
    manual: dict = field(default_factory=dict)    # chat_id -> [{"id", "name", "site"}] последняя пачка /next

    @classmethod
    def load(cls) -> "State":
        if STATE_PATH.exists():
            d = json.loads(STATE_PATH.read_text())
            return cls(owner_id=d.get("owner_id"), offset=d.get("offset", 0))
        return cls()

    def save(self) -> None:
        STATE_PATH.write_text(json.dumps({"owner_id": self.owner_id, "offset": self.offset}))


# ---------- клиенты ----------

class Backend:
    def __init__(self) -> None:
        self.c = httpx.Client(base_url=BACKEND, headers={"X-Token": MS_TOKEN}, timeout=15)

    def match(self, q: str) -> list[dict]:
        return self.c.get("match", params={"q": q, "n": 3}).raise_for_status().json()

    def manual(self, clinic_id: Optional[int], kind: str, ts: datetime, note: str = "", said: str = "") -> None:
        self.c.post("manual", json={"clinic_id": clinic_id, "kind": kind, "ts": ts.isoformat(), "note": note,
                                    "clinic_said": said}).raise_for_status()

    def add_clinic(self, name: str, site: str = "", phone: str = "") -> dict:
        return self.c.post("clinics", json={"name": name, "site": site, "phone": phone}).raise_for_status().json()

    def form_submitted(self, clinic_id: int, ts: datetime, source: str) -> dict:
        return self.c.post("form-submitted", json={"clinic_id": clinic_id, "ts": ts.isoformat(), "source": source}).raise_for_status().json()

    def row(self, clinic_id: int) -> Optional[dict]:
        for r in self.c.get("report").raise_for_status().json()["rows"]:
            if r["id"] == clinic_id:
                return r
        return None

    def pending(self) -> list[dict]:
        return self.c.get("pending").raise_for_status().json()

    def report_xlsx(self) -> bytes:
        return self.c.get("report.xlsx").raise_for_status().content

    def lead(self, clinic_id: int) -> dict:
        return self.c.get(f"lead/{clinic_id}").raise_for_status().json()

    def last_callback(self) -> Optional[dict]:
        rows = self.c.get("callbacks").raise_for_status().json()
        return rows[-1] if rows else None

    def delete_callback(self, cb_id: int) -> dict:
        return self.c.delete(f"callbacks/{cb_id}").raise_for_status().json()


class Telegram:
    def __init__(self, token: str) -> None:
        self.c = httpx.Client(base_url=f"https://api.telegram.org/bot{token}/", timeout=60)

    def updates(self, offset: int) -> list[dict]:
        r = self.c.get("getUpdates", params={"offset": offset, "timeout": 50, "allowed_updates": '["message"]'})
        return r.raise_for_status().json().get("result", [])

    def send(self, chat_id: int, text: str) -> None:
        self.c.post("sendMessage", json={"chat_id": chat_id, "text": text}).raise_for_status()

    def send_file(self, chat_id: int, name: str, data: bytes) -> None:
        self.c.post("sendDocument", data={"chat_id": chat_id}, files={"document": (name, data)}).raise_for_status()

    def download(self, file_id: str, dest: Path) -> Path:
        info = self.c.get("getFile", params={"file_id": file_id}).raise_for_status().json()["result"]
        token = self.c.base_url.path.strip("/").removeprefix("bot")
        data = httpx.get(f"https://api.telegram.org/file/bot{token}/{info['file_path']}", timeout=120).raise_for_status().content
        dest.parent.mkdir(parents=True, exist_ok=True)
        ext = Path(info["file_path"]).suffix or ".oga"
        dest = dest.with_suffix(ext)
        dest.write_bytes(data)
        return dest


# ---------- логика ----------

def fmt_time(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    return datetime.fromisoformat(iso).astimezone(MSK).strftime("%H:%M")


def describe(row: Optional[dict], name: str) -> str:
    if not row:
        return f"Записала: {name}."
    if not row["submitted_at"]:
        return (f"Записала: {name}, перезвон в {fmt_time(row['first_callback'])}. "
                f"Заявку им мы не оставляли, так что дельты не будет — но в отчёте клиника есть.")
    if row["callback_minutes"] is None:
        return f"{name}: заявка в {fmt_time(row['submitted_at'])}, а перезвон раньше неё? Проверь время."
    mins = row["callback_minutes"]
    mood = "быстро!" if mins <= 5 else ("нормально." if mins <= 30 else "долго.")
    return (f"{name}: заявка в {fmt_time(row['submitted_at'])}, перезвон в {fmt_time(row['first_callback'])} — "
            f"{mins:.0f} мин, {mood}")


def record(be: Backend, clinic_id: int, name: str, kind: str, ts: datetime) -> str:
    before = be.row(clinic_id)
    extra = ""
    if before and before["first_callback"]:
        extra = f"\n(перезвон уже был в {fmt_time(before['first_callback'])}, в отчёт идёт первый)"
    be.manual(clinic_id, kind, ts, note="telegram")
    return describe(be.row(clinic_id), name) + extra


def ask(tg: Telegram, chat_id: int, st: State, query: str, cands: list[dict], kind: str, ts: datetime) -> None:
    st.pending[chat_id] = {"cands": cands, "kind": kind, "ts": ts.isoformat(), "query": query}
    name = pretty_name(query)   # query здесь — «сырое» название пользователя
    if not cands:
        tg.send(chat_id, f"В списке «{name}» не нашла. «да» — заведу как новую клинику; «?» — запишу звонок без клиники, "
                         f"разберёмся позже. Если знаешь номер, с которого звонили, пришли его — найду по номеру.")
        return
    lines = [f"{i}. {c['name']}" + ("" if c["pending"] else " (заявки не было)") for i, c in enumerate(cands, 1)]
    tg.send(chat_id, f"Похоже на кого-то из этих? Ответь цифрой:\n" + "\n".join(lines)
                     + f"\n\nЕсли это новая клиника «{name}» — ответь «да».")


def manual_batch(be: Backend, n: int = 5) -> list[dict]:
    """Клиники, где бот не справился, ещё не в списке: заводим их и отдаём ссылки для ручной заявки."""
    from common import CANDIDATES_CSV, CLINICS_CSV, normalize_domain, read_csv
    have = {normalize_domain(r["site"]) for r in read_csv(CLINICS_CSV) if r.get("site")}
    rows = [r for r in read_csv(CANDIDATES_CSV)
            if r["status"] in ("captcha", "no_form", "mask_fail") and r["domain"] not in have]
    rows.sort(key=lambda r: (r["status"] != "captcha", -int(r["points"] or 0)))
    out = []
    for r in rows[:n]:
        c = be.add_clinic(r["name"], site=r["site"], phone=r["phone"])
        out.append({"id": c["id"], "name": c["name"], "site": r["site"], "why": r["status"]})
    return out


def lead_card(be: Backend, cid: int) -> str:
    d = be.lead(cid)
    m, l, a = d["measure"], d.get("lpr") or {}, d.get("analysis") or {}
    name = m["clinic"]
    lines = [f"{name} — https://{d['site']}" if d.get("site") else name]
    if m["submitted_at"] and m["first_callback"]:
        lines.append(f"Заявка {fmt_time(m['submitted_at'])}, перезвон {fmt_time(m['first_callback'])}, {m['callback_minutes']:.0f} мин.")
    elif m["submitted_at"]:
        lines.append(f"Заявка {fmt_time(m['submitted_at'])}, перезвона нет уже {m['waiting_minutes']:.0f} мин.")
    else:
        lines.append("Заявку ещё не оставляли.")
    if a.get("score") is not None:
        lines.append(f"Разговор: {a['score']:.0f}/10" + (f", администратор {a['admin_name']}" if a.get("admin_name") else "") + ".")
        if a.get("summary"):
            lines.append(a["summary"])
    if l:
        if l.get("director"):
            lines.append(f"Руководитель: {l['director']}" + (f" ({l['director_post']})" if l.get("director_post") else "") + ".")
        if l.get("chief_doctor"):
            lines.append(f"Главный врач: {l['chief_doctor']}.")
        if l.get("legal_name"):
            lines.append(f"Юрлицо: {l['legal_name']}" + (f", ИНН {l['inn']}" if l.get("inn") else "") + ".")
        if l.get("emails"):
            lines.append(f"Почты: {l['emails']}.")
        if l.get("people") and not (l.get("director") or l.get("chief_doctor")):
            lines.append(f"Люди с сайта: {l['people']}.")
    else:
        lines.append("ЛПР ещё не искали: ./run.sh lpr --ids " + str(cid))
    # черновик письма
    to = l.get("director") or l.get("chief_doctor") or ""
    greet = f"Здравствуйте, {to.split()[1] if to and len(to.split()) > 1 else ''}!".replace(" !", "!")
    if m["submitted_at"] and m["first_callback"]:
        fact = (f"{datetime.fromisoformat(m['submitted_at']).strftime('%d.%m')} в {fmt_time(m['submitted_at'])} я оставил заявку на "
                f"консультацию по имплантации на вашем сайте. Перезвонили через {m['callback_minutes']:.0f} мин.")
    elif m["submitted_at"]:
        fact = (f"{datetime.fromisoformat(m['submitted_at']).strftime('%d.%m')} в {fmt_time(m['submitted_at'])} я оставил заявку на "
                f"консультацию по имплантации на вашем сайте, и мне так и не перезвонили.")
    else:
        fact = ""
    if fact:
        lines.append("\nЧерновик:\n" + greet + " Меня зовут Радмир. " + fact +
                     (" " + a["summary"] if a.get("summary") else "") +
                     " Я делаю сервис, который отвечает на такие заявки за минуту, и хотел бы показать цифры по вашей клинике "
                     "и по рынку. Найдётся 15 минут на этой неделе?")
    return "\n".join(lines)


def handle_audio(m: dict, chat_id: int, be: Backend, tg: Telegram) -> None:
    f = m.get("voice") or m.get("audio") or m.get("document") or {}
    if not f.get("file_id"):
        return
    last = be.last_callback()
    ts = datetime.fromtimestamp(m["date"], tz=timezone.utc).astimezone(MSK)
    dest = tg.download(f["file_id"], INBOX / ts.strftime("%Y%m%d_%H%M%S"))
    hint = f" Привяжу к последнему перезвону: {last['clinic']} {fmt_time(last['started_at'])}." if last and last.get("clinic") else ""
    tg.send(chat_id, f"Получила запись, расшифровываю, это 1–2 минуты.{hint}")
    import subprocess
    cmd = [str(CALLS_PY), str(PIPELINE), str(dest), "--quiet"] + (["--clinic-id", str(last["clinic_id"])] if last and last.get("clinic_id") else [])
    env = dict(os.environ)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env, cwd=str(ROOT.parent / "calls"))
        if out.returncode != 0:
            log.warning("pipeline failed: %s", out.stderr[-500:])
            tg.send(chat_id, "Не смогла обработать запись: " + (out.stderr.strip().splitlines() or ["?"])[-1][:200])
            return
        tg.send(chat_id, out.stdout.split("---")[0].strip() or "Готово.")
    except subprocess.TimeoutExpired:
        tg.send(chat_id, "Расшифровка не уложилась в 10 минут, посмотри logs/bot.log.")


def handle(text: str, msg_time: datetime, chat_id: int, be: Backend, tg: Telegram, st: State) -> None:
    p = parse_message(text, msg_time)

    if p.command:
        if p.command == "start":
            tg.send(chat_id, "Привет, я Луиза. После звонка клиники просто напиши мне её название — "
                             "я найду её в списке и посчитаю, за сколько минут перезвонили. /help — подсказка.")
        elif p.command == "help":
            tg.send(chat_id, __doc__.split("Сообщения:")[1].split("Длинные фразы")[0].strip())
        elif p.command == "pending":
            rows = be.pending()
            if not rows:
                tg.send(chat_id, "Все, кому оставили заявку, уже перезвонили. Или заявок пока нет.")
            else:
                lines = [f"{r['clinic']} — ждём {r['waiting_minutes']:.0f} мин (заявка в {fmt_time(r['submitted_at'])})" for r in rows]
                tg.send(chat_id, "Ещё не перезвонили:\n" + "\n".join(lines))
        elif p.command == "report":
            tg.send_file(chat_id, "report.xlsx", be.report_xlsx())
        elif p.command == "lead":
            q = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            if not q:
                tg.send(chat_id, "Кого показать? /lead Легенда")
                return
            cands = be.match(q)
            if not cands:
                tg.send(chat_id, "Не нашла такую клинику.")
                return
            tg.send(chat_id, lead_card(be, cands[0]["id"]))
        elif p.command in ("digest", "stats"):
            import subprocess
            subprocess.Popen([str(ROOT.parent / "digest.sh")], cwd=str(ROOT.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif p.command == "next":
            batch = manual_batch(be)
            if not batch:
                tg.send(chat_id, "Ручных клиник не осталось — все, где бот не справился, уже в списке.")
                return
            st.manual[chat_id] = batch
            why = {"captcha": "капча", "no_form": "форма не нашлась", "mask_fail": "маска телефона"}
            lines = [f"{i}. {b['name']} — https://{b['site']} ({why.get(b['why'], b['why'])})" for i, b in enumerate(batch, 1)]
            tg.send(chat_id, "Эти сайты бот не осилил. Оставь заявку руками (имя, " + os.environ.get("MS_PHONE", "твой номер") + ", «консультация "
                             "по имплантации») и напиши «готово 1 3» — время зафиксирую по сообщению.\n\n" + "\n".join(lines))
        elif p.command == "wave":
            if PAUSE_FLAG.exists():
                tg.send(chat_id, "Стоит пауза. Сначала /resume.")
                return
            import subprocess
            subprocess.Popen([str(DAILY_SH)], cwd=str(ROOT.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            tg.send(chat_id, "Запускаю волну, отчитаюсь, когда закончу.")
        elif p.command == "pause":
            PAUSE_FLAG.parent.mkdir(exist_ok=True)
            PAUSE_FLAG.touch()
            tg.send(chat_id, "Ежедневные волны на паузе. /resume — снять.")
        elif p.command == "resume":
            PAUSE_FLAG.unlink(missing_ok=True)
            tg.send(chat_id, "Пауза снята, волны идут по будням в 12:00.")
        else:
            tg.send(chat_id, "Такой команды не знаю. /help")
        return

    if p.unknown:
        be.manual(None, p.kind, p.ts, note="telegram", said="")
        tg.send(chat_id, f"Ок, звонок в {p.ts.strftime('%H:%M')} записала без клиники. "
                         f"Узнаешь, кто это был — напиши «Название {p.ts.strftime('%H:%M')}», привяжу.")
        return

    pend = st.pending.get(chat_id)

    if p.negative:
        if pend:
            st.pending.pop(chat_id, None)
            tg.send(chat_id, "Ок, сняла вопрос. Ничего не записала.")
            return
        last = be.last_callback()
        if last and (datetime.now(MSK) - datetime.fromisoformat(last["started_at"])).total_seconds() < 6 * 3600:
            r = be.delete_callback(last["id"])
            tg.send(chat_id, f"Откатила последнюю запись: {r.get('clinic_name') or 'клиника'} в {fmt_time(last['started_at'])}.")
        else:
            tg.send(chat_id, "Откатывать нечего — за последние 6 часов записей не было.")
        return

    if p.wave_at is not None:
        if p.wave_at:
            WAVE_AT.parent.mkdir(exist_ok=True)
            WAVE_AT.write_text(p.wave_at)
            tg.send(chat_id, f"Хорошо, сегодня волна пойдёт в {p.wave_at} вместо 12:00.")
        else:
            tg.send(chat_id, "Про волну: /wave — запустить сейчас, «волну на 13:00» — перенести сегодня, /pause — отменить.")
        return

    if p.done is not None:
        batch = st.manual.get(chat_id) or []
        if not batch:
            tg.send(chat_id, "Не помню ручной пачки — сначала /next.")
            return
        picked = [batch[i - 1] for i in p.done if 1 <= i <= len(batch)] if p.done else batch
        if not picked:
            tg.send(chat_id, f"Номера от 1 до {len(batch)}, например «готово 1 3».")
            return
        for b in picked:
            be.form_submitted(b["id"], p.ts, "manual")
        st.manual[chat_id] = [b for b in batch if b not in picked]
        tg.send(chat_id, "Зафиксировала заявки в " + p.ts.strftime("%H:%M") + ": " + ", ".join(b["name"] for b in picked)
                         + ". Теперь жди звонков.")
        return

    if p.choice is not None:
        if not pend or not (1 <= p.choice <= len(pend["cands"])):
            tg.send(chat_id, "Мне нечего выбирать — сначала напиши название клиники.")
            return
        st.pending.pop(chat_id)
        c = pend["cands"][p.choice - 1]
        tg.send(chat_id, record(be, c["id"], c["name"], pend["kind"], datetime.fromisoformat(pend["ts"])))
        return

    if p.confirm:
        if not pend or not pend.get("query"):
            tg.send(chat_id, "Что записать? Напиши название клиники.")
            return
        st.pending.pop(chat_id)
        name = pretty_name(pend["query"])
        created = be.add_clinic(name)
        prefix = "" if created.get("existed") else f"Добавила в список «{created['name']}» (№{created['id']}).\n"
        tg.send(chat_id, prefix + record(be, created["id"], created["name"], pend["kind"], datetime.fromisoformat(pend["ts"])))
        return

    p = enrich_with_llm(p, text)

    if p.phone and not p.query:
        cands = be.c.get("match", params={"phone": p.phone, "n": 3}).raise_for_status().json()
        if cands:
            tg.send(chat_id, record(be, cands[0]["id"], cands[0]["name"], p.kind, p.ts) + "\n(нашла по номеру телефона)")
            return
        tg.send(chat_id, f"Номер {p.phone} в базе клиник не нашла. Напиши название.")
        return

    if not p.query and len(text.split()) >= 4 and not pend:
        tg.send(chat_id, "Не поняла. Если это клиника — напиши только её название. Если просьба — команды в /help, "
                         "перенос волны: «волну на 13:00», откат записи: «отмени».")
        return

    if not p.query:
        if pend and (p.has_time or p.kind != "call"):
            # уточнили только время/канал к предыдущему вопросу
            if p.has_time:
                pend["ts"] = p.ts.isoformat()
            if p.kind != "call":
                pend["kind"] = p.kind
            tg.send(chat_id, f"Поняла, время {p.ts.strftime('%H:%M')}. Так кто это был — цифра из списка или «да» для новой?")
            return
        tg.send(chat_id, "Напиши название клиники, например «Легенда» или «Легенда 14:35».")
        return

    cands = be.match(p.query)
    if not cands and p.phone:
        cands = be.c.get("match", params={"phone": p.phone, "n": 3}).raise_for_status().json()
    cid = decide(cands)
    if cid is None and p.suspicious:
        tg.send(chat_id, "Не поняла. Если это клиника — напиши только её название. Команды: /help")
        return
    if cid is not None:
        st.pending.pop(chat_id, None)
        tg.send(chat_id, record(be, cid, cands[0]["name"], p.kind, p.ts))
        return
    # слабые совпадения без заявки только запутают — показываем лишь тех, кому заявку оставляли, или сильных
    cands = [c for c in cands if c["pending"] or c["score"] >= 60]
    ask(tg, chat_id, st, p.raw or p.query, cands, p.kind, p.ts)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)   # иначе токен бота попадает в лог с URL
    if not TG_TOKEN:
        sys.exit("MS_TG_TOKEN не задан")
    st = State.load()
    tg = Telegram(TG_TOKEN)
    be = Backend()
    log.info("bot started, owner=%s, backend=%s, llm=%s", st.owner_id, BACKEND, LLM_MODEL if OPENROUTER_KEY else "off")
    while True:
        try:
            for u in tg.updates(st.offset):
                st.offset = u["update_id"] + 1
                st.save()
                m = u.get("message") or {}
                is_audio = any(k in m for k in ("voice", "audio", "document"))
                if not m.get("text") and not is_audio:
                    continue
                chat_id = m["chat"]["id"]
                uid = m["from"]["id"]
                msg_time = datetime.fromtimestamp(m["date"], tz=timezone.utc)
                if st.owner_id is None and (m.get("text") or "").strip().startswith("/start"):
                    st.owner_id = uid
                    st.save()
                    log.info("owner set to %s", uid)
                if uid != st.owner_id:
                    tg.send(chat_id, f"Этот бот приватный. Твой id: {uid}")
                    continue
                try:
                    if is_audio and not m.get("text"):
                        handle_audio(m, chat_id, be, tg)
                        continue
                    handle(m["text"], msg_time, chat_id, be, tg, st)
                except httpx.HTTPError as e:
                    log.exception("backend/telegram error")
                    tg.send(chat_id, f"Ошибка: {e}. Бэкенд запущен?")
        except httpx.HTTPError as e:
            log.warning("poll error: %s", e)
            time.sleep(5)
        except Exception:
            log.exception("unexpected")
            time.sleep(5)


if __name__ == "__main__":
    main()
