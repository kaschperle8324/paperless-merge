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
FALLBACK_DAYS = int(os.environ.get("FALLBACK_DAYS", "45"))
DEFAULT_LOOKBACK = int(os.environ.get("DEFAULT_LOOKBACK_DAYS", "60"))
VERIFY_SSL = os.environ.get("VERIFY_SSL", "true").lower() not in ("0", "false", "no")
APP_USER = os.environ.get("APP_USER")
APP_PASSWORD = os.environ.get("APP_PASSWORD")
PLAN_TTL = 3600  # Sekunden, wie lange ein Vorschlag gültig bleibt

app = Flask(__name__)
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

    def documents(self, type_id, start, end, exclude_tag=None):
        params = {
            "document_type__id": type_id,
            "created__gte": start.isoformat(),
            "created__lt": (end + dt.timedelta(days=1)).isoformat(),
            "ordering": "created",
            "fields": "id,title,correspondent,created,content,tags,archive_serial_number",
        }
        if exclude_tag:
            params["tags__id__none"] = exclude_tag
        return self.all("/api/documents/", **params)

    def correspondents(self):
        return {c["id"]: c["name"] for c in self.all("/api/correspondents/")}

    def merge(self, doc_ids, metadata_id, delete_originals):
        return self.send("POST", "/api/documents/bulk_edit/", {
            "documents": doc_ids,
            "method": "merge",
            "parameters": {"metadata_document_id": metadata_id, "delete_originals": delete_originals},
        })

    def add_tag(self, doc_ids, tag_id):
        return self.send("POST", "/api/documents/bulk_edit/", {
            "documents": doc_ids, "method": "add_tag", "parameters": {"tag": tag_id},
        })


# --------------------------------------------------------------------------- Zuordnungslogik
KEYWORDS = (r"(?:lieferscheine?|liefer\s*-?\s*schein|liefer-?nr|ls-?nr|lief\.?\s*-?\s*nr"
            r"|delivery\s*note|lieferung)")
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
<title>Rechnung + Lieferschein</title><style>
body{font-family:system-ui,sans-serif;margin:0;background:#f4f5f7;color:#222}
header{background:#17541f;color:#fff;padding:14px 20px;font-size:1.2em}
main{max-width:1100px;margin:20px auto;padding:0 16px}
.card{background:#fff;border-radius:8px;padding:16px;margin-bottom:16px;box-shadow:0 1px 3px #0002}
label{display:block;margin:8px 0 4px;font-weight:600}
input[type=date],input[type=number]{padding:6px;font-size:1em}
button{background:#17541f;color:#fff;border:0;padding:10px 18px;border-radius:6px;font-size:1em;cursor:pointer}
button.sec{background:#666}
.grp-head{display:flex;gap:12px;align-items:center;border-bottom:1px solid #ddd;padding-bottom:10px}
.row{display:flex;gap:12px;align-items:center;padding:8px 0 8px 28px;border-bottom:1px dashed #eee}
img.th{width:70px;height:95px;object-fit:cover;border:1px solid #ccc;background:#eee}
.meta{color:#666;font-size:.9em}
.badge{padding:2px 8px;border-radius:10px;font-size:.8em;color:#fff}
.sicher{background:#2e7d32}.mittel{background:#ef6c00}.unsicher{background:#c62828}
.tag{background:#17541f;color:#fff;padding:1px 6px;border-radius:4px;font-size:.8em}
select{min-width:320px;margin-left:28px}
.err{background:#fdecea;color:#b71c1c}.ok{background:#e8f5e9}
a{color:#17541f}
</style></head><body><header>Paperless · Rechnung + Lieferschein zusammenführen</header><main>
{% block body %}{% endblock %}</main></body></html>"""


def page(body, **ctx):
    return render_template_string(BASE.replace("{% block body %}{% endblock %}", body), **ctx)


INDEX = """
{% if error %}<div class="card err">{{ error }}</div>{% endif %}
<form class="card" method="post" action="{{ url_for('preview') }}">
  <h3>1. Zeitraum festlegen</h3>
  <label>Rechnungsdatum von</label><input type="date" name="start" value="{{ start }}" required>
  <label>Rechnungsdatum bis</label><input type="date" name="end" value="{{ end }}" required>
  <label>Lieferscheine zusätzlich so viele Tage vor dem Startdatum berücksichtigen</label>
  <input type="number" name="lookback" value="{{ lookback }}" min="0" max="365">
  <label><input type="checkbox" name="delete_originals" value="1" checked>
    Originale nach dem Zusammenführen in den Papierkorb verschieben</label>
  <p class="meta">Ohne Haken bleiben die Originale erhalten; die verwendeten Lieferscheine
    bekommen dann das Tag „{{ done_tag }}“ und werden beim nächsten Lauf nicht mehr vorgeschlagen.</p>
  <button>Suchen &amp; Vorschläge anzeigen</button>
</form>
<div class="card meta">Es wird noch nichts verändert – im nächsten Schritt prüfst du alle Zuordnungen.</div>
"""

PREVIEW = """
<form method="post" action="{{ url_for('execute') }}" id="f">
<input type="hidden" name="plan" value="{{ plan_id }}">
<div class="card"><h3>2. Zuordnungen prüfen</h3>
  Zeitraum {{ start }} bis {{ end }} · {{ plan.groups|length }} Rechnungen mit Lieferschein-Vorschlag,
  {{ plan.without|length }} ohne, {{ plan.unassigned|length }} Lieferscheine nicht zugeordnet.<br>
  Originale: <b>{{ "werden in den Papierkorb verschoben" if delete_originals else "bleiben erhalten" }}</b>
  <p class="meta">Reihenfolge im neuen PDF: Rechnung zuerst, danach die Lieferscheine nach Datum.
  Das neue Dokument übernimmt Titel, Korrespondent, Tags und Dokumententyp „{{ type_invoice }}“ der Rechnung.
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
      <span class="meta">{{ g.inv.date }} · {{ g.inv.correspondent }}{% if g.inv.asn %} · ASN {{ g.inv.asn }}{% endif %} ·
      <a href="{{ link(g.inv.id) }}" target="_blank">{{ "in Paperless öffnen" if public else "PDF ansehen" }}</a></span></div>
  </div>
  {% for i in g['items'] %}
  <div class="row">
    <input type="checkbox" name="ls_{{ g.inv.id }}" value="{{ i.id }}" class="ls" data-inv="{{ g.inv.id }}" {{ 'checked' if i.checked }}>
    <a href="{{ link(i.id) }}" target="_blank"><img class="th" loading="lazy" src="{{ url_for('thumb', doc_id=i.id) }}"></a>
    <div><span class="badge {{ i.level }}">{{ i.level }}</span> <b>{{ i.title }}</b><br>
      <span class="meta">{{ i.date }} · {{ i.correspondent }} · {{ i.reason }}</span></div>
  </div>
  {% endfor %}
  {% if plan.unassigned %}
  <p class="meta" style="margin-left:28px">Weitere Lieferscheine manuell hinzufügen (Strg/Cmd für Mehrfachauswahl):</p>
  <select name="add_{{ g.inv.id }}" multiple size="3" class="add" data-inv="{{ g.inv.id }}">
    {% for u in plan.unassigned %}<option value="{{ u.id }}">{{ u.date }} · {{ u.correspondent }} · {{ u.title }}</option>{% endfor %}
  </select>
  {% endif %}
</div>
{% endmacro %}

{% for g in plan.groups %}{{ group(g) }}{% endfor %}
{% if plan.without %}
<details class="card"><summary><b>{{ plan.without|length }} Rechnungen ohne gefundenen Lieferschein</b> (manuell zuordenbar)</summary>
{% for g in plan.without %}{{ group(g) }}{% endfor %}</details>
{% endif %}
{% if plan.unassigned %}
<details class="card"><summary><b>{{ plan.unassigned|length }} nicht zugeordnete Lieferscheine</b></summary>
<ul>{% for u in plan.unassigned %}<li><a href="{{ link(u.id) }}" target="_blank">{{ u.title }}</a>
  <span class="meta">{{ u.date }} · {{ u.correspondent }}</span></li>{% endfor %}</ul></details>
{% endif %}

<div class="card"><button type="submit">3. Ausgewählte jetzt zusammenführen</button>
  <a href="{{ url_for('index') }}"><button type="button" class="sec">Abbrechen</button></a></div>
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
<div class="card {{ 'err' if errors else 'ok' }}"><h3>Ergebnis</h3>
  {{ done|length }} Zusammenführung(en) an Paperless übergeben{% if errors %}, {{ errors|length }} Fehler{% endif %}.
  <p class="meta">Paperless verarbeitet die neuen PDFs im Hintergrund – sie erscheinen nach kurzer Zeit.</p></div>
{% for d in done %}<div class="card">✔ {{ d.title }} + {{ d.count }} Lieferschein(e)</div>{% endfor %}
{% for e in errors %}<div class="card err">✘ {{ e }}</div>{% endfor %}
<div class="card">
  <form method="post" action="{{ url_for('cleanup_titles') }}" style="display:inline">
    <button class="sec">Titelzusatz „(merged)“ entfernen</button></form>
  <span class="meta">(nach ein, zwei Minuten, wenn Paperless fertig ist)</span>
  <p><a href="{{ url_for('index') }}">Neuer Durchlauf</a></p></div>
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
    return page(INDEX, error=error, start=first_prev.isoformat(), end=today.isoformat(),
                lookback=DEFAULT_LOOKBACK, done_tag=DONE_TAG)


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

    try:
        pl = Paperless()
        t_inv = pl.id_by_name("/api/document_types/", TYPE_INVOICE)
        t_ls = pl.id_by_name("/api/document_types/", TYPE_DELIVERY)
        if not t_inv or not t_ls:
            return index(error=f"Dokumententyp „{TYPE_INVOICE}“ oder „{TYPE_DELIVERY}“ nicht gefunden.")
        done_tag = pl.id_by_name("/api/tags/", DONE_TAG)
        # Rechnungen mit etwas Puffer laden, damit Lieferscheine an die "beste" Rechnung gehen
        invoices = pl.documents(t_inv, start - dt.timedelta(days=lookback), end + dt.timedelta(days=FALLBACK_DAYS))
        deliveries = pl.documents(t_ls, start - dt.timedelta(days=lookback), end, exclude_tag=done_tag)
        corr = pl.correspondents()
    except Exception as e:  # noqa: BLE001
        return index(error=f"Fehler bei der Abfrage von Paperless: {e}")

    plan = build_plan(invoices, deliveries, corr, start)
    plan["groups"] = [g for g in plan["groups"] if g["inv"]["date"] <= end.isoformat()]
    plan["without"] = [g for g in plan["without"] if g["inv"]["date"] <= end.isoformat()]

    plan_id = uuid.uuid4().hex
    with PLANS_LOCK:
        PLANS[plan_id] = {"ts": time.time(), "plan": plan, "delete_originals": delete_originals}
    return page(PREVIEW, plan=plan, plan_id=plan_id, start=start, end=end,
                delete_originals=delete_originals, public=PAPERLESS_PUBLIC_URL, link=doc_link, type_invoice=TYPE_INVOICE)


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
    for inv, ls_ids in jobs:
        try:
            pl.merge([inv["id"]] + ls_ids, inv["id"], stored["delete_originals"])
            done.append({"title": inv["title"], "count": len(ls_ids)})
            tag_ls.extend(ls_ids)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{inv['title']}: {e}")

    if tag_ls and not stored["delete_originals"]:
        try:
            pl.add_tag(tag_ls, pl.ensure_tag(DONE_TAG))
        except Exception as e:  # noqa: BLE001
            errors.append(f"Tag „{DONE_TAG}“ konnte nicht gesetzt werden: {e}")
    return page(RESULT, done=done, errors=errors)


@app.route("/cleanup-titles", methods=["POST"])
@require_auth
def cleanup_titles():
    pl = Paperless()
    t_inv = pl.id_by_name("/api/document_types/", TYPE_INVOICE)
    docs = pl.all("/api/documents/", document_type__id=t_inv, title__iendswith="(merged)", fields="id,title")
    errors = []
    for d in docs:
        new = re.sub(r"\s*\(merged\)\s*$", "", d["title"], flags=re.I)
        try:
            pl.send("PATCH", f"/api/documents/{d['id']}/", {"title": new})
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
    msg = f"{len(docs) - len(errors)} Titel bereinigt." if docs else "Keine Dokumente mit „(merged)“ gefunden."
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
