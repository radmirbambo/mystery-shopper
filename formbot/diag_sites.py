"""Диагностика: что именно на сайте — реальный виджет капчи, невидимая reCAPTCHA v3 или просто слово в скрипте.
    .venv/bin/python diag_sites.py            # все сайты из clinics.csv
    .venv/bin/python diag_sites.py 2,4,10     # только эти id
"""
import csv, json, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
clinics = list(csv.DictReader(open(ROOT.parent / "data" / "clinics.csv", encoding="utf-8")))
if len(sys.argv) > 1:
    want = set(sys.argv[1].split(","))
    clinics = [c for c in clinics if c["id"] in want]
clinics = [c for c in clinics if c["site"].strip()]

JS = r"""
() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const q = (s) => Array.from(document.querySelectorAll(s));
  const html = document.documentElement.outerHTML.toLowerCase();
  const scripts = q('script[src]').map(s => s.src).filter(s => /captcha|turnstile/i.test(s));
  const widgets = q('.g-recaptcha, .h-captcha, .smart-captcha, .cf-turnstile, [data-sitekey], iframe[src*="captcha"], iframe[src*="turnstile"], .captcha, [class*="captcha"], [id*="captcha"]');
  const w = widgets.map(e => ({tag: e.tagName.toLowerCase(), cls: (e.className||'').toString().slice(0,60), id: e.id, visible: vis(e), inForm: !!e.closest('form')}));
  const v3 = scripts.some(s => /recaptcha\/api\.js\?render=/.test(s)) || !!document.querySelector('.grecaptcha-badge');
  const tels = q('input').filter(i => i.type === 'tel' || /tel|phone|телефон/i.test((i.name||'')+(i.id||'')+(i.placeholder||'')));
  const forms = q('form').length;
  const btns = q('button, a, [role=button]').filter(vis).map(b => (b.innerText||'').trim()).filter(t => /запис|звон|заявк|консульт/i.test(t)).slice(0,8);
  return {captcha_scripts: scripts, captcha_widgets: w, recaptcha_v3_badge: v3,
          word_captcha_in_html: html.includes('captcha'),
          tel_inputs: tels.length, tel_visible: tels.filter(vis).length, forms, cta_buttons: btns,
          title: document.title};
}
"""
out = {}
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(locale="ru-RU", viewport={"width": 1280, "height": 900},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
    for c in clinics:
        page = ctx.new_page()
        url = c["site"] if c["site"].startswith("http") else "https://" + c["site"]
        try:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
            except Exception as e:
                if "interrupted" not in repr(e): raise
                time.sleep(3); page.wait_for_load_state("domcontentloaded", timeout=20000)
            time.sleep(3)
            d = page.evaluate(JS)
            d["final_url"] = page.url
        except Exception as e:
            d = {"error": repr(e)[:150]}
        out[c["id"]] = {"name": c["name"], "site": c["site"], **d}
        print(c["id"], c["name"], json.dumps(d, ensure_ascii=False)[:400], flush=True)
        page.close()
    b.close()
json.dump(out, open(ROOT / "diag_sites.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
