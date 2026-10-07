"""
Оставляет заявку «консультация по имплантации» на сайте каждой клиники из data/clinics.csv
и фиксирует время в бэкенде (POST /form-submitted).

    pip install -r requirements.txt && playwright install chromium
    export OPENROUTER_API_KEY=...         # LLM-подбор формы для «сложных» страниц (бесплатная модель)
    export MS_BACKEND=https://YOUR-HOST/  MS_TOKEN=change-me
    export MS_NAME="Радмир"  MS_PHONE="+7 9XX XXX-XX-XX"   # тот номер, на который смотрит Voximplant
    python fill_forms.py                 # по умолчанию: показывает заполненную форму и ждёт Enter перед отправкой
    python fill_forms.py --auto          # без подтверждения
    python fill_forms.py --ids 16,44     # только эти клиники
    python fill_forms.py --dry           # ничего не отправлять, только найти формы и сделать скриншоты

Правила, зашитые в код:
  • одна заявка на клинику (бэкенд отдаёт 409 на повтор, бот перед стартом проверяет /submitted/{id});
  • если на странице капча — сайт помечается «manual», никаких обходов;
  • между сайтами пауза 20–40 секунд;
  • скриншоты до и после отправки — в ./screenshots, это доказательство для письма ЛПР.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
from llm_guard import llm_available, note_429  # noqa: E402

ROOT = Path(__file__).resolve().parent
CLINICS_CSV = ROOT.parent / "data" / "clinics.csv"
SHOTS = ROOT / "screenshots"
SHOTS.mkdir(exist_ok=True)
LOG = ROOT / "formbot_log.csv"

BACKEND = os.environ.get("MS_BACKEND", "http://127.0.0.1:8000/").rstrip("/") + "/"
TOKEN = os.environ.get("MS_TOKEN", "change-me")
NAME = os.environ.get("MS_NAME", "Радмир")
PHONE = os.environ.get("MS_PHONE", "")
MESSAGE = os.environ.get("MS_MESSAGE", "Хочу записаться на консультацию по имплантации. Перезвоните, пожалуйста.")
OPENROUTER_KEY = os.environ.get("MS_LLM_KEY") or os.environ.get("OPENROUTER_API_KEY", "") or "local"
LLM_MODEL = os.environ.get("MS_LLM_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
LLM_BASE = os.environ.get("MS_LLM_BASE", "https://openrouter.ai/api/v1").rstrip("/")
GOTO_TIMEOUT = int(os.environ.get("MS_GOTO_TIMEOUT", "25000"))
MSK = timezone(timedelta(hours=3))

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
CAPTCHA_MARKERS = ["recaptcha", "hcaptcha", "smartcaptcha", "captcha", "cf-turnstile"]
PHONE_HINTS = ["tel", "phone", "телефон", "номер"]
NAME_HINTS = ["name", "имя", "фио", "как вас зовут"]
MSG_HINTS = ["message", "comment", "сообщение", "комментар", "вопрос"]
SUBMIT_HINTS = ["отправ", "записат", "перезвон", "заказать", "оставить", "получить", "консультац", "submit", "send"]
MARKETING_HINTS = ["рассылк", "реклам", "новост", "акци", "уведомлен", "подписк"]   # такие галочки не трогаем
OPEN_HINTS = ["записаться", "запись", "заказать звонок", "обратный звонок", "позвоните мне", "позвонить мне",
              "перезвон", "оставить заявку", "консультац"]

# JS: есть ли на странице настоящая капча (виджет или подключённый скрипт), а не просто слово в html
CAPTCHA_JS = """
() => {
  const w = document.querySelector('.g-recaptcha, .h-captcha, .smart-captcha, .cf-turnstile, [data-sitekey], '
    + 'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="smartcaptcha"], iframe[src*="turnstile"], .grecaptcha-badge');
  const s = Array.from(document.querySelectorAll('script[src]')).map(x => x.src)
    .filter(x => /recaptcha|hcaptcha|smartcaptcha|turnstile/i.test(x));
  if (w) return 'captcha widget: ' + (w.className || w.tagName).toString().slice(0, 40);
  if (s.length) return 'captcha script: ' + s[0].slice(0, 80);
  return '';
}
"""

# JS: помечает все интерактивные элементы data-ms="N" и возвращает их компактное описание
COLLECT_JS = """
() => {
  const out = [];
  const els = document.querySelectorAll('input, textarea, button, [role=button], label, a.btn, [type=submit]');
  let n = 0;
  const visible = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  els.forEach((el) => {
    n += 1; el.setAttribute('data-ms', String(n));
    const form = el.closest('form');
    if (form && !form.getAttribute('data-ms-form')) form.setAttribute('data-ms-form', 'f' + n);
    let label = '';
    if (el.id) { const l = document.querySelector('label[for="' + el.id + '"]'); if (l) label = l.innerText; }
    if (!label && el.closest('label')) label = el.closest('label').innerText;
    if (!label && el.getAttribute('type') === 'checkbox' && el.parentElement) label = el.parentElement.innerText;
    out.push({ ms: n, tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || '',
      name: el.getAttribute('name') || '', id: el.id || '', placeholder: el.getAttribute('placeholder') || '',
      text: (el.innerText || el.value || '').trim().slice(0, 80), label: (label || '').trim().slice(0, 80),
      form: form ? form.getAttribute('data-ms-form') : '', visible: visible(el) });
  });
  return out;
}
"""


def log(row: dict) -> None:
    new = not LOG.exists()
    with open(LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ts", "id", "clinic", "site", "status", "note"])
        if new:
            w.writeheader()
        w.writerow(row)


def has(el: dict, hints: list[str]) -> bool:
    blob = " ".join([el["type"], el["name"], el["id"], el["placeholder"], el["label"], el["text"]]).lower()
    return any(h in blob for h in hints)


def pick_by_heuristics(els: list[dict]) -> dict | None:
    """Ищем видимый input телефона; берём его форму; в ней — имя, сообщение, согласие, кнопку."""
    phones = [e for e in els if e["visible"] and e["tag"] == "input" and (e["type"] == "tel" or has(e, PHONE_HINTS))]
    if not phones:
        return None
    phone = phones[0]
    same = [e for e in els if e["form"] == phone["form"] and e["form"]] if phone["form"] else phones[:1]
    name = next((e for e in same if e["tag"] == "input" and e["type"] in ("", "text") and has(e, NAME_HINTS)), None)
    msg = next((e for e in same if e["tag"] == "textarea" or has(e, MSG_HINTS)), None)
    consents = [e for e in same if e["type"] == "checkbox" and not has(e, MARKETING_HINTS)]
    submit = next((e for e in same if (e["type"] == "submit" or e["tag"] == "button") and e["visible"]), None)
    if not submit:
        submit = next((e for e in els if e["visible"] and e["tag"] in ("button",) and has(e, SUBMIT_HINTS)), None)
    if not submit:
        return None
    return {"phone": phone["ms"], "name": name["ms"] if name else None, "message": msg["ms"] if msg else None,
            "consent": [e["ms"] for e in consents], "submit": submit["ms"], "how": "heuristics"}


def pick_by_llm(els: list[dict], site: str) -> dict | None:
    """Для страниц, где эвристика не справилась: отдаём список элементов модели (OpenRouter), просим выбрать."""
    if not OPENROUTER_KEY or not llm_available():
        return None
    vis = [e for e in els if e["visible"]][:150]
    prompt = (
        "Это интерактивные элементы страницы стоматологической клиники " + site + ". "
        "Найди форму заявки/обратного звонка для пациента (НЕ поиск, НЕ подписка на рассылку, НЕ логин). "
        "Верни ТОЛЬКО JSON без пояснений: {\"phone\": ms|null, \"name\": ms|null, \"message\": ms|null, "
        "\"consent\": ms|null, \"submit\": ms|null}. Если подходящей формы нет — {\"phone\": null}. "
        "Если поле телефона видно только после клика по кнопке вроде «Записаться» — верни "
        "{\"open\": ms} с этой кнопкой. Ответ начни сразу с символа {.\n\n" + json.dumps(vis, ensure_ascii=False)
    )
    try:
        r = httpx.post(LLM_BASE + "/chat/completions",
                       headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
                       json={"model": LLM_MODEL, "max_tokens": 700, "temperature": 0,
                             "messages": [{"role": "user", "content": prompt}]}, timeout=60)
        if r.status_code == 429:
            note_429()
            print("   llm: 429, пауза 15 мин")
            return None
        text = r.raise_for_status().json()["choices"][0]["message"]["content"] or ""
    except Exception as e:
        print("   llm error:", repr(e)[:100])
        return None
    objs = re.findall(r"\{[^{}]*\}", text)   # модель может сначала порассуждать — берём последний объект
    data = None
    for cand in reversed(objs):
        try:
            data = json.loads(cand)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(data, dict):
        return None
    if data.get("open"):
        return {"open": data["open"], "how": "llm-open"}
    if not data.get("phone") or not data.get("submit"):
        return None
    data["how"] = "llm"
    return data


SUCCESS_MARKERS = ["спасибо", "заявка принята", "свяжемся", "заявка отправлена", "успешно", "мы вам перезвоним",
                   "заявка получена", "скоро перезвоним", "thank you"]


def page_says_success(page: Page) -> bool:
    try:
        body = page.content().lower()
        return any(m in body for m in SUCCESS_MARKERS)
    except Exception:
        return False


def find_in_frames(page: Page):
    """Форма записи внутри iframe (Bitrix24, Yclients-виджеты): возвращает (frame, plan) или (None, None)."""
    for fr in page.frames[1:]:
        if not fr.url or fr.url.startswith("about:") or "captcha" in fr.url or "recaptcha" in fr.url:
            continue
        try:
            els = fr.evaluate(COLLECT_JS)
        except Exception:
            continue
        plan = pick_by_heuristics(els)
        if plan:
            return fr, plan
    return None, None


REVEAL_JS = """
(k) => {
  const isTel = (i) => i.type === 'tel' || /tel|phone|телефон|номер/i.test((i.name||'') + (i.id||'') + (i.placeholder||''));
  const bad = /отзыв|review|поиск|search|подпис|subscribe|войти|login|вакан/i;
  const inputs = Array.from(document.querySelectorAll('input')).filter(i => isTel(i) && i.closest('form')
    && !bad.test((i.closest('form').innerText || '') + ' ' + (i.closest('form').className || '') + ' ' + (i.closest('form').id || '')));
  const hidden = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width === 0 || r.height === 0 || s.visibility === 'hidden' || s.display === 'none'; };
  // одна форма — один кандидат
  const seen = new Set(); const cands = [];
  for (const i of inputs) { const f = i.closest('form'); if (hidden(i) && !seen.has(f)) { seen.add(f); cands.push(i); } }
  const target = cands[k];
  if (!target) return false;
  // раскрываем всех предков модалки: display/visibility/opacity/transform, чтобы форму можно было заполнить и отправить
  let el = target;
  while (el && el !== document.body) {
    el.style.setProperty('display', el.tagName === 'INPUT' ? 'inline-block' : 'block', 'important');
    el.style.setProperty('visibility', 'visible', 'important');
    el.style.setProperty('opacity', '1', 'important');
    el.style.setProperty('transform', 'none', 'important');
    el.style.setProperty('pointer-events', 'auto', 'important');
    el.style.setProperty('max-height', 'none', 'important');
    el.style.setProperty('height', 'auto', 'important');
    if (el.hasAttribute('hidden')) el.removeAttribute('hidden');
    el = el.parentElement;
  }
  target.scrollIntoView({block: 'center'});
  return !hidden(target);
}
"""


def reveal_hidden_form(page, k: int = 0) -> bool:
    """k-я скрытая форма записи (в модалке, которую не открыть кнопкой) — раскрываем стилями."""
    try:
        return bool(page.evaluate(REVEAL_JS, k))
    except Exception:
        return False


def sel(ms: int) -> str:
    return f'[data-ms="{ms}"]'


def dismiss_popups(page: Page) -> None:
    for text in ["Принять", "Согласен", "Понятно", "Хорошо", "Ок", "OK", "Закрыть", "Accept"]:
        try:
            btn = page.locator("button, a, [role=button], div[class*=cookie] *").filter(has_text=re.compile("^\\s*" + text + "\\s*$", re.I)).first
            if btn.count() and btn.is_visible(timeout=500):
                btn.click(timeout=1000)
                time.sleep(0.5)
        except Exception:
            pass


def page_has_captcha(page: Page) -> str:
    """'' если капчи нет, иначе описание (виджет/скрипт). Обходов нет — только детекция."""
    try:
        return page.evaluate(CAPTCHA_JS) or ""
    except Exception:
        html = page.content().lower()
        return "captcha marker in html" if any(m in html for m in CAPTCHA_MARKERS) else ""


def type_phone(scope, page: Page, ms: int) -> bool:
    """Вводит номер с учётом маски. Маски бывают трёх видов: пустое поле (нужен +7…), поле с готовым «+7 (»
    (нужны только 10 цифр), поле, ждущее 8…. Пробуем варианты, пока в value не окажутся наши 10 цифр."""
    digits = re.sub(r"\D", "", PHONE)
    local = digits[-10:]
    loc = scope.locator(sel(ms))
    try:
        loc.click(force=True, timeout=5000)
    except Exception:
        return False
    for candidate in (local, "+7" + local, "8" + local):
        loc.press("Control+A")
        loc.press("Backspace")
        loc.press("Meta+A")
        loc.press("Backspace")
        # некоторые маски дописывают «+7 (» после очистки — учитываем это, вводя только 10 цифр
        page.keyboard.type(candidate, delay=60)
        time.sleep(0.3)
        got = re.sub(r"\D", "", loc.input_value())
        if got.endswith(local) and 10 <= len(got) <= 11:
            return True
    # поле раскрыто принудительно или маска не пускает клавиатуру — ставим значение через JS с событиями
    for candidate in ("+7" + local, local, "8" + local):
        try:
            scope.evaluate("""([s, v]) => { const el = document.querySelector(s);
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                setter.call(el, v);
                el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }""",
                [sel(ms), candidate])
            time.sleep(0.3)
            got = re.sub(r"\D", "", loc.input_value())
            if got.endswith(local) and 10 <= len(got) <= 11:
                return True
        except Exception:
            continue
    return False


def set_consent(scope, ms: int) -> str:
    """Ставит галочку согласия: сначала check(), потом клик по label, потом JS. Возвращает 'ok' | 'failed'."""
    box = scope.locator(sel(ms))
    try:
        if box.is_checked():
            return "ok"
    except Exception:
        pass
    for attempt in ("check", "label", "js"):
        try:
            if attempt == "check":
                box.check(force=True, timeout=2000)
            elif attempt == "label":
                scope.evaluate("""(s) => { const el = document.querySelector(s);
                    const lbl = (el.id && document.querySelector('label[for="' + el.id + '"]')) || el.closest('label')
                                || (el.nextElementSibling && el.nextElementSibling.tagName === 'LABEL' ? el.nextElementSibling : null);
                    if (lbl) lbl.click(); }""", sel(ms))
            else:
                scope.evaluate("""(s) => { const el = document.querySelector(s); el.checked = true;
                    el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }""", sel(ms))
            time.sleep(0.3)
            if box.is_checked():
                return "ok"
        except Exception:
            pass
    return "failed"


def open_form_by_button(page: Page) -> bool:
    """Форма спрятана за кнопкой «Записаться»/«Заказать звонок»: кликаем первую подходящую видимую кнопку."""
    start_url = page.url
    for hint in OPEN_HINTS:
        btns = page.locator("button, a, [role=button], div, span").filter(has_text=re.compile("^\\s*" + hint, re.I))
        for i in range(min(btns.count(), 12)):
            btn = btns.nth(i)
            try:
                if not btn.is_visible(timeout=300) or len(btn.inner_text() or "") > 60:
                    continue
                href = (btn.get_attribute("href") or "").strip()
                if href and not href.startswith(("#", "javascript:")):
                    continue   # это ссылка на другую страницу, а не кнопка модалки
                btn.click(timeout=3000)
                if page.url != start_url:
                    page.go_back(wait_until="domcontentloaded")
                    time.sleep(1.5)
                    continue
                for _ in range(8):   # модалка может анимироваться — ждём до 4 с
                    time.sleep(0.5)
                    if any(e["visible"] and e["tag"] == "input" and (e["type"] == "tel" or has(e, PHONE_HINTS))
                           for e in page.evaluate(COLLECT_JS)):
                        return True
            except Exception:
                continue
    return False


def fill_and_submit(page: Page, plan: dict, clinic: dict, args, scope=None) -> tuple[str, str]:
    """Возвращает (status, note). status: submitted | dry | skipped | error. scope — Page или Frame с формой."""
    scope = scope or page
    tag = f'{clinic["id"]}_{re.sub(r"[^a-zA-Zа-яА-Я0-9]+", "_", clinic["name"])[:40]}'
    if plan.get("name"):
        try:
            scope.fill(sel(plan["name"]), NAME, force=True, timeout=5000)
        except Exception:
            pass
    phone_ok = type_phone(scope, page, plan["phone"])
    if plan.get("message"):
        try:
            scope.fill(sel(plan["message"]), MESSAGE, force=True, timeout=5000)
        except Exception:
            pass
    consent_ids = plan.get("consent") or []
    if not isinstance(consent_ids, list):
        consent_ids = [consent_ids]
    consent = ",".join(set_consent(scope, ms) for ms in consent_ids) or "none"
    try:
        scope.locator(sel(plan["submit"])).scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    page.screenshot(path=str(SHOTS / f"{tag}_before.png"), full_page=False)
    detail = f'{plan["how"]}{"; iframe" if scope is not page else ""}; phone={"ok" if phone_ok else "MASK FAILED"}; consent={consent}'
    if not phone_ok:
        return "skipped", "phone mask failed: " + detail

    if args.dry:
        return "dry", detail
    if not args.auto:
        print(f"   форма заполнена ({plan['how']}). Enter — отправить, s — пропустить: ", end="", flush=True)
        if sys.stdin.readline().strip().lower() == "s":
            return "skipped", "manual skip"

    ts = datetime.now(MSK).isoformat()
    clicked_via = "click"
    try:
        scope.click(sel(plan["submit"]), timeout=5000)
    except Exception:
        # клик не прошёл (кнопку перекрыл виджет) ИЛИ прошёл, но страница ушла в навигацию и бросила исключение.
        # Прежде чем кликать повторно через JS, проверяем, не отправилась ли форма уже — иначе клиника получит дубль.
        time.sleep(3)
        if page_says_success(page):
            clicked_via = "click(nav)"
        else:
            dismiss_popups(page)
            try:
                scope.evaluate("(s) => document.querySelector(s).click()", sel(plan["submit"]))
                clicked_via = "js"
            except Exception:
                clicked_via = "js-failed"
    time.sleep(4)
    success = page_says_success(page)
    detail += f"; submit={clicked_via}"
    cap = page_has_captcha(page)
    if cap and not success:
        if not getattr(args, "assist", False):
            page.screenshot(path=str(SHOTS / f"{tag}_after.png"), full_page=False)
            return "skipped", "captcha after submit (" + cap + ")"
        # режим assist: капчу решает человек в открытом браузере, мы только ждём
        print(f"   ⚠ КАПЧА — реши её в браузере ({cap[:40]}). Жду до 120 с…", flush=True)
        for _ in range(60):
            time.sleep(2)
            if page_says_success(page):
                success = True
                break
        if not success:
            print("   отправилось? Enter — да, n — нет: ", end="", flush=True)
            if sys.stdin.readline().strip().lower() == "n":
                return "skipped", "assist: not sent"
            success = True
    page.screenshot(path=str(SHOTS / f"{tag}_after.png"), full_page=False)

    r = httpx.post(BACKEND + "form-submitted", json={"clinic_id": int(clinic["id"]), "ts": ts, "source": "formbot"},
                   headers={"X-Token": TOKEN}, timeout=10)
    return ("submitted" if success else "submitted?"), f'{detail}; backend {r.status_code}; success_marker={success}'


def process_site(page: Page, clinic: dict, args) -> tuple[str, str]:
    site = clinic["site"].strip()
    if not site:
        return "skipped", "no site"
    url = site if site.startswith("http") else "https://" + site
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT)
    except PWTimeout:
        return "error", "timeout"
    except Exception as e:
        if "interrupted" in repr(e):
            # сайт сразу редиректит (http→https, /→/ru/) — ждём, пока новая навигация закончится
            time.sleep(3)
            page.wait_for_load_state("domcontentloaded", timeout=20000)
        elif not site.startswith("http") and any(k in repr(e) for k in ("ERR_CERT", "ERR_SSL", "ERR_CONNECTION")):
            # битый сертификат или https не поднят — пробуем по http
            try:
                page.goto("http://" + site, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT)
            except PWTimeout:
                return "error", "timeout (http)"
        else:
            raise
    try:
        page.wait_for_load_state("load", timeout=15000)
    except Exception:
        pass
    time.sleep(3)
    dismiss_popups(page)
    cap = page_has_captcha(page)
    if cap and not getattr(args, "assist", False):
        return "skipped", "captcha on page (" + cap + ")"

    els = page.evaluate(COLLECT_JS)
    plan = pick_by_heuristics(els)
    if not plan and open_form_by_button(page):
        # форма спрятана за кнопкой — открыли по эвристике, ищем ещё раз
        els = page.evaluate(COLLECT_JS)
        plan = pick_by_heuristics(els)
        if not plan:
            cap = page_has_captcha(page)
            if cap and not getattr(args, "assist", False):
                return "skipped", "captcha in popup form (" + cap + ")"
    if not plan:
        frame, plan = find_in_frames(page)
        if plan:
            return fill_and_submit(page, plan, clinic, args, scope=frame)
    if not plan:
        for k in range(4):   # перебираем скрытые формы записи, пока эвристика не соберёт план
            if not reveal_hidden_form(page, k):
                break
            time.sleep(0.4)
            els = page.evaluate(COLLECT_JS)
            plan = pick_by_heuristics(els)
            if plan:
                plan["how"] = f"heuristics+reveal{k}"
                break
    if not plan:
        plan = pick_by_llm(els, site)
    if plan and plan.get("open"):
        # форма прячется за кнопкой «Записаться» — открываем и ищем ещё раз
        try:
            page.click(sel(plan["open"]), timeout=3000)
            time.sleep(1.5)
            els = page.evaluate(COLLECT_JS)
            plan = pick_by_heuristics(els) or pick_by_llm(els, site)
        except Exception:
            plan = None
    if not plan or plan.get("open"):
        return "skipped", "form not found"
    return fill_and_submit(page, plan, clinic, args)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true", help="не спрашивать подтверждения перед отправкой")
    ap.add_argument("--dry", action="store_true", help="только найти формы, не отправлять")
    ap.add_argument("--ids", default="", help="только эти id через запятую")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--assist", action="store_true", help="сайты с капчей: заполняем сами, капчу решаешь ты в браузере")
    args = ap.parse_args()

    if not PHONE and not args.dry:
        sys.exit("Укажи MS_PHONE — твой мобильный, на него будут перезванивать.")

    clinics = list(csv.DictReader(open(CLINICS_CSV, encoding="utf-8")))
    if args.ids:
        want = {x.strip() for x in args.ids.split(",")}
        clinics = [c for c in clinics if c["id"] in want]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        ctx = browser.new_context(
            locale="ru-RU", viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT, ignore_https_errors=True,
        )
        page = ctx.new_page()
        for c in clinics:
            print(f'[{c["id"]}] {c["name"]} — {c["site"] or "нет сайта"}')
            if not args.dry:
                try:
                    st = httpx.get(BACKEND + f'submitted/{c["id"]}', headers={"X-Token": TOKEN}, timeout=10).json()
                    if st.get("submitted"):
                        print("   уже оставляли, пропуск")
                        continue
                except Exception as e:
                    print("   бэкенд недоступен:", e)
                    continue
            try:
                status, note = process_site(page, c, args)
            except Exception as e:  # любая ошибка на одном сайте не должна ронять прогон
                status, note = "error", repr(e)[:200]
            print(f"   → {status}: {note}")
            log({"ts": datetime.now(MSK).isoformat(), "id": c["id"], "clinic": c["name"],
                 "site": c["site"], "status": status, "note": note})
            time.sleep(2 if (args.dry or args.assist) else random.uniform(20, 40))
        browser.close()


if __name__ == "__main__":
    main()
