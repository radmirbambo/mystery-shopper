"""pytest discover/"""
from common import make_aliases, normalize_domain, normalize_phone
from fetch_osm import group_by_domain
from promote import pick, to_clinic
from triage import classify


def test_normalize_domain():
    assert normalize_domain("https://www.Site.ru/path?x=1") == "site.ru"
    assert normalize_domain("site.ru") == "site.ru"
    assert normalize_domain("http://AdionaMed.ru") == "adionamed.ru"
    assert normalize_domain("https://vk.com/clinic") == ""
    assert normalize_domain("https://m.vk.com/clinic") == ""
    assert normalize_domain("") == ""
    assert normalize_domain("nonsense") == ""


def test_normalize_phone():
    assert normalize_phone("+7 (495) 123-45-67") == "+7 495 123-45-67"
    assert normalize_phone("84951234567") == "+7 495 123-45-67"
    assert normalize_phone("4951234567") == "+7 495 123-45-67"
    assert normalize_phone("123") == ""


def test_make_aliases_strips_generic_words_and_adds_domain_root():
    a = make_aliases("Стоматологическая клиника «Легенда»", "sk-legenda.ru")
    assert a == "легенда; sk-legenda"


def test_make_aliases_keeps_only_root_when_name_has_no_generic_words():
    assert make_aliases("Smile Symphony", "smilesymphony.ru") == "smilesymphony"


def test_group_by_domain_merges_chain_points_and_dedups():
    els = [
        {"type": "node", "id": 1, "tags": {"name": "ПрезиДЕНТ", "website": "https://prezi-dent.ru", "phone": "+7 495 111-11-11"}},
        {"type": "node", "id": 2, "tags": {"name": "ПрезиДЕНТ", "website": "http://www.prezi-dent.ru/", "phone": "+7 495 222-22-22"}},
        {"type": "node", "id": 2, "tags": {"name": "dup"}},
        {"type": "way", "id": 3, "tags": {"name": "Без сайта", "phone": "+7 495 333-33-33"}},
        {"type": "node", "id": 4, "tags": {"name": "Соцсеть", "website": "https://vk.com/x"}},
    ]
    g = group_by_domain(els)
    assert list(g) == ["prezi-dent.ru"]
    row = g["prezi-dent.ru"]
    assert row["points"] == "2"
    assert row["phone"] == "+7 495 111-11-11"
    assert row["phones_extra"] == "+7 495 222-22-22"
    assert row["name"] == "ПрезиДЕНТ"


def test_classify():
    assert classify("dry", "heuristics; phone=ok") == "ok"
    assert classify("skipped", "captcha on page (recaptcha)") == "captcha"
    assert classify("skipped", "phone mask failed: x") == "mask_fail"
    assert classify("skipped", "form not found") == "no_form"
    assert classify("error", "timeout") == "dead"


def test_pick_prefers_implant_then_bigger_chains_and_skips_promoted():
    cands = [
        {"domain": "a.ru", "status": "ok", "clinic_id": "", "implant": "0", "points": "5"},
        {"domain": "b.ru", "status": "ok", "clinic_id": "", "implant": "1", "points": "1"},
        {"domain": "c.ru", "status": "ok", "clinic_id": "77", "implant": "1", "points": "9"},
        {"domain": "d.ru", "status": "captcha", "clinic_id": "", "implant": "1", "points": "9"},
        {"domain": "e.ru", "status": "ok", "clinic_id": "", "implant": "1", "points": "3"},
    ]
    assert [c["domain"] for c in pick(cands, 10)] == ["e.ru", "b.ru", "a.ru"]
    assert [c["domain"] for c in pick(cands, 1)] == ["e.ru"]


def test_to_clinic_sets_priority_and_aliases():
    c = {"name": "Клиника Улыбка", "site": "ulybka.ru", "domain": "ulybka.ru", "phone": "+7 495 1", "phones_extra": "", "implant": "1"}
    row = to_clinic(c, 53)
    assert row["id"] == "53" and row["priority"] == "1"
    assert row["aliases"] == "улыбка; ulybka"


# ---- lpr ----
from lpr import find_emails, find_inn, find_ogrn, parse_people, pick_links, role_snippets, strip_html


def test_strip_html_and_inn_ogrn():
    h = "<html><script>var x=1;</script><div>ООО «Легенда» ИНН: 7701234567, ОГРН 1027700132195</div></html>"
    t = strip_html(h)
    assert "var x" not in t
    assert find_inn(t) == "7701234567"
    assert find_ogrn(t) == "1027700132195"
    assert find_inn("телефон 7701234567") == ""


def test_find_emails_filters_junk():
    t = "пишите info@clinic.ru или Director@Clinic.RU, картинка logo@2x.png, sentry@sentry.io"
    assert find_emails(t) == ["info@clinic.ru", "director@clinic.ru"]


def test_role_snippets_and_pick_links():
    t = "Наш главный врач Иванов Иван Иванович принимает по будням. Директор клиники — Петрова Анна."
    s = role_snippets(t)
    assert any("Иванов" in x for x in s) and any("Петрова" in x for x in s)
    h = '<a href="/about/">О нас</a><a href="https://other.ru/contacts">x</a><a href="/kontakty">Контакты</a><a href="/uslugi">Услуги</a>'
    assert pick_links("https://clinic.ru/", h) == ["https://clinic.ru/about/", "https://clinic.ru/kontakty"]


def test_parse_people():
    assert parse_people('вот: [{"name": "Иванов И.И.", "role": "главный врач"}, {"role": "x"}]') == [{"name": "Иванов И.И.", "role": "главный врач"}]
    assert parse_people("ничего") == []
    assert parse_people('[: [{"name": "Петрова Анна", "role": "директор"}]]') == [{"name": "Петрова Анна", "role": "директор"}]
    assert parse_people("Ответ: []") == []
    assert parse_people('[{"name": "Эдуарду Самвеловиче", "role": "директор"}, {"name": "Петрова Анна", "role": "директор"}]') == [{"name": "Петрова Анна", "role": "директор"}]


def test_regex_people():
    from lpr import regex_people
    t = ("Главный врач — Иванов Иван Иванович, стаж 20 лет. Генеральный директор: Петрова Анна Сергеевна. "
         "Директор клиники Москвы работает с 2010 года. Главный врач клиники Сидоров Пётр.")
    p = regex_people(t)
    assert {"name": "Иванов Иван Иванович", "role": "главный врач"} in p
    assert {"name": "Петрова Анна Сергеевна", "role": "генеральный директор"} in p
    assert {"name": "Сидоров Пётр", "role": "главный врач"} in p
    assert not any(x["name"].startswith("Москвы") for x in p)
    assert regex_people("Главный врач Врачи Стоматологи Ортопеды") == []


def test_find_legal_names():
    from lpr import find_legal_names
    t = 'Политика ООО «Ника» и ООО "Дента-Люкс". АО район Митино не юрлицо. ИП «Иванов»'
    assert find_legal_names(t) == ["ООО Ника", "ООО Дента-Люкс", "ИП Иванов"]


def test_people_filters_job_titles_and_gov_emails():
    from lpr import regex_people, parse_people, find_emails
    assert regex_people("Главный врач Терапевт Клиники Химки") == []
    assert parse_people('[{"name": "Терапевт Клиники", "role": "главный врач"}]') == []
    assert find_emails("sao@77.rospotrebnadzor.ru и info@clinic.ru") == ["info@clinic.ru"]
