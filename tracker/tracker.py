"""
Baltic MA Tracker
Downloads the human medicines registers of Lithuania, Latvia and Estonia,
detects newly authorised products, updates the dashboard data and
sends a weekly email digest (plus an alert if a source breaks).

Run:  python tracker/tracker.py            (normal daily run)
      python tracker/tracker.py --email    (force sending the digest now)
      python tracker/tracker.py --offline DIR  (use local files: lt.csv, lv.xml, ee.csv)
"""
import csv
import io
import json
import os
import re
import smtplib
import sys
import traceback
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

csv.field_size_limit(10**9)

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
DOCS_DIR = ROOT / "docs"
TZ = ZoneInfo("Europe/Vilnius")
TODAY = datetime.now(TZ).date()
WINDOW_DAYS = 7

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,lt;q=0.8",
}

LT_URL = "https://vapris.vvkt.lt/vvkt-web/public/medications/export"
LV_URL = "https://dati.zva.gov.lv/zalu-registrs/export/HumanProducts.xml"
LV_CKAN = "https://data.gov.lv/dati/api/3/action/package_show?id=latvijas-zalu-registrs"
EE_URL = "https://www.ravimiregister.ee/?pv=PublicDownloads"
EE_TARGET = "ctl10$packagesSpecialCsvDownload"  # "Laadi alla detailne CSV v2"

COUNTRY_NAMES = {"LT": "Lithuania", "LV": "Latvia", "EE": "Estonia"}


# ----------------------------------------------------------------- helpers
def clean(v):
    return re.sub(r"\s+", " ", (v or "").replace("\t", " ")).strip()


def parse_date(s):
    s = clean(s)
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d-%b-%y", "%d-%b-%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s, fmt).date()
        except ValueError:
            continue
    return None


def procedure_of(text, ma_no="", parallel=False):
    t = (text or "").lower()
    if parallel or "lygiagre" in t or "parallel" in t or "paralēl" in t:
        return "Parallel import"
    if (ma_no or "").upper().startswith("EU/"):
        return "Central (EU)"
    if "decentral" in t or "detsentraal" in t:
        return "DCP"
    if "centraliz" in t or "centralis" in t or "tsentraal" in t:
        return "Central (EU)"
    if "savitarp" in t or "savstarp" in t or "mutual" in t or "vastastik" in t:
        return "MRP"
    if "nacion" in t or "national" in t or "riiklik" in t:
        return "National"
    return clean(text) or "Non-central (not specified)"


# legal-form / filler words dropped when comparing company names
_DROP = {
    "uab", "ab", "ad", "as", "sia", "dd", "ou", "gmbh", "ag", "kg", "co", "sa", "spa", "srl", "bv", "nv",
    "ltd", "limited", "inc", "llc", "plc", "aps", "oy", "oyj", "spzoo", "doo", "kft", "zrt", "sl", "sas",
    "sarl", "ks", "cokg", "eood", "ood", "pharma", "pharmaceutical", "pharmaceuticals", "laboratories",
    "laboratorios", "laboratoires", "lab", "company", "corporation", "corp", "the", "and",
}
_COUNTRIES = (
    "germany|france|italy|spain|poland|lithuania|latvia|estonia|netherlands|the netherlands|ireland|"
    "denmark|sweden|finland|norway|austria|belgium|hungary|czech republic|czechia|slovenia|slovakia|"
    "croatia|romania|bulgaria|greece|cyprus|malta|portugal|luxembourg|iceland|switzerland|"
    "united kingdom|india|israel|usa|united states|japan|china|turkey|serbia"
)
_TRAIL_COUNTRY = re.compile(r",\s*(" + _COUNTRIES + r")\s*$", re.I)


def mah_key(name):
    s = clean(name)
    s = _TRAIL_COUNTRY.sub("", s)
    s = s.lower().replace("ö", "o").replace("ü", "u").replace("ä", "a").replace("&", " ")
    # join dotted abbreviations: "b.v." -> "bv", "sp. z o.o." -> "spzoo", "k.s." -> "ks"
    s = re.sub(r"\b([a-z])\.\s?(?=[a-z]\.)", r"\1", s)
    s = re.sub(r"sp\.?\s*z\s*o\.?\s*o\.?", " spzoo ", s)
    tokens = re.findall(r"[a-z0-9ąčęėįšųūžāēģīķļņō]+", s.replace(".", ""))
    kept = [t for t in tokens if t not in _DROP]
    return " ".join(kept or tokens)


def http_get(url, **kw):
    import requests
    r = requests.get(url, headers=UA, timeout=300, **kw)
    r.raise_for_status()
    return r


# ----------------------------------------------------------------- sources
def fetch_lt(offline=None):
    raw = Path(offline, "lt.csv").read_bytes() if offline else http_get(LT_URL).content
    text = raw.decode("utf-8-sig")
    rows = csv.DictReader(io.StringIO(text), delimiter=";")
    need = {"VID", "Registracijos data", "Registruotojas", "Preparato (sugalvotas) pavadinimas"}
    if not need.issubset(set(rows.fieldnames or [])):
        raise RuntimeError(f"LT columns changed: {rows.fieldnames}")
    products = {}
    for r in rows:
        if clean(r.get("Stadija")).lower().startswith("išregistr"):
            continue
        key = clean(r["VID"])
        if not key:
            continue
        pack_no = clean(r.get("(pakuotės) Pakuotės registracijos Nr."))
        ma_no = pack_no.rsplit("/", 1)[0] if pack_no.count("/") >= 2 else pack_no
        p = products.get(key)
        if p is None:
            basis = clean(r.get("Tiekimo LR rinkai pagrindas"))
            products[key] = {
                "key": key,
                "name": clean(r.get("Preparato (sugalvotas) pavadinimas")),
                "substance": clean(r.get("Veiklioji (-osios) medžiaga (-os)")),
                "strength": clean(r.get("Stiprumas")),
                "form": clean(r.get("Farmacinė forma")),
                "atc": clean(r.get("ATC kodas")),
                "mah": clean(r.get("Registruotojas")),
                "procedure": procedure_of(basis, ma_no),
                "ma_no": ma_no,
                "rx": clean(r.get("Recepto poreikis")),
                "auth_date": parse_date(r.get("Registracijos data")),
                "url": f"https://vapris.vvkt.lt/vvkt-web/public/medications/view/{key}",
            }
        else:
            d = parse_date(r.get("Registracijos data"))
            if d and (p["auth_date"] is None or d < p["auth_date"]):
                p["auth_date"] = d
    return products


def _lv_bytes():
    try:
        return http_get(LV_URL).content
    except Exception as first:
        # Fallback: the copy on the Latvian open data portal (zip or xml)
        meta = http_get(LV_CKAN).json()
        for res in meta["result"]["resources"]:
            u = res.get("url", "")
            if "HumanProducts" in u and u != LV_URL:
                data = http_get(u).content
                if u.endswith(".zip"):
                    import zipfile
                    with zipfile.ZipFile(io.BytesIO(data)) as z:
                        name = [n for n in z.namelist() if n.endswith(".xml")][0]
                        return z.read(name)
                return data
        raise first


def fetch_lv(offline=None):
    raw = Path(offline, "lv.xml").read_bytes() if offline else _lv_bytes()
    products = {}
    seen_any = False
    for _, el in ET.iterparse(io.BytesIO(raw), events=("end",)):
        if el.tag != "product":
            continue
        seen_any = True
        g = lambda t: clean(el.findtext(t))
        key = g("authorisation_no")
        removed = g("prd_removed") == "1" or bool(g("remove_date"))
        if key and not removed:
            p = products.get(key)
            if p is None:
                parallel = bool(g("parallel_importer") or g("parallel_distributor"))
                products[key] = {
                    "key": key,
                    "name": g("short_name") or g("medicine_name"),
                    "substance": g("active_substance"),
                    "strength": g("strength"),
                    "form": g("pharmaceutical_form"),
                    "atc": g("atc_code"),
                    "mah": g("marketing_authorisation_holder"),
                    "procedure": procedure_of(g("authorisation_procedure"), key, parallel),
                    "ma_no": key,
                    "rx": g("legal_status"),
                    "auth_date": parse_date(g("date_of_authorisation")),
                    "url": g("summary_of_product_characteristics") or "https://dati.zva.gov.lv/zalu-registrs/lv",
                }
        el.clear()
    if not seen_any:
        raise RuntimeError("LV XML has no <product> elements – format changed?")
    return products


EE_PAGES = [
    "https://www.ravimiregister.ee/?pv=PublicDownloads",
    "https://www.ravimiregister.ee/en/publichomepage.aspx?pv=PublicDownloads",
    "https://ravimiregister.ee/publichomepage.aspx?pv=PublicDownloads",
]


def _ee_once(page_url):
    import requests
    from html import unescape
    s = requests.Session()
    s.headers.update(UA)
    s.headers.update({"Accept-Language": "et-EE,et;q=0.9,en;q=0.8", "Upgrade-Insecure-Requests": "1"})
    s.get("https://www.ravimiregister.ee/", timeout=60)  # pick up cookies like a browser
    page = s.get(page_url, timeout=120, headers={"Referer": "https://www.ravimiregister.ee/"})
    page.raise_for_status()
    form = {}
    for m in re.finditer(r"<input[^>]+>", page.text, re.I):
        tag = m.group(0)
        typ = re.search(r'type="([^"]*)"', tag, re.I)
        if typ and typ.group(1).lower() != "hidden":
            continue
        name = re.search(r'name="([^"]*)"', tag, re.I)
        val = re.search(r'value="([^"]*)"', tag, re.I)
        if name:
            form[unescape(name.group(1))] = unescape(val.group(1)) if val else ""
    if "__VIEWSTATE" not in form:
        raise RuntimeError("download page changed (no __VIEWSTATE)")
    form["__EVENTTARGET"] = EE_TARGET
    form["__EVENTARGUMENT"] = ""
    r = s.post(page.url, data=form, timeout=600,
               headers={"Referer": page.url, "Origin": "https://www.ravimiregister.ee"})
    r.raise_for_status()
    if r.content[:200].lstrip().lower().startswith((b"<!doctype", b"<html")):
        raise RuntimeError("returned an HTML page instead of the CSV")
    return r.content


def _ee_bytes():
    """The Estonian site sometimes blocks cloud servers (HTTP 403). Retry a few
    times over ~8 minutes, alternating between equivalent page addresses."""
    import time
    waits = [0, 30, 90, 180, 180]
    last = None
    for i, w in enumerate(waits):
        time.sleep(w)
        url = EE_PAGES[i % len(EE_PAGES)]
        try:
            return _ee_once(url)
        except Exception as e:
            last = e
            print(f"EE attempt {i + 1} failed ({url}): {e}")
    raise last


def fetch_ee(offline=None):
    raw = Path(offline, "ee.csv").read_bytes() if offline else _ee_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    rows = csv.DictReader(io.StringIO(text), delimiter=";")
    need = {"Müügiloa number", "Müügiloa hoidja", "Pakendi nimetus", "Müügiloa kehtivuse algus"}
    if not need.issubset(set(rows.fieldnames or [])):
        raise RuntimeError(f"EE columns changed: {rows.fieldnames}")
    products = {}
    for r in rows:
        if "INIMESTEL" not in clean(r.get("Ravimi liik")).upper():
            continue  # human medicines only
        kind = clean(r.get("Pakendi liik")).lower()
        if "lõppenud" in kind or "müügiloata" in kind:
            continue
        end = parse_date(r.get("Müügiloa kehtivuse lõpp"))
        if end and end < TODAY:
            continue
        key = clean(r.get("Müügiloa number"))
        if not key:
            continue
        p = products.get(key)
        if p is None:
            parallel = bool(clean(r.get("Päritolumaa")))
            spc = clean(r.get("SPCLinkEst"))
            products[key] = {
                "key": key,
                "name": clean(r.get("Pakendi nimetus")),
                "substance": clean(r.get("Toimeaine nimetus")),
                "strength": clean(r.get("Tugevus")),
                "form": clean(r.get("Ravimvorm")),
                "atc": clean(r.get("ATC kood")),
                "mah": clean(r.get("Müügiloa hoidja")),
                "procedure": procedure_of("", key, parallel),
                "ma_no": key,
                "rx": clean(r.get("Retseptinõude liik")),
                "auth_date": parse_date(r.get("Müügiloa kehtivuse algus")),
                "url": f"https://www.ravimiregister.ee/Data/SPC/{spc}" if spc.endswith(".pdf") else "https://www.ravimiregister.ee/",
            }
        else:
            d = parse_date(r.get("Müügiloa kehtivuse algus"))
            if d and (p["auth_date"] is None or d < p["auth_date"]):
                p["auth_date"] = d
    return products


SOURCES = {"LT": fetch_lt, "LV": fetch_lv, "EE": fetch_ee}


# ----------------------------------------------------------------- state
def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data, compact=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    else:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


# ----------------------------------------------------------------- email
def build_email(new_items, failures, dash_url, start, end):
    css_th = "text-align:left;padding:6px 8px;border-bottom:2px solid #ccc;font-size:12px;color:#555"
    css_td = "padding:6px 8px;border-bottom:1px solid #eee;font-size:13px;vertical-align:top"

    def table(items, show_country=False):
        cols = (["Country"] if show_country else []) + ["Product", "Active substance", "Strength / form",
                                                         "ATC", "MAH", "Procedure", "Authorised"]
        h = "<table style='border-collapse:collapse;width:100%'><tr>" + "".join(
            f"<th style='{css_th}'>{c}</th>" for c in cols) + "</tr>"
        for it in items:
            cells = ([it["country_list"]] if show_country else []) + [
                f"<a href='{escape(it['url'])}'>{escape(it['name'])}</a>",
                escape(it["substance"]), escape(" · ".join(x for x in (it["strength"], it["form"]) if x)),
                escape(it["atc"]), escape(it["mah"]), escape(it["procedure"]),
                escape(it["auth_date"] or "—") + ("" if it["by_date"] else " <span style='color:#a60'>(newly listed)</span>"),
            ]
            h += "<tr>" + "".join(f"<td style='{css_td}'>{c}</td>" for c in cells) + "</tr>"
        return h + "</table>"

    central = [i for i in new_items if i["procedure"] == "Central (EU)"]
    national = [i for i in new_items if i["procedure"] != "Central (EU)"]

    # merge central products listed in several countries into one line
    merged = {}
    for i in central:
        k = (re.sub(r"[^a-z0-9]", "", i["name"].lower()), i["strength"].lower())
        if k in merged:
            merged[k]["countries"].add(i["country"])
        else:
            merged[k] = dict(i, countries={i["country"]})
    central_rows = []
    for v in merged.values():
        v["country_list"] = ", ".join(sorted(v["countries"]))
        central_rows.append(v)
    central_rows.sort(key=lambda x: (x["name"].lower(), x["strength"]))

    body = (f"<div style='font-family:Arial,Helvetica,sans-serif;max-width:1100px'>"
            f"<h2 style='margin:0 0 4px'>New medicine authorisations — Baltics</h2>"
            f"<p style='color:#555;margin:0 0 16px'>{start:%d %b} – {end:%d %b %Y}. "
            f"<a href='{dash_url}'>Open the dashboard</a> to filter all registers.</p>")
    if failures:
        body += ("<div style='background:#fdecea;border:1px solid #f5c2c0;padding:10px;margin-bottom:16px'>"
                 "<b>Source problem:</b> " + "; ".join(escape(f) for f in failures) +
                 ". Results for that country may be incomplete.</div>")
    counts = Counter(i["country"] for i in national)
    summary = ", ".join(f"{COUNTRY_NAMES[c]} {counts.get(c, 0)}" for c in ("LT", "LV", "EE"))
    body += f"<p><b>{len(national)}</b> national / DCP / MRP / parallel-import products ({summary}) and <b>{len(central_rows)}</b> EU centrally authorised products.</p>"
    for c in ("LT", "LV", "EE"):
        items = sorted([i for i in national if i["country"] == c], key=lambda x: (x["name"].lower(), x["strength"]))
        body += f"<h3 style='margin:20px 0 6px'>{COUNTRY_NAMES[c]} ({len(items)})</h3>"
        body += table(items) if items else "<p style='color:#777'>Nothing new.</p>"
    body += f"<h3 style='margin:24px 0 6px'>EU centrally authorised ({len(central_rows)})</h3>"
    body += table(central_rows, show_country=True) if central_rows else "<p style='color:#777'>Nothing new.</p>"
    body += ("<p style='color:#999;font-size:11px;margin-top:24px'>'Newly listed' = appeared in the register this week "
             "although its authorisation date is older (e.g. late entry). Sources: VVKT, ZVA, Ravimiamet.</p></div>")
    return body


def send_email(subject, html):
    user, pwd = os.environ.get("EMAIL_USER"), os.environ.get("EMAIL_PASSWORD")
    to = os.environ.get("EMAIL_TO") or user
    if not (user and pwd):
        print("EMAIL_USER/EMAIL_PASSWORD not set – email not sent. Preview saved to state/last_email.html")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd.replace(" ", ""))
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    print(f"Email sent to {to}")


def _failed_days(meta, country_name):
    code = {v: k for k, v in COUNTRY_NAMES.items()}.get(country_name, country_name)
    return meta.get(code, {}).get("fail_streak", 0)


# ----------------------------------------------------------------- main
def main():
    args = sys.argv[1:]
    force_email = "--email" in args
    offline = args[args.index("--offline") + 1] if "--offline" in args else None

    seen = load_json(STATE_DIR / "seen.json", {})
    meta = load_json(STATE_DIR / "meta.json", {})
    old_data = load_json(DOCS_DIR / "data.json", {})
    old_rows = defaultdict(list)
    for row in old_data.get("rows", []):
        old_rows[row[0]].append(row)

    today_s = TODAY.isoformat()
    rows, failures = [], []

    for c, fn in SOURCES.items():
        try:
            prods = fn(offline)
            prev = meta.get(c, {}).get("count")
            if not prods:
                raise RuntimeError("0 products parsed")
            if prev and len(prods) < 0.7 * prev:
                raise RuntimeError(f"only {len(prods)} products vs {prev} last time – download looks incomplete")
        except Exception as e:
            traceback.print_exc()
            failures.append(f"{COUNTRY_NAMES[c]}: {str(e)[:200]}")
            rows.extend(old_rows.get(c, []))  # keep yesterday's data on the dashboard
            m = meta.setdefault(c, {})
            if m.get("last_fail_day") != today_s:
                m["fail_streak"] = m.get("fail_streak", 0) + 1
                m["last_fail_day"] = today_s
            m["last_error"] = f"{today_s}: {str(e)[:300]}"
            continue

        baseline = c not in seen
        cs = seen.setdefault(c, {})
        for k, p in prods.items():
            if k not in cs:
                cs[k] = "baseline" if baseline else today_s
            fs = cs[k]
            rows.append([c, k, p["name"], p["substance"], p["strength"], p["form"], p["atc"], p["mah"],
                         mah_key(p["mah"]), p["procedure"],
                         p["auth_date"].isoformat() if p["auth_date"] else "",
                         "" if fs == "baseline" else fs, p["rx"], p["url"]])
        meta[c] = {"count": len(prods), "last_ok": today_s, "fail_streak": 0,
                   "baseline_date": meta.get(c, {}).get("baseline_date", today_s)}
        print(f"{c}: {len(prods)} products ({'baseline' if baseline else 'compared'})")

    # canonical display name per MAH key (most common spelling)
    spell = defaultdict(Counter)
    for r in rows:
        spell[r[8]][r[7]] += 1
    canon = {k: v.most_common(1)[0][0] for k, v in spell.items()}

    cols = ["country", "key", "name", "substance", "strength", "form", "atc", "mah", "mah_key",
            "procedure", "auth_date", "first_seen", "rx", "url"]
    save_json(DOCS_DIR / "data.json", {
        "updated": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        "baselines": {c: meta.get(c, {}).get("baseline_date") for c in SOURCES},
        "failures": failures, "columns": cols, "mah_names": canon, "rows": rows}, compact=True)
    save_json(STATE_DIR / "seen.json", seen)
    save_json(STATE_DIR / "meta.json", meta)

    # weekly digest (Mondays, or when forced)
    start = TODAY - timedelta(days=WINDOW_DAYS)
    repo = os.environ.get("GITHUB_REPOSITORY", "user/baltic-ma-tracker")
    owner, name = repo.split("/", 1)
    dash_url = f"https://{owner.lower()}.github.io/{name}/"
    now_h = datetime.now(TZ).hour
    digest_due = TODAY.weekday() == 0 and meta.get("_last_digest") != today_s
    if digest_due and failures and now_h < 12:
        digest_due = False  # a source failed this morning: wait for the midday retry run
        print("Source failed - digest postponed to the midday run")
    if digest_due or force_email:
        new_items = []
        for r in rows:
            d = dict(zip(cols, r))
            ad = parse_date(d["auth_date"])
            fs = parse_date(d["first_seen"])
            by_date = bool(ad and start < ad <= TODAY)
            by_seen = bool(fs and start < fs <= TODAY)
            if by_date or by_seen:
                d["by_date"] = by_date or not by_seen
                d["mah"] = canon.get(d["mah_key"], d["mah"])
                new_items.append(d)
        html = build_email(new_items, failures, dash_url, start + timedelta(days=1), TODAY)
        (STATE_DIR / "last_email.html").write_text(html, encoding="utf-8")
        send_email(f"Baltic new authorisations: {len(new_items)} products ({TODAY:%d %b})", html)
        if not force_email:
            meta["_last_digest"] = today_s
            save_json(STATE_DIR / "meta.json", meta)
    elif failures and all(_failed_days(meta, c.split(":")[0]) >= 2 for c in failures):
        html = ("<p>The daily register update had a problem:</p><ul>" +
                "".join(f"<li>{escape(f)}</li>" for f in failures) +
                f"</ul><p>The dashboard keeps the last good data for that country. "
                f"It has failed two days in a row, so the source may have changed or be blocking the server.</p><p><a href='{dash_url}'>Dashboard</a></p>")
        last_alert = meta.get("_last_alert")
        if last_alert != today_s:
            send_email("Baltic tracker: source problem", html)
            meta["_last_alert"] = today_s
            save_json(STATE_DIR / "meta.json", meta)


if __name__ == "__main__":
    main()
