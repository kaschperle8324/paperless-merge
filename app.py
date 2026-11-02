#!/usr/bin/env python3
"""
Paperless-ngx Erweiterung: Rechnungen + zugehörige Lieferscheine zu einem PDF zusammenführen.

Ablauf:
  1. Zeitraum eingeben (Rechnungsdatum von/bis)
  2. Tool sucht Rechnungen + Lieferscheine und schlägt Zuordnungen vor
  3. Prüfseite: Zuordnungen kontrollieren, an-/abwählen, manuell ergänzen
  4. Erst nach Bestätigung wird in Paperless zusammengeführt
     (Rechnung = Seite 1, danach Lieferscheine nach Datum; Metadaten/Dokumententyp
      kommen von der Rechnung -> neues Dokument ist Typ "Rechnung")
"""
import datetime as dt
import os
import re
import threading
import time
import uuid
from functools import wraps

import requests
from flask import Flask, Response, abort, redirect, render_template_string, request, url_for

# --------------------------------------------------------------------------- Konfiguration
PAPERLESS_URL = os.environ.get("PAPERLESS_URL", "http://paperless:8000").rstrip("/")
PAPERLESS_PUBLIC_URL = os.environ.get("PAPERLESS_PUBLIC_URL", "").rstrip("/")  # optional
TOKEN = os.environ.get("PAPERLESS_TOKEN", "")
TYPE_INVOICE = os.environ.get("DOCTYPE_INVOICE", "Rechnung")
TYPE_DELIVERY = os.environ.get("DOCTYPE_DELIVERY", "Lieferschein")
DONE_TAG = os.environ.get("DONE_TAG", "LS-zugeordnet")
DEFAULT_TAGS = [t.strip().lower() for t in os.environ.get("DEFAULT_TAGS", "Posteingang").split(",") if t.strip()]
FALLBACK_DAYS = int(os.environ.get("FALLBACK_DAYS", "45"))
DEFAULT_LOOKBACK = int(os.environ.get("DEFAULT_LOOKBACK_DAYS", "60"))
VERIFY_SSL = os.environ.get("VERIFY_SSL", "true").lower() not in ("0", "false", "no")
APP_USER = os.environ.get("APP_USER")
APP_PASSWORD = os.environ.get("APP_PASSWORD")
AUTO_CLEAN_TITLES = os.environ.get("AUTO_CLEAN_TITLES", "true").lower() not in ("0", "false", "no")
PLAN_TTL = 3600  # Sekunden, wie lange ein Vorschlag gültig bleibt


def _read_version():
    for p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"), "/app/VERSION"):
        try:
            with open(p, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            continue
    return "dev"


VERSION = _read_version()

app = Flask(__name__)
app.jinja_env.filters["de"] = lambda d: f"{d[8:10]}.{d[5:7]}.{d[0:4]}" if d and len(d) >= 10 else d
PLANS = {}
PLANS_LOCK = threading.Lock()


# --------------------------------------------------------------------------- Paperless-API
class Paperless:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Token {TOKEN}", "Accept": "application/json"})
        self.s.verify = VERIFY_SSL

    def get(self, path, **params):
        r = self.s.get(PAPERLESS_URL + path, params=params, timeout=60)
        r.raise_for_status()
        return r

    def send(self, method, path, payload):
        r = self.s.request(method, PAPERLESS_URL + path, json=payload, timeout=120)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path}: HTTP {r.status_code} – {r.text[:300]}")
        return r.json() if r.content else {}

    def all(self, path, **params):
        params.setdefault("page_size", 100)
        page, out = 1, []
        while True:
            data = self.get(path, page=page, **params).json()
            out.extend(data.get("results", []))
            if not data.get("next"):
                return out
            page += 1

    def id_by_name(self, path, name):
        res = self.all(path, name__iexact=name)
        return res[0]["id"] if res else None

    def ensure_tag(self, name):
        tid = self.id_by_name("/api/tags/", name)
        if tid:
            return tid
        return self.send("POST", "/api/tags/", {"name": name, "matching_algorithm": 0})["id"]

    def documents(self, type_id, start, end, exclude_tag=None, corr_ids=None, corr_none=False):
        params = {
            "document_type__id": type_id,
            "created__gte": start.isoformat(),
            "created__lt": (end + dt.timedelta(days=1)).isoformat(),
            "ordering": "created",
            "fields": "id,title,correspondent,created,content,tags,archive_serial_number",
        }
        if exclude_tag:
            params["tags__id__none"] = exclude_tag
        if corr_none:
            params["correspondent__isnull"] = "true"
            docs = self.all("/api/documents/", **params)
            return [d for d in docs if d.get("correspondent") is None]
        if corr_ids:
            params["correspondent__id__in"] = ",".join(map(str, sorted(corr_ids)))
            docs = self.all("/api/documents/", **params)
            return [d for d in docs if d.get("correspondent") in corr_ids]
        return self.all("/api/documents/", **params)

    def correspondent_list(self):
        return sorted(self.all("/api/correspondents/"), key=lambda c: c["name"].lower())

    def correspondents(self):
        return {c["id"]: c["name"] for c in self.all("/api/correspondents/")}

    def merge(self, doc_ids, metadata_id, delete_originals):
        return self.send("POST", "/api/documents/bulk_edit/", {
            "documents": doc_ids,
            "method": "merge",
            "parameters": {"metadata_document_id": metadata_id, "delete_originals": delete_originals},
        })

    def tags(self):
        return sorted(self.all("/api/tags/"), key=lambda t: t["name"].lower())

    def modify_tags(self, doc_ids, add=(), remove=()):
        return self.send("POST", "/api/documents/bulk_edit/", {
            "documents": doc_ids, "method": "modify_tags",
            "parameters": {"add_tags": list(add), "remove_tags": list(remove)},
        })

    def add_tag(self, doc_ids, tag_id):
        return self.send("POST", "/api/documents/bulk_edit/", {
            "documents": doc_ids, "method": "add_tag", "parameters": {"tag": tag_id},
        })


# --------------------------------------------------------------------------- Titel "(merged)"
MERGED_RE = re.compile(r"\s*\(merged\)", re.I)


def clean_title(title):
    """Entfernt jedes "(merged)" aus einem Titel – auch mehrfach hintereinander."""
    new = re.sub(r"\s{2,}", " ", MERGED_RE.sub("", title or "")).strip()
    return new or title


def clean_merged_titles(pl):
    """Bereinigt alle Dokumente, deren Titel "(merged)" enthält. Rückgabe: (gefunden, bereinigt, Fehler)."""
    docs = pl.all("/api/documents/", title__icontains="(merged)", fields="id,title")
    fixed, errors = 0, []
    for d in docs:
        new = clean_title(d["title"])
        if new == d["title"]:
            continue
        try:
            pl.send("PATCH", f"/api/documents/{d['id']}/", {"title": new})
            fixed += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{d['title']}: {e}")
    return len(docs), fixed, errors


def _auto_clean_titles(expected, timeout=300, interval=15):
    """Läuft im Hintergrund, bis Paperless die neuen Dokumente angelegt hat, und bereinigt deren Titel."""
    deadline, cleaned = time.time() + timeout, 0
    while time.time() < deadline and cleaned < expected:
        time.sleep(interval)
        try:
            cleaned += clean_merged_titles(Paperless())[1]
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- Zuordnungslogik
KEYWORDS = (r"(?:lieferscheine?|liefer\s*-?\s*schein|liefer-?nr|ls-?nr|lief\.?\s*-?\s*nr"
            r"|delivery\s*note|lieferung|belegs?)")
NUM_RE = re.compile(
    KEYWORDS + r"(?:\s*-?\s*(?:nr|nummer|no|number|#))?\.?\s*[:#.]?\s*(?=[A-Z\-/]{0,6}\d)([A-Z0-9][A-Z0-9\-/]{2,24})",
    re.I,
)
DATE_LIKE = re.compile(r"^\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}$")
LONG_TOKEN = re.compile(r"(?<![A-Z0-9])([A-Z0-9\-/]*\d{5,}[A-Z0-9\-/]*)(?![A-Z0-9])", re.I)


def _clean_number(n):
    n = n.upper().strip("-/")
    if sum(c.isdigit() for c in n) < 4 or DATE_LIKE.match(n):
        return None
    return n


def extract_numbers(text):
    """Nummern, die hinter 'Lieferschein', 'LS-Nr.' usw. stehen."""
    out = set()
    for m in NUM_RE.finditer(text or ""):
        n = _clean_number(m.group(1))
        if n:
            out.add(n)
    return out


def delivery_numbers(doc):
    nums = extract_numbers(doc["content"]) | extract_numbers(doc["title"])
    if not nums:  # Fallback: lange Ziffernfolgen im Titel, z.B. "LS 80012345"
        for m in LONG_TOKEN.finditer(doc["title"] or ""):
            n = _clean_number(m.group(1))
            if n:
                nums.add(n)
    return nums


# typische OCR-Verwechslungen: L/I/| -> 1, O/Q -> 0, S -> 5, B -> 8, Z -> 2
OCR_MAP = str.maketrans("LI|!OQSBZ", "111100582")


def ocr_norm(text):
    """Text so vereinheitlichen, dass z. B. '5LI12345' und '51112345' gleich aussehen."""
    return (text or "").upper().translate(OCR_MAP)


def contains_number(haystack_upper, num):
    variants = {num}
    stripped = num.lstrip("0")
    if len(stripped) >= 5:
        variants.add(stripped)
    for v in variants:
        if re.search(r"(?<![A-Z0-9])0*" + re.escape(v) + r"(?![A-Z0-9])", haystack_upper):
            return True
    return False


def _date(doc):
    return dt.date.fromisoformat(str(doc["created"])[:10])


def summarize(doc, corr):
    return {
        "id": doc["id"],
        "title": doc.get("title") or f"Dokument {doc['id']}",
        "date": _date(doc).isoformat(),
        "correspondent": corr.get(doc.get("correspondent"), "–"),
        "asn": doc.get("archive_serial_number"),
        "tags": doc.get("tags") or [],
    }


def level_of(score):
    if score >= 90:
        return "sicher"
    if score >= 50:
        return "mittel"
    return "unsicher"


def build_plan(invoices, deliveries, corr, inv_start):
    """Erzeugt Zuordnungsvorschläge. Jeder Lieferschein wird höchstens einer Rechnung zugeordnet."""
    inv_text = {i["id"]: f"{i.get('title', '')}\n{i.get('content', '')}".upper() for i in invoices}
    inv_refs = {i["id"]: extract_numbers(i.get("content")) for i in invoices}
    ls_text = {d["id"]: f"{d.get('title', '')}\n{d.get('content', '')}".upper() for d in deliveries}
    ls_nums = {d["id"]: delivery_numbers(d) for d in deliveries}
    inv_fz = {k: v.translate(OCR_MAP) for k, v in inv_text.items()}
    ls_fz = {k: v.translate(OCR_MAP) for k, v in ls_text.items()}

    candidates = []
    for inv in invoices:
        idate, icorr = _date(inv), inv.get("correspondent")
        for ls in deliveries:
            ldate, lcorr = _date(ls), ls.get("correspondent")
            same_corr = icorr is not None and icorr == lcorr
            diff_corr = icorr is not None and lcorr is not None and icorr != lcorr
            score, reason = 0, ""

            for n in sorted(ls_nums[ls["id"]]):
                short = sum(c.isdigit() for c in n) < 5
                if short and not same_corr:
                    continue
                if contains_number(inv_text[inv["id"]], n):
                    score, reason = 100, f"Lieferschein-Nr. {n} steht in der Rechnung"
                    break
            if not score:
                for n in sorted(inv_refs[inv["id"]]):
                    short = sum(c.isdigit() for c in n) < 5
                    if short and not same_corr:
                        continue
                    if contains_number(ls_text[ls["id"]], n):
                        score, reason = 95, f"Rechnung nennt Lieferschein {n}, im Lieferschein gefunden"
                        break
            if not score:  # 2. Durchgang: OCR-tolerant (LI <-> 11, O <-> 0 ...)
                for n in sorted(ls_nums[ls["id"]]):
                    if sum(c.isdigit() for c in n) < 5 and not same_corr:
                        continue
                    if contains_number(inv_fz[inv["id"]], ocr_norm(n)):
                        score, reason = 90, f"Lieferschein-Nr. {n} steht in der Rechnung (OCR-tolerant verglichen)"
                        break
            if not score:
                for n in sorted(inv_refs[inv["id"]]):
                    if sum(c.isdigit() for c in n) < 5 and not same_corr:
                        continue
                    if contains_number(ls_fz[ls["id"]], ocr_norm(n)):
                        score, reason = 90, f"Rechnung nennt Lieferschein {n}, im Lieferschein gefunden (OCR-tolerant)"
                        break
            if score and diff_corr:
                score -= 40
                reason += " – ACHTUNG: anderer Korrespondent"
            if not score and same_corr and \
                    idate - dt.timedelta(days=FALLBACK_DAYS) <= ldate <= idate + dt.timedelta(days=3):
                score, reason = 30, "nur gleicher Korrespondent + passendes Datum (keine Nummer gefunden)"
            if score:
                candidates.append((score, abs((idate - ldate).days), inv["id"], ls["id"], reason))

    candidates.sort(key=lambda c: (-c[0], c[1]))
    used_ls, matches = set(), {}
    for score, _, inv_id, ls_id, reason in candidates:
        if ls_id in used_ls:
            continue
        used_ls.add(ls_id)
        matches.setdefault(inv_id, []).append((ls_id, score, reason))

    ls_by_id = {d["id"]: d for d in deliveries}
    groups, without = [], []
    for inv in invoices:
        if _date(inv) < inv_start:
            continue
        items = []
        for ls_id, score, reason in matches.get(inv["id"], []):
            lvl = level_of(score)
            items.append({**summarize(ls_by_id[ls_id], corr), "score": score, "reason": reason,
                          "level": lvl, "checked": lvl != "unsicher"})
        items.sort(key=lambda x: x["date"])
        entry = {"inv": summarize(inv, corr), "items": items,
                 "checked": any(i["checked"] for i in items)}
        (groups if items else without).append(entry)

    unassigned = [summarize(d, corr) for d in deliveries if d["id"] not in used_ls]
    return {"groups": groups, "without": without, "unassigned": unassigned}


# --------------------------------------------------------------------------- Web-Oberfläche
def require_auth(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if APP_USER and APP_PASSWORD:
            auth = request.authorization
            if not auth or auth.username != APP_USER or auth.password != APP_PASSWORD:
                return Response("Anmeldung erforderlich", 401,
                                {"WWW-Authenticate": 'Basic realm="Paperless Merge"'})
        return f(*a, **kw)
    return wrapper


BASE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Rechnung + Lieferschein</title><style>
:root{
  --bg:#f5f6fa;--surface:#fff;--surface2:#f0f2f8;--text:#111827;--muted:#6b7280;--line:#e5e7eb;
  --accent:#6366f1;--accent2:#8b5cf6;--accent-soft:#eef2ff;--ring:#6366f155;
  --ok-bg:#dcfce7;--ok:#15803d;--warn-bg:#ffedd5;--warn:#c2410c;--bad-bg:#fee2e2;--bad:#b91c1c;
  --shadow:0 1px 2px #0000000d,0 4px 16px #0000000a;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0b0f1a;--surface:#141a29;--surface2:#1b2234;--text:#e5e7eb;--muted:#94a3b8;--line:#263045;
  --accent:#818cf8;--accent2:#a78bfa;--accent-soft:#1e2340;--ring:#818cf855;
  --ok-bg:#052e1a;--ok:#4ade80;--warn-bg:#3b1d06;--warn:#fb923c;--bad-bg:#3a0d0d;--bad:#f87171;
  --shadow:0 1px 2px #0006,0 6px 20px #0004;
}}
*{box-sizing:border-box}
body{font-family:"Inter",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:0;background:var(--bg);color:var(--text);line-height:1.5}
header{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--surface) 85%,transparent);backdrop-filter:blur(10px);
  border-bottom:1px solid var(--line);padding:14px 24px;display:flex;align-items:center;gap:12px}
.logo{width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,var(--accent),var(--accent2));
  display:grid;place-items:center;color:#fff;font-weight:700;font-size:15px}
header h1{font-size:1.05rem;margin:0;font-weight:650}
header small{color:var(--muted);display:block;font-size:.8rem;font-weight:400}
main{margin:24px 0 40px;padding:0 clamp(12px,2vw,32px)}
h3{margin:0 0 12px;font-size:1.05rem;font-weight:650}
.card{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:20px;margin-bottom:16px;box-shadow:var(--shadow)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(420px,100%),1fr));gap:28px}
.cols>section{min-width:0}
.groups{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(620px,100%),1fr));gap:16px;align-items:start}
.groups>.card{margin-bottom:0}
.chip.plain span{--c:var(--accent);--tc:#fff}
.chip .cnt{font-style:normal;opacity:.6;font-size:.78rem;margin-left:6px}
.linkbtn{background:none;border:0;box-shadow:none;color:var(--accent);padding:0;font-size:.85rem;font-weight:600;cursor:pointer}
label.f{display:block;margin:0 0 6px;font-size:.85rem;font-weight:600;color:var(--muted)}
input[type=date],input[type=number],input[type=search],select{width:100%;padding:10px 12px;font:inherit;color:var(--text);
  background:var(--surface2);border:1px solid var(--line);border-radius:10px;outline:none}
input:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--ring)}
.switch{display:flex;gap:10px;align-items:center;margin-top:14px;cursor:pointer;font-weight:500}
.switch input{width:18px;height:18px;accent-color:var(--accent)}
button,.btn{display:inline-flex;align-items:center;gap:8px;background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border:0;
  padding:11px 20px;border-radius:12px;font:inherit;font-weight:600;cursor:pointer;text-decoration:none;box-shadow:0 4px 14px var(--ring)}
button:hover,.btn:hover{filter:brightness(1.08)}
button.sec,.btn.sec{background:var(--surface2);color:var(--text);box-shadow:none;border:1px solid var(--line)}
.meta{color:var(--muted);font-size:.88rem}
.grp-head{display:flex;gap:14px;align-items:center}
.items{margin-top:12px;border-top:1px solid var(--line)}
.row{display:flex;gap:14px;align-items:center;padding:12px 0 12px 32px;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}
input[type=checkbox]{width:18px;height:18px;accent-color:var(--accent);flex:none}
img.th{width:64px;height:86px;object-fit:cover;border-radius:8px;border:1px solid var(--line);background:var(--surface2);flex:none;transition:transform .15s}
img.th:hover{transform:scale(1.05)}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:.75rem;font-weight:600;letter-spacing:.02em}
.sicher{background:var(--ok-bg);color:var(--ok)}.mittel{background:var(--warn-bg);color:var(--warn)}.unsicher{background:var(--bad-bg);color:var(--bad)}
.tag{display:inline-block;background:var(--accent-soft);color:var(--accent);padding:2px 10px;border-radius:999px;font-size:.75rem;font-weight:600}
.add{margin:10px 0 0 32px;width:calc(100% - 32px)}
.err{border-color:var(--bad);background:var(--bad-bg);color:var(--bad)}
.ok{border-color:var(--ok);background:var(--ok-bg);color:var(--ok)}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px;max-height:220px;overflow:auto;padding:2px}
.chip input{display:none}
.chip span{display:inline-block;padding:6px 14px;border-radius:999px;border:1px solid var(--line);background:var(--surface2);
  cursor:pointer;font-size:.88rem;font-weight:500;transition:all .12s}
.chip span::before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--c);margin-right:8px;vertical-align:1px}
.chip input:checked+span{background:var(--c);color:var(--tc);border-color:var(--c);box-shadow:0 2px 8px #0002}
.chip input:checked+span::before{background:var(--tc)}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-size:.78rem;font-weight:600;margin-right:4px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}
.stat{background:var(--surface2);border:1px solid var(--line);border-radius:12px;padding:8px 14px}
.stat b{font-size:1.2rem;display:block}
details.card summary{cursor:pointer;font-weight:600}
details .groups{margin-top:14px}
.bar{position:sticky;bottom:14px;display:flex;gap:10px;align-items:center;justify-content:space-between;
  background:color-mix(in srgb,var(--surface) 90%,transparent);backdrop-filter:blur(10px)}
a{color:var(--accent)}
.ver{margin-left:auto;padding:4px 12px;border-radius:999px;background:var(--accent-soft);color:var(--accent);font-size:.8rem;font-weight:600}
ul{padding-left:18px}
</style></head><body>
<header><div class="logo">R+L</div><div><h1>Rechnung + Lieferschein</h1><small>Paperless-ngx Zusammenführung</small></div><span class="ver">v{{ version }}</span></header>
<main>
{% block body %}{% endblock %}</main></body></html>"""


def page(body, **ctx):
    ctx.setdefault("version", VERSION)
    return render_template_string(BASE.replace("{% block body %}{% endblock %}", body), **ctx)


INDEX = """
{% if error %}<div class="card err">{{ error }}</div>{% endif %}
<form class="card" method="post" action="{{ url_for('preview') }}">
 <div class="cols">
  <section>
   <h3>1 · Zeitraum &amp; Optionen</h3>
   <div class="grid2">
     <div><label class="f">Rechnungsdatum von</label><input type="date" name="start" value="{{ start }}" required></div>
     <div><label class="f">Rechnungsdatum bis</label><input type="date" name="end" value="{{ end }}" required></div>
     <div><label class="f">Lieferscheine zusätzlich Tage vorher</label>
       <input type="number" name="lookback" value="{{ lookback }}" min="0" max="365"></div>
   </div>
   <label class="switch"><input type="checkbox" name="delete_originals" value="1" checked>
     Originale nach dem Zusammenführen in den Papierkorb verschieben</label>
   <p class="meta">Ohne Haken bleiben die Originale erhalten; verwendete Lieferscheine bekommen das Tag
     „{{ done_tag }}“ und werden beim nächsten Lauf nicht mehr vorgeschlagen.</p>
  </section>

  <section>
   <h3>Korrespondenten</h3>
   <p class="meta" style="margin:0">Nichts ausgewählt = alle durchsuchen.
     <button type="button" class="linkbtn" data-clear="corrchips">Auswahl leeren</button></p>
   {% if corrs %}
   {% if corrs|length > 12 %}<input type="search" class="filter" data-target="corrchips" placeholder="Korrespondent suchen …" style="margin-top:10px">{% endif %}
   <div class="chips" id="corrchips">
     {% for c in corrs %}<label class="chip plain" data-name="{{ c.name|lower }}"><input type="checkbox" name="corr" value="{{ c.id }}"><span>{{ c.name }}{% if c.document_count is not none %}<em class="cnt">{{ c.document_count }}</em>{% endif %}</span></label>{% endfor %}
   </div>
   <label class="switch"><input type="checkbox" name="corr_none" value="1" checked>
     Lieferscheine ohne Korrespondent mit durchsuchen</label>
   {% else %}<p class="meta">Keine Korrespondenten geladen.</p>{% endif %}
  </section>

  <section>
   <h3>Tags für die neuen Dokumente</h3>
   <p class="meta" style="margin:0">Zusätzlich zu den Tags der Rechnung. Anklicken zum An-/Abwählen.</p>
   {% if tags %}
   {% if tags|length > 12 %}<input type="search" class="filter" data-target="tagchips" placeholder="Tag suchen …" style="margin-top:10px">{% endif %}
   <div class="chips" id="tagchips">
     {% for t in tags %}<label class="chip" data-name="{{ t.name|lower }}"><input type="checkbox" name="tags" value="{{ t.id }}"
       {{ 'checked' if t.preselect }}><span style="--c:{{ t.color or '#6366f1' }};--tc:{{ t.text_color or '#ffffff' }}">{{ t.name }}</span></label>{% endfor %}
   </div>
   {% else %}<p class="meta">Keine Tags geladen.</p>{% endif %}
  </section>
 </div>
 <div style="margin-top:24px;display:flex;gap:16px;align-items:center;flex-wrap:wrap">
   <button>Suchen &amp; Vorschläge anzeigen →</button>
   <span class="meta">Es wird noch nichts verändert – im nächsten Schritt prüfst du alle Zuordnungen.</span></div>
</form>
<form class="card" method="post" action="{{ url_for('cleanup_titles') }}"
      onsubmit="return confirm('„(merged)“ aus allen Dokumenttiteln in Paperless entfernen?')"
      style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
  <button class="sec">Titel bereinigen</button>
  <span class="meta">Entfernt nachträglich jedes „(merged)“ aus allen Dokumenttiteln in Paperless – auch mehrfache.</span>
</form>
<script>
document.querySelectorAll('input.filter').forEach(f=>f.addEventListener('input',()=>{const q=f.value.toLowerCase();
  document.querySelectorAll('#'+f.dataset.target+' .chip').forEach(c=>c.style.display=c.dataset.name.includes(q)||c.querySelector('input').checked?'':'none')}));
document.querySelectorAll('[data-clear]').forEach(b=>b.addEventListener('click',()=>
  document.querySelectorAll('#'+b.dataset.clear+' input').forEach(i=>i.checked=false)));
</script>
"""

PREVIEW = """
<form method="post" action="{{ url_for('execute') }}" id="f">
<input type="hidden" name="plan" value="{{ plan_id }}">
<div class="card"><h3>2 · Zuordnungen prüfen</h3>
  <div class="meta">Zeitraum {{ start.strftime('%d.%m.%Y') }} – {{ end.strftime('%d.%m.%Y') }}</div>
  <div class="stats">
    <div class="stat"><b>{{ plan.groups|length }}</b><span class="meta">mit Vorschlag</span></div>
    <div class="stat"><b>{{ plan.without|length }}</b><span class="meta">Rechnungen ohne</span></div>
    <div class="stat"><b>{{ plan.unassigned|length }}</b><span class="meta">Lieferscheine frei</span></div>
  </div>
  Originale: <b>{{ "werden in den Papierkorb verschoben" if delete_originals else "bleiben erhalten" }}</b><br>
  Korrespondenten: {% for c in sel_corrs %}<span class="pill tag">{{ c }}</span>{% else %}<span class="meta">alle</span>{% endfor %}<br>
  Zusätzliche Tags: {% for t in extra_tags %}<span class="pill" style="background:{{ t.color or '#6366f1' }};color:{{ t.text_color or '#fff' }}">{{ t.name }}</span>{% else %}<span class="meta">keine</span>{% endfor %}
  <p class="meta">Reihenfolge im neuen PDF: Rechnung zuerst, danach die Lieferscheine nach Datum.
  Das neue Dokument übernimmt Titel, Korrespondent und Dokumententyp „{{ type_invoice }}“ der Rechnung sowie <b>alle Tags der Rechnung und der zugehörigen Lieferscheine</b>.
  <span class="badge sicher">sicher</span> Nummer gefunden ·
  <span class="badge mittel">mittel</span> Nummer gefunden, aber Auffälligkeit ·
  <span class="badge unsicher">unsicher</span> nur Korrespondent/Datum, standardmäßig abgewählt</p>
</div>

{% macro group(g) %}
<div class="card">
  <div class="grp-head">
    <input type="checkbox" name="inv" value="{{ g.inv.id }}" class="inv" {{ 'checked' if g.checked }}>
    <a href="{{ link(g.inv.id) }}" target="_blank"><img class="th" loading="lazy" src="{{ url_for('thumb', doc_id=g.inv.id) }}"></a>
    <div><span class="tag">{{ type_invoice }}</span> <b>{{ g.inv.title }}</b><br>
      <span class="meta">{{ g.inv.date|de }} · {{ g.inv.correspondent }}{% if g.inv.asn %} · ASN {{ g.inv.asn }}{% endif %} ·
      <a href="{{ link(g.inv.id) }}" target="_blank">{{ "in Paperless öffnen" if public else "PDF ansehen" }}</a></span></div>
  </div>
  {% if g['items'] %}<div class="items">{% endif %}
  {% for i in g['items'] %}
  <div class="row">
    <input type="checkbox" name="ls_{{ g.inv.id }}" value="{{ i.id }}" class="ls" data-inv="{{ g.inv.id }}" {{ 'checked' if i.checked }}>
    <a href="{{ link(i.id) }}" target="_blank"><img class="th" loading="lazy" src="{{ url_for('thumb', doc_id=i.id) }}"></a>
    <div><span class="badge {{ i.level }}">{{ i.level }}</span> <b>{{ i.title }}</b><br>
      <span class="meta">{{ i.date|de }} · {{ i.correspondent }} · {{ i.reason }}</span></div>
  </div>
  {% endfor %}
  {% if g['items'] %}</div>{% endif %}
  {% if plan.unassigned %}
  <p class="meta" style="margin:12px 0 0 32px">Weitere Lieferscheine manuell hinzufügen (Strg/Cmd für Mehrfachauswahl):</p>
  <select name="add_{{ g.inv.id }}" multiple size="3" class="add" data-inv="{{ g.inv.id }}">
    {% for u in plan.unassigned %}<option value="{{ u.id }}">{{ u.date|de }} · {{ u.correspondent }} · {{ u.title }}</option>{% endfor %}
  </select>
  {% endif %}
</div>
{% endmacro %}

<div class="groups">{% for g in plan.groups %}{{ group(g) }}{% endfor %}</div>
{% if plan.without %}
<details class="card"><summary><b>{{ plan.without|length }} Rechnungen ohne gefundenen Lieferschein</b> (manuell zuordenbar)</summary>
<div class="groups">{% for g in plan.without %}{{ group(g) }}{% endfor %}</div></details>
{% endif %}
{% if plan.unassigned %}
<details class="card"><summary><b>{{ plan.unassigned|length }} nicht zugeordnete Lieferscheine</b></summary>
<ul>{% for u in plan.unassigned %}<li><a href="{{ link(u.id) }}" target="_blank">{{ u.title }}</a>
  <span class="meta">{{ u.date|de }} · {{ u.correspondent }}</span></li>{% endfor %}</ul></details>
{% endif %}

<div class="card bar"><a class="btn sec" href="{{ url_for('index') }}">← Abbrechen</a>
  <button type="submit">3 · Ausgewählte zusammenführen</button></div>
</form>
<script>
document.querySelectorAll('.ls,.add').forEach(el => el.addEventListener('change', () => {
  const inv = document.querySelector('.inv[value="' + el.dataset.inv + '"]');
  if (el.checked || (el.selectedOptions && el.selectedOptions.length)) inv.checked = true;
}));
document.getElementById('f').addEventListener('submit', e => {
  let n = 0;
  document.querySelectorAll('.inv:checked').forEach(inv => {
    const id = inv.value;
    const ls = document.querySelectorAll('.ls[data-inv="' + id + '"]:checked').length;
    const add = document.querySelector('.add[data-inv="' + id + '"]');
    if (ls + (add ? add.selectedOptions.length : 0) > 0) n++;
  });
  if (!n) { alert('Nichts ausgewählt.'); e.preventDefault(); return; }
  if (!confirm(n + ' Rechnung(en) jetzt mit ihren Lieferscheinen zusammenführen?')) e.preventDefault();
});
</script>
"""

RESULT = """
<div class="card {{ 'err' if errors else 'ok' }}"><h3>✓ Ergebnis</h3>
  {{ done|length }} Zusammenführung(en) an Paperless übergeben{% if errors %}, {{ errors|length }} Fehler{% endif %}.
  <p class="meta">Paperless verarbeitet die neuen PDFs im Hintergrund – sie erscheinen nach kurzer Zeit.</p></div>
{% for d in done %}<div class="card">✔ {{ d.title }} + {{ d.count }} Lieferschein(e)</div>{% endfor %}
{% for e in errors %}<div class="card err">✘ {{ e }}</div>{% endfor %}
<div class="card">
  {% if auto_clean %}<p class="meta" style="margin-top:0">Der Titelzusatz „(merged)“ wird automatisch entfernt,
    sobald Paperless die neuen Dokumente angelegt hat (bis zu 5 Minuten).</p>{% endif %}
  <form method="post" action="{{ url_for('cleanup_titles') }}" style="display:inline">
    <button class="sec">„(merged)“ jetzt aus allen Titeln entfernen</button></form>
  <p style="margin-top:14px"><a class="btn" href="{{ url_for('index') }}">Neuer Durchlauf</a></p></div>
"""


CLEANUP = """
<div class="card {{ 'err' if errors else 'ok' }}">{{ msg }}</div>
{% for e in errors %}<div class="card err">{{ e }}</div>{% endfor %}
<div class="card"><a href="{{ url_for('index') }}">Zurück zum Start</a></div>
"""


def _cleanup_plans():
    now = time.time()
    with PLANS_LOCK:
        for k in [k for k, v in PLANS.items() if now - v["ts"] > PLAN_TTL]:
            del PLANS[k]


@app.route("/")
@require_auth
def index(error=None):
    today = dt.date.today()
    first_prev = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    tags, corrs = [], []
    try:
        pl = Paperless()
        corrs = pl.correspondent_list()
        for t in pl.tags():
            if t["name"] == DONE_TAG:
                continue
            t["preselect"] = t["name"].lower() in DEFAULT_TAGS
            tags.append(t)
    except Exception as e:  # noqa: BLE001
        error = error or f"Tags/Korrespondenten konnten nicht geladen werden: {e}"
    return page(INDEX, error=error, start=first_prev.isoformat(), end=today.isoformat(),
                lookback=DEFAULT_LOOKBACK, done_tag=DONE_TAG, tags=tags, corrs=corrs)


@app.route("/preview", methods=["POST"])
@require_auth
def preview():
    _cleanup_plans()
    try:
        start = dt.date.fromisoformat(request.form["start"])
        end = dt.date.fromisoformat(request.form["end"])
        lookback = max(0, int(request.form.get("lookback") or 0))
    except ValueError:
        return index(error="Ungültige Datumsangabe.")
    if end < start:
        return index(error="Das Enddatum liegt vor dem Startdatum.")
    delete_originals = request.form.get("delete_originals") == "1"
    corr_ids = {int(x) for x in request.form.getlist("corr") if x.isdigit()}
    corr_none = request.form.get("corr_none") == "1"

    try:
        pl = Paperless()
        t_inv = pl.id_by_name("/api/document_types/", TYPE_INVOICE)
        t_ls = pl.id_by_name("/api/document_types/", TYPE_DELIVERY)
        if not t_inv or not t_ls:
            return index(error=f"Dokumententyp „{TYPE_INVOICE}“ oder „{TYPE_DELIVERY}“ nicht gefunden.")
        done_tag = pl.id_by_name("/api/tags/", DONE_TAG)
        # Rechnungen mit etwas Puffer laden, damit Lieferscheine an die "beste" Rechnung gehen
        ls_from = start - dt.timedelta(days=lookback)
        invoices = pl.documents(t_inv, ls_from, end + dt.timedelta(days=FALLBACK_DAYS), corr_ids=corr_ids)
        deliveries = pl.documents(t_ls, ls_from, end, exclude_tag=done_tag, corr_ids=corr_ids)
        if corr_ids and corr_none:  # nur bei Filter nötig – ohne Filter sind diese schon enthalten
            deliveries += pl.documents(t_ls, ls_from, end, exclude_tag=done_tag, corr_none=True)
        elif not corr_ids and not corr_none:
            deliveries = [d for d in deliveries if d.get("correspondent") is not None]
        corr = pl.correspondents()
        wanted = {int(x) for x in request.form.getlist("tags") if x.isdigit()}
        extra_tags = [t for t in pl.tags() if t["id"] in wanted] if wanted else []
    except Exception as e:  # noqa: BLE001
        return index(error=f"Fehler bei der Abfrage von Paperless: {e}")

    plan = build_plan(invoices, deliveries, corr, start)
    plan["groups"] = [g for g in plan["groups"] if g["inv"]["date"] <= end.isoformat()]
    plan["without"] = [g for g in plan["without"] if g["inv"]["date"] <= end.isoformat()]

    plan_id = uuid.uuid4().hex
    with PLANS_LOCK:
        PLANS[plan_id] = {"ts": time.time(), "plan": plan, "delete_originals": delete_originals,
                          "extra_tags": [t["id"] for t in extra_tags]}
    return page(PREVIEW, plan=plan, plan_id=plan_id, start=start, end=end,
                delete_originals=delete_originals, public=PAPERLESS_PUBLIC_URL, link=doc_link,
                type_invoice=TYPE_INVOICE, extra_tags=extra_tags,
                sel_corrs=[corr.get(c, f"#{c}") for c in sorted(corr_ids, key=lambda c: corr.get(c, "").lower())])


@app.route("/execute", methods=["POST"])
@require_auth
def execute():
    with PLANS_LOCK:
        stored = PLANS.pop(request.form.get("plan", ""), None)
    if not stored:
        return index(error="Der Vorschlag ist abgelaufen oder wurde schon ausgeführt. Bitte neu suchen.")
    plan = stored["plan"]
    all_groups = {g["inv"]["id"]: g for g in plan["groups"] + plan["without"]}
    known_ls = {i["id"]: i for g in all_groups.values() for i in g["items"]}
    known_ls.update({u["id"]: u for u in plan["unassigned"]})

    jobs, seen = [], {}
    for inv_id in map(int, request.form.getlist("inv")):
        if inv_id not in all_groups:
            continue
        ls_ids = []
        for v in request.form.getlist(f"ls_{inv_id}") + request.form.getlist(f"add_{inv_id}"):
            lid = int(v)
            if lid in known_ls and lid not in ls_ids:
                ls_ids.append(lid)
        if not ls_ids:
            continue
        for lid in ls_ids:
            if lid in seen:
                return index(error=f"Lieferschein „{known_ls[lid]['title']}“ wurde mehreren Rechnungen "
                                   f"zugeordnet. Es wurde nichts ausgeführt – bitte neu suchen.")
            seen[lid] = inv_id
        ls_ids.sort(key=lambda x: (known_ls[x]["date"], x))
        jobs.append((all_groups[inv_id]["inv"], ls_ids))

    pl = Paperless()
    done, errors, tag_ls = [], [], []
    extra = stored.get("extra_tags", [])
    try:
        done_tag_id = pl.id_by_name("/api/tags/", DONE_TAG)
    except Exception:  # noqa: BLE001
        done_tag_id = None
    for inv, ls_ids in jobs:
        # Das neue Dokument übernimmt die Tags der Rechnung. Damit es auch die Tags der Lieferscheine
        # und die gewählten Zusatz-Tags bekommt, werden diese vorher an die Rechnung gehängt.
        wanted = []
        for t in [*(t for lid in ls_ids for t in known_ls[lid].get("tags", [])), *extra]:
            if t not in wanted and t not in inv.get("tags", []) and t != done_tag_id:
                wanted.append(t)
        add_now = wanted
        try:
            if add_now:
                pl.modify_tags([inv["id"]], add=add_now)
            if clean_title(inv["title"]) != inv["title"]:  # sonst entsteht "(merged) (merged)"
                pl.send("PATCH", f"/api/documents/{inv['id']}/", {"title": clean_title(inv["title"])})
            pl.merge([inv["id"]] + ls_ids, inv["id"], stored["delete_originals"])
            done.append({"title": clean_title(inv["title"]), "count": len(ls_ids)})
            tag_ls.extend(ls_ids)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{inv['title']}: {e}")
            if add_now:  # nichts zusammengeführt -> Rechnung wieder wie vorher
                try:
                    pl.modify_tags([inv["id"]], remove=add_now)
                except Exception:  # noqa: BLE001
                    pass
            continue
        if add_now and not stored["delete_originals"]:
            try:  # Original-Rechnung bleibt erhalten -> dort die Zusatz-Tags wieder entfernen
                pl.modify_tags([inv["id"]], remove=add_now)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{inv['title']}: übernommene Tags am Original nicht entfernt: {e}")

    if tag_ls and not stored["delete_originals"]:
        try:
            pl.add_tag(tag_ls, pl.ensure_tag(DONE_TAG))
        except Exception as e:  # noqa: BLE001
            errors.append(f"Tag „{DONE_TAG}“ konnte nicht gesetzt werden: {e}")
    if done and AUTO_CLEAN_TITLES:
        threading.Thread(target=_auto_clean_titles, args=(len(done),), daemon=True).start()
    return page(RESULT, done=done, errors=errors, auto_clean=AUTO_CLEAN_TITLES)


@app.route("/cleanup-titles", methods=["POST"])
@require_auth
def cleanup_titles():
    try:
        found, fixed, errors = clean_merged_titles(Paperless())
    except Exception as e:  # noqa: BLE001
        return page(CLEANUP, msg=f"Fehler bei der Abfrage von Paperless: {e}", errors=[])
    msg = f"{fixed} Titel bereinigt." if found else "Keine Dokumente mit „(merged)“ im Titel gefunden."
    return page(CLEANUP, msg=msg, errors=errors)


def doc_link(doc_id):
    """Link zum Dokument: in Paperless (wenn PUBLIC_URL gesetzt), sonst PDF über dieses Tool."""
    if PAPERLESS_PUBLIC_URL:
        return f"{PAPERLESS_PUBLIC_URL}/documents/{doc_id}/details"
    return url_for("doc_pdf", doc_id=doc_id)


@app.route("/doc/<int:doc_id>")
@require_auth
def doc_pdf(doc_id):
    try:
        r = Paperless().get(f"/api/documents/{doc_id}/preview/")
    except Exception:  # noqa: BLE001
        abort(404)
    return Response(r.content, content_type=r.headers.get("Content-Type", "application/pdf"),
                    headers={"Content-Disposition": f'inline; filename="dokument-{doc_id}.pdf"'})


@app.route("/thumb/<int:doc_id>")
@require_auth
def thumb(doc_id):
    try:
        r = Paperless().get(f"/api/documents/{doc_id}/thumb/")
    except Exception:  # noqa: BLE001
        abort(404)
    return Response(r.content, content_type=r.headers.get("Content-Type", "image/webp"),
                    headers={"Cache-Control": "max-age=3600"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
