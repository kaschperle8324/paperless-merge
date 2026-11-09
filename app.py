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
import difflib
import hashlib
import hmac
import io
import json
import os
import shutil
import tempfile
import re
import secrets
import threading
import time
import uuid
from functools import wraps

import requests
from flask import Flask, Response, abort, redirect, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

# --------------------------------------------------------------------------- Konfiguration
PAPERLESS_URL = os.environ.get("PAPERLESS_URL", "http://paperless:8000").rstrip("/")
PAPERLESS_PUBLIC_URL = os.environ.get("PAPERLESS_PUBLIC_URL", "").rstrip("/")  # optional
# Ziel der Taste "Paperless öffnen": PUBLIC_URL, sonst PAPERLESS_URL (passt, wenn dort IP:Port steht)
PAPERLESS_BROWSER_URL = PAPERLESS_PUBLIC_URL or PAPERLESS_URL
TOKEN = os.environ.get("PAPERLESS_TOKEN", "")
TYPE_INVOICE = os.environ.get("DOCTYPE_INVOICE", "Rechnung")
TYPE_DELIVERY = os.environ.get("DOCTYPE_DELIVERY", "Lieferschein")
DONE_TAG = os.environ.get("DONE_TAG", "LS-zugeordnet")          # an verwendeten Lieferscheinen
MERGED_TAG = os.environ.get("MERGED_TAG", "LS-ergänzt")          # an Rechnungen mit ergänzten Lieferscheinen
DEFAULT_TAGS = [t.strip().lower() for t in os.environ.get("DEFAULT_TAGS", "Posteingang").split(",") if t.strip()]
FALLBACK_DAYS = int(os.environ.get("FALLBACK_DAYS", "45"))
DEFAULT_LOOKBACK = int(os.environ.get("DEFAULT_LOOKBACK_DAYS", "60"))
VERIFY_SSL = os.environ.get("VERIFY_SSL", "true").lower() not in ("0", "false", "no")
APP_USER = os.environ.get("APP_USER")
APP_PASSWORD = os.environ.get("APP_PASSWORD")
AUTO_CLEAN_TITLES = os.environ.get("AUTO_CLEAN_TITLES", "true").lower() not in ("0", "false", "no")
# KI-Anbindung: Texterkennung (OCR) und Auswertung, einstellbar in der Oberfläche (Seite "Einstellungen").
# Die Umgebungsvariablen gelten als Vorbelegung, solange in der Oberfläche nichts gespeichert wurde.
AI_PROVIDERS = {  # Anbieter mit OpenAI-kompatibler API: (Adresse, Standardmodell)
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "mistral": ("https://api.mistral.ai/v1", "mistral-small-latest"),
}
OCR_PROVIDERS = {"mistral": ("https://api.mistral.ai/v1/ocr", "mistral-ocr-latest")}
DATA_DIR = os.environ.get("DATA_DIR", "/data")
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", os.path.join(DATA_DIR, "einstellungen.json"))
AI_MAX_CHARS = int(os.environ.get("AI_MAX_CHARS", "6000"))    # Textlänge je Dokument, die an die KI geht
AI_TIMEOUT = int(os.environ.get("AI_TIMEOUT", "60"))
AI_CACHE_FILE = os.environ.get("AI_CACHE_FILE", os.path.join(DATA_DIR, "ki-cache.json"))
AUDIT_MAX_DOCS = int(os.environ.get("AUDIT_MAX_DOCS", "200"))
# Vorgaben für die Suche nach Rechnungen zu nicht zugeordneten Lieferscheinen
SEARCH_DOCTYPES = [t.strip() for t in os.environ.get("SEARCH_DOCTYPES", TYPE_INVOICE).split(",") if t.strip()]
SEARCH_DAYS_BEFORE = int(os.environ.get("SEARCH_DAYS_BEFORE", "30"))
SEARCH_DAYS_AFTER = int(os.environ.get("SEARCH_DAYS_AFTER", "365"))
SEARCH_UNLIMITED = os.environ.get("SEARCH_UNLIMITED", "false").lower() in ("1", "true", "yes")
SEARCH_SAME_CORR = os.environ.get("SEARCH_SAME_CORR", "true").lower() not in ("0", "false", "no")
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
            err = RuntimeError(f"{method} {path}: HTTP {r.status_code} – {r.text[:300]}")
            err.status = r.status_code
            raise err
        try:
            return r.json() if r.content else {}
        except ValueError:
            return {"result": r.text}

    def _send_new_or_legacy(self, new_path, new_payload, legacy_payload):
        """Neuer Endpunkt (Paperless 3.x), bei 404/405 der alte bulk_edit-Weg (Paperless 2.x)."""
        try:
            return self.send("POST", new_path, new_payload)
        except RuntimeError as e:
            if getattr(e, "status", None) not in (404, 405):
                raise
        return self.send("POST", "/api/documents/bulk_edit/", legacy_payload)

    def delete_docs(self, doc_ids):
        """Verschiebt Dokumente in den Papierkorb (in Paperless wiederherstellbar)."""
        return self._send_new_or_legacy(
            "/api/documents/delete/", {"documents": list(doc_ids)},
            {"documents": list(doc_ids), "method": "delete", "parameters": {}},
        )

    def download(self, doc_id, original=False):
        """PDF eines Dokuments (Archivversion mit Textebene, wenn vorhanden)."""
        r = self.s.get(f"{PAPERLESS_URL}/api/documents/{doc_id}/download/",
                       params={"original": "true"} if original else None, timeout=120)
        r.raise_for_status()
        return r.content

    def remove_pages(self, doc_id, pages, page_count):
        """Entfernt Seiten (1-basiert) aus einem Dokument. Paperless legt eine neue Version an."""
        keep = [p for p in range(1, page_count + 1) if p not in set(pages)]
        if not keep:
            raise RuntimeError("Es dürfen nicht alle Seiten entfernt werden.")
        return self._send_new_or_legacy(
            "/api/documents/edit_pdf/",
            {"documents": [doc_id], "operations": [{"page": p} for p in keep], "update_document": True},
            {"documents": [doc_id], "method": "delete_pages", "parameters": {"pages": sorted(set(pages))}},
        )

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
            "fields": "id,title,correspondent,created,content,tags,archive_serial_number,original_file_name",
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

    def doc_types(self):
        return sorted(self.all("/api/document_types/"), key=lambda t: t["name"].lower())

    def find_docs(self, type_ids, start=None, end=None, corr_id=None, content=None):
        """Dokumente bestimmter Typen, optional nach Zeitraum, Korrespondent und Textinhalt gefiltert."""
        params = {"fields": "id,title,correspondent,created,content,tags,archive_serial_number,document_type,"
                            "original_file_name,page_count",
                  "ordering": "created"}
        if type_ids:
            params["document_type__id__in"] = ",".join(map(str, sorted(type_ids)))
        if start:
            params["created__gte"] = start.isoformat()
        if end:
            params["created__lt"] = (end + dt.timedelta(days=1)).isoformat()
        if corr_id:
            params["correspondent__id"] = corr_id
        if content:
            params["content__icontains"] = content
        docs = self.all("/api/documents/", **params)
        if type_ids:  # absichern, falls ein Filter vom Server ignoriert wird
            docs = [d for d in docs if d.get("document_type") in type_ids]
        if corr_id:
            docs = [d for d in docs if d.get("correspondent") == corr_id]
        return docs

    def correspondents_full(self):
        return sorted(self.all("/api/correspondents/"), key=lambda c: c["name"].lower())

    def doc_ids_of_correspondent(self, corr_id):
        docs = self.all("/api/documents/", correspondent__id=corr_id, fields="id,correspondent")
        return [d["id"] for d in docs if d.get("correspondent") == corr_id]  # nur echte Treffer anfassen

    def set_correspondent(self, doc_ids, corr_id):
        for i in range(0, len(doc_ids), 500):
            self.send("POST", "/api/documents/bulk_edit/", {"documents": doc_ids[i:i + 500],
                                                            "method": "set_correspondent",
                                                            "parameters": {"correspondent": corr_id}})

    def delete_correspondent(self, corr_id):
        return self.send("DELETE", f"/api/correspondents/{corr_id}/", None)

    def rename_correspondent(self, corr_id, name):
        return self.send("PATCH", f"/api/correspondents/{corr_id}/", {"name": name})

    def set_correspondent_match(self, corr_id, match):
        return self.send("PATCH", f"/api/correspondents/{corr_id}/", {"match": match})

    def correspondent_list(self):
        return sorted(self.all("/api/correspondents/"), key=lambda c: c["name"].lower())

    def correspondents(self):
        return {c["id"]: c["name"] for c in self.all("/api/correspondents/")}

    def merge(self, doc_ids, metadata_id, delete_originals):
        return self._send_new_or_legacy(
            "/api/documents/merge/",
            {"documents": doc_ids, "metadata_document_id": metadata_id, "delete_originals": delete_originals},
            {"documents": doc_ids, "method": "merge",
             "parameters": {"metadata_document_id": metadata_id, "delete_originals": delete_originals}},
        )

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


def is_merged_doc(doc, merged_tag_id=None):
    """Rechnung wurde schon mit Lieferscheinen zusammengeführt: Tag gesetzt oder von Paperless
    erzeugte Merge-Datei (Dateiname "<ids>_merged.pdf")."""
    if merged_tag_id and merged_tag_id in (doc.get("tags") or []):
        return True
    return str(doc.get("original_file_name") or "").lower().endswith("_merged.pdf")


def _date(doc):
    return dt.date.fromisoformat(str(doc["created"])[:10])


def summarize(doc, corr):
    return {
        "id": doc["id"],
        "title": doc.get("title") or f"Dokument {doc['id']}",
        "date": _date(doc).isoformat(),
        "correspondent": corr.get(doc.get("correspondent"), "–"),
        "corr_id": doc.get("correspondent"),
        "asn": doc.get("archive_serial_number"),
        "tags": doc.get("tags") or [],
        "merged": bool(doc.get("_merged")),
    }


def level_of(score):
    if score >= 90:
        return "sicher"
    if score >= 50:
        return "mittel"
    return "unsicher"


def _src(extra, num):
    """Quelle einer zusätzlich gefundenen Nummer ("KI", "OCR") oder ""."""
    if not extra:
        return ""
    if isinstance(extra, dict):
        return extra.get(num, "")
    return "KI" if num in extra else ""


def build_plan(invoices, deliveries, corr, inv_start, hide_merged=True, ls_extra=None, inv_extra=None):
    """Erzeugt Zuordnungsvorschläge. Jeder Lieferschein wird höchstens einer Rechnung zugeordnet."""
    inv_text = {i["id"]: f"{i.get('title', '')}\n{i.get('content', '')}".upper() for i in invoices}
    ls_extra, inv_extra = ls_extra or {}, inv_extra or {}
    inv_refs = {i["id"]: extract_numbers(i.get("content")) | set(inv_extra.get(i["id"], [])) for i in invoices}
    ls_text = {d["id"]: f"{d.get('title', '')}\n{d.get('content', '')}".upper() for d in deliveries}
    ls_nums = {d["id"]: delivery_numbers(d) | set(ls_extra.get(d["id"], [])) for d in deliveries}
    inv_fz = {k: v.translate(OCR_MAP) for k, v in inv_text.items()}
    ls_fz = {k: v.translate(OCR_MAP) for k, v in ls_text.items()}
    all_ls_nums = sorted({n for ns in ls_nums.values() for n in ns})
    inv_found = {}
    for i in invoices:
        found, seen = [], set()
        for n in sorted(inv_refs[i["id"]]) + [n for n in all_ls_nums
                                              if contains_number(inv_text[i["id"]], n)
                                              or contains_number(inv_fz[i["id"]], ocr_norm(n))]:
            if ocr_norm(n) not in seen:
                seen.add(ocr_norm(n))
                found.append(n)
        inv_found[i["id"]] = found

    candidates = []
    for inv in invoices:
        idate, icorr = _date(inv), inv.get("correspondent")
        for ls in deliveries:
            ldate, lcorr = _date(ls), ls.get("correspondent")
            same_corr = icorr is not None and icorr == lcorr
            diff_corr = icorr is not None and lcorr is not None and icorr != lcorr
            score, reason, matched = 0, "", None

            for n in sorted(ls_nums[ls["id"]]):
                short = sum(c.isdigit() for c in n) < 5
                if short and not same_corr:
                    continue
                if contains_number(inv_text[inv["id"]], n):
                    score, reason, matched = 100, f"Lieferschein-Nr. {n} steht in der Rechnung", n
                    break
            if not score:
                for n in sorted(inv_refs[inv["id"]]):
                    short = sum(c.isdigit() for c in n) < 5
                    if short and not same_corr:
                        continue
                    if contains_number(ls_text[ls["id"]], n):
                        score, reason, matched = 95, f"Rechnung nennt Lieferschein {n}, im Lieferschein gefunden", n
                        break
            if not score:  # 2. Durchgang: OCR-tolerant (LI <-> 11, O <-> 0 ...)
                for n in sorted(ls_nums[ls["id"]]):
                    if sum(c.isdigit() for c in n) < 5 and not same_corr:
                        continue
                    if contains_number(inv_fz[inv["id"]], ocr_norm(n)):
                        score, reason, matched = 90, f"Lieferschein-Nr. {n} steht in der Rechnung (OCR-tolerant verglichen)", n
                        break
            if not score:
                for n in sorted(inv_refs[inv["id"]]):
                    if sum(c.isdigit() for c in n) < 5 and not same_corr:
                        continue
                    if contains_number(ls_fz[ls["id"]], ocr_norm(n)):
                        score, reason, matched = 90, f"Rechnung nennt Lieferschein {n}, im Lieferschein gefunden (OCR-tolerant)", n
                        break
            if score and diff_corr:
                score -= 40
                reason += " – ACHTUNG: anderer Korrespondent"
            if not score and same_corr and \
                    idate - dt.timedelta(days=FALLBACK_DAYS) <= ldate <= idate + dt.timedelta(days=3):
                score, reason = 30, "nur gleicher Korrespondent + passendes Datum (keine Nummer gefunden)"
            if inv.get("_merged") and hide_merged and score < 90:
                continue  # an bereits fertige Rechnungen nur eindeutige Nummerntreffer "binden"
            if score:
                candidates.append((score, abs((idate - ldate).days), inv["id"], ls["id"], reason, matched))

    candidates.sort(key=lambda c: (-c[0], c[1]))
    used_ls, matches = set(), {}
    for score, _, inv_id, ls_id, reason, matched in candidates:
        if ls_id in used_ls:
            continue
        used_ls.add(ls_id)
        matches.setdefault(inv_id, []).append((ls_id, score, reason, matched))

    ls_by_id = {d["id"]: d for d in deliveries}
    groups, without, contained = [], [], []
    hidden = 0
    for inv in invoices:
        if inv.get("_merged") and hide_merged:
            # Lieferscheine, deren Nummer in einer schon zusammengeführten Rechnung steht, nicht erneut anbieten
            for ls_id, score, reason, matched in matches.get(inv["id"], []):
                contained.append({**summarize(ls_by_id[ls_id], corr), "in_inv": summarize(inv, corr),
                                  "match": matched,
                                  "nums": [{"n": x, "hit": matched is not None and ocr_norm(x) == ocr_norm(matched)}
                                           for x in sorted(ls_nums[ls_id])]})
            if _date(inv) >= inv_start:
                hidden += 1
            continue
        if _date(inv) < inv_start:
            continue
        items = []
        for ls_id, score, reason, matched in matches.get(inv["id"], []):
            lvl = level_of(score)
            items.append({**summarize(ls_by_id[ls_id], corr), "score": score, "reason": reason,
                          "level": lvl, "checked": lvl != "unsicher",
                          "nums": [{"n": x, "hit": matched is not None and ocr_norm(x) == ocr_norm(matched),
                                    "ai": _src(ls_extra.get(ls_id), x)} for x in sorted(ls_nums[ls_id])]})
        items.sort(key=lambda x: x["date"])
        hits = {ocr_norm(i["nums"][k]["n"]) for i in items for k in range(len(i["nums"])) if i["nums"][k]["hit"]}
        hits |= {ocr_norm(m[3]) for m in matches.get(inv["id"], []) if m[3]}
        inv_sum = summarize(inv, corr)
        inv_sum["nums"] = [{"n": x, "hit": ocr_norm(x) in hits, "ai": _src(inv_extra.get(inv["id"]), x)}
                           for x in inv_found[inv["id"]]]
        entry = {"inv": inv_sum, "items": items,
                 "checked": any(i["checked"] for i in items)}
        (groups if items else without).append(entry)

    unassigned = [{**summarize(d, corr), "nums": [{"n": x, "hit": False, "ai": _src(ls_extra.get(d["id"]), x)}
                                                   for x in sorted(ls_nums[d["id"]])]}
                  for d in deliveries if d["id"] not in used_ls]
    return {"groups": groups, "without": without, "unassigned": unassigned,
            "contained": contained, "hidden": hidden}


def ai_enhance(invoices, deliveries, corr, inv_start, hide_merged, plan):
    """Verbessert den Plan: OCR (z. B. Mistral) liest schwache Dokumente neu, die KI ergänzt fehlende
    Lieferscheinnummern und prüft unsichere Vorschläge. Jede Stufe ist einzeln abschaltbar."""
    ai, ocr = AI(), OCR()
    use_ai = ai.cfg["enabled"]
    stats = {"extracted": 0, "ocr_docs": 0, "ocr_nums": 0, "verified": 0, "upgraded": 0, "rejected": 0}
    text = {d["id"]: f"{d.get('title', '')}\n{d.get('content', '')}" for d in invoices + deliveries}
    pl = Paperless() if ocr.enabled else None

    def better_text(doc_id):
        """Text über die OCR neu erkennen lassen (nur wenn OCR eingerichtet)."""
        if not ocr.enabled:
            return None
        try:
            t = ocr.text(pl.download(doc_id))
        except Exception as e:  # noqa: BLE001
            ocr.errors.append(f"PDF {doc_id}: {e}")
            return None
        if t and t.strip():
            stats["ocr_docs"] += 1
            # Paperless-Text behalten und OCR-Text ergänzen – die KI sieht dann beide Fassungen
            text[doc_id] = f"{text[doc_id][:AI_MAX_CHARS // 2]}\n\n[Texterkennung]\n{t}"
        return t

    def find_numbers(doc_id, kind, title):
        """Erst OCR-Text mit der normalen Erkennung, dann die KI. Rückgabe {nummer: quelle}."""
        found = {}
        t = better_text(doc_id)
        if t:
            nums = delivery_numbers({"content": t, "title": title}) if kind == "ls" else extract_numbers(t)
            found.update({n: "OCR" for n in nums})
            stats["ocr_nums"] += len(nums)
        if not found and use_ai:
            found.update({n: "KI" for n in ai.extract_numbers(text[doc_id], kind)})
        return found

    # 1. Lieferscheine ohne erkannte Nummer
    ls_extra = {}
    for d in deliveries:
        if not delivery_numbers(d):
            nums = find_numbers(d["id"], "ls", d.get("title", ""))
            if nums:
                ls_extra[d["id"]] = nums
    # 2. Rechnungen ohne sicheren Lieferschein
    inv_extra = {}
    weak = [g["inv"]["id"] for g in plan["without"]] + \
           [g["inv"]["id"] for g in plan["groups"] if all(i["level"] == "unsicher" for i in g["items"])]
    titles = {i["id"]: i.get("title", "") for i in invoices}
    for inv_id in weak:
        nums = find_numbers(inv_id, "inv", titles.get(inv_id, ""))
        if nums:
            inv_extra[inv_id] = nums
    stats["extracted"] = sum(1 for v in list(ls_extra.values()) + list(inv_extra.values())
                             for src in v.values() if src == "KI")
    if ls_extra or inv_extra:
        plan = build_plan(invoices, deliveries, corr, inv_start, hide_merged, ls_extra, inv_extra)

    # 3. unsichere Vorschläge prüfen lassen
    for g in plan["groups"]:
        for i in g["items"]:
            if i["level"] != "unsicher" or not use_ai:
                continue
            v = ai.verify_pair(text[g["inv"]["id"]], text[i["id"]])
            if not v:
                continue
            stats["verified"] += 1
            pct = round(v["sicherheit"] * 100)
            if v["passt"] and v["sicherheit"] >= 0.75:
                i.update(level="sicher", checked=True, ai=True,
                         reason=f"KI: passt ({pct} %) – {v['begruendung']}")
                stats["upgraded"] += 1
            elif not v["passt"] and v["sicherheit"] >= 0.6:
                i.update(checked=False, ai=True, reason=f"KI: passt nicht ({pct} %) – {v['begruendung']}")
                stats["rejected"] += 1
            else:
                i.update(ai=True, reason=f"{i['reason']} · KI unsicher ({pct} %): {v['begruendung']}")
        g["checked"] = any(i["checked"] for i in g["items"])
    stats["calls"] = ai.calls
    stats["ocr_calls"] = ocr.calls
    stats["use_ai"], stats["use_ocr"] = use_ai, ocr.enabled
    stats["budget_hit"] = (use_ai and ai.calls >= ai.budget) or (ocr.enabled and ocr.calls >= ocr.budget)
    stats["errors"] = (ocr.errors + ai.errors)[-4:]
    return plan, stats


# --------------------------------------------------------------------------- Einstellungen (KI-Zugänge)
_SETTINGS = {"mtime": None, "data": {}}
_SETTINGS_LOCK = threading.Lock()


def _env_bool(name, default=False):
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _env_defaults():
    ai_key = os.environ.get("AI_API_KEY", "").strip()
    ai_prov = os.environ.get("AI_PROVIDER", "openai").strip().lower()
    if os.environ.get("AI_BASE_URL") and ai_prov not in AI_PROVIDERS:
        ai_prov = "custom"
    ai_on = bool(ai_key) or _env_bool("AI_ENABLED")
    ocr_key = os.environ.get("OCR_API_KEY", "").strip()
    ocr_on = bool(ocr_key) or os.environ.get("OCR_PROVIDER", "").strip().lower() == "mistral"
    return {
        "ai": {"provider": ai_prov if ai_on else "off", "api_key": ai_key,
               "model": os.environ.get("AI_MODEL", "").strip(), "base_url": os.environ.get("AI_BASE_URL", "").strip(),
               "max_calls": int(os.environ.get("AI_MAX_CALLS", "30"))},
        "ocr": {"provider": "mistral" if ocr_on else "off", "api_key": ocr_key,
                "model": os.environ.get("OCR_MODEL", "").strip(), "max_calls": int(os.environ.get("OCR_MAX_CALLS", "20"))},
    }


def load_settings():
    """Umgebungsvariablen als Vorbelegung, darüber die in der Oberfläche gespeicherten Werte."""
    with _SETTINGS_LOCK:
        try:
            mtime = os.path.getmtime(SETTINGS_FILE)
        except OSError:
            mtime = None
        if mtime != _SETTINGS["mtime"]:
            data = {}
            if mtime is not None:
                try:
                    with open(SETTINGS_FILE, encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError):
                    data = {}
            _SETTINGS.update(mtime=mtime, data=data)
        stored = _SETTINGS["data"]
    merged = _env_defaults()
    for sec in ("ai", "ocr"):
        merged[sec].update({k: v for k, v in (stored.get(sec) or {}).items() if k in merged[sec]})
    return merged


def save_settings(settings):
    os.makedirs(os.path.dirname(SETTINGS_FILE) or ".", exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, SETTINGS_FILE)
    with _SETTINGS_LOCK:
        _SETTINGS["mtime"] = None


def ai_cfg(settings=None):
    st = settings or load_settings()
    a, o = st["ai"], st["ocr"]
    prov = a.get("provider") or "off"
    if prov == "off":
        return {"enabled": False, "provider": "off", "model": "", "base_url": "", "key": "", "max_calls": 0}
    base = (a.get("base_url") or "").rstrip("/") or AI_PROVIDERS.get(prov, ("", ""))[0]
    model = a.get("model") or AI_PROVIDERS.get(prov, ("", ""))[1]
    key = a.get("api_key") or (o.get("api_key") if prov == "mistral" and o.get("provider") == "mistral" else "")
    enabled = bool(base and model and (key or prov == "custom"))
    return {"enabled": enabled, "provider": prov, "model": model, "base_url": base, "key": key,
            "max_calls": int(a.get("max_calls") or 30)}


def ocr_cfg(settings=None):
    st = settings or load_settings()
    a, o = st["ai"], st["ocr"]
    prov = o.get("provider") or "off"
    if prov not in OCR_PROVIDERS:
        return {"enabled": False, "provider": "off", "model": "", "url": "", "key": "", "max_calls": 0}
    url, default_model = OCR_PROVIDERS[prov]
    key = o.get("api_key") or (a.get("api_key") if a.get("provider") == "mistral" else "")
    return {"enabled": bool(key), "provider": prov, "model": o.get("model") or default_model, "url": url, "key": key,
            "max_calls": int(o.get("max_calls") or 20)}


def mask_key(key):
    return f"••••{key[-4:]}" if key and len(key) > 8 else ("••••" if key else "")


# --------------------------------------------------------------------------- KI
class AIError(RuntimeError):
    pass


class AI:
    """Schlanker Client für OpenAI-kompatible Chat-APIs mit Ergebnis-Cache und Anfragen-Budget."""
    _cache = None
    _lock = threading.Lock()

    def __init__(self, budget=None):
        self.cfg = ai_cfg()
        self.budget = self.cfg["max_calls"] if budget is None else budget
        self.calls = 0
        self.errors = []

    @classmethod
    def _load_cache(cls):
        if cls._cache is None:
            cls._cache = {}
            try:
                with open(AI_CACHE_FILE, encoding="utf-8") as f:
                    cls._cache = json.load(f)
            except (OSError, ValueError):
                pass
        return cls._cache

    @classmethod
    def _save_cache(cls):
        try:
            os.makedirs(os.path.dirname(AI_CACHE_FILE) or ".", exist_ok=True)
            tmp = AI_CACHE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cls._cache, f, ensure_ascii=False)
            os.replace(tmp, AI_CACHE_FILE)
        except OSError:
            pass  # Cache ist optional

    def chat_json(self, system, user, cache_key=None):
        """Schickt eine Anfrage und liefert das JSON-Objekt der Antwort (oder None bei Budget-Ende/Fehler)."""
        if cache_key:
            with self._lock:
                hit = self._load_cache().get(cache_key)
            if hit is not None:
                return hit
        if not self.cfg["enabled"] or self.calls >= self.budget:
            return None
        self.calls += 1
        headers = {"Content-Type": "application/json"}
        if self.cfg["key"]:
            headers["Authorization"] = f"Bearer {self.cfg['key']}"
        body = {"model": self.cfg["model"], "temperature": 0,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        try:
            r = requests.post(f"{self.cfg['base_url']}/chat/completions", json=body, headers=headers,
                              timeout=AI_TIMEOUT)
            if r.status_code >= 400:
                raise AIError(f"HTTP {r.status_code}: {r.text[:200]}")
            text = r.json()["choices"][0]["message"]["content"] or ""
            m = re.search(r"\{.*\}", text, re.S)
            if not m:
                raise AIError(f"keine JSON-Antwort: {text[:120]}")
            result = json.loads(m.group(0))
        except (requests.RequestException, AIError, ValueError, KeyError, IndexError) as e:
            self.errors.append(str(e))
            return None
        if cache_key:
            with self._lock:
                self._load_cache()[cache_key] = result
                self._save_cache()
        return result

    def _key(self, kind, *texts):
        h = hashlib.sha256(("\x00".join([self.cfg["model"], kind, *texts])).encode("utf-8")).hexdigest()
        return f"{kind}:{h[:32]}"

    def extract_numbers(self, text, kind):
        """Liefert die Lieferscheinnummern, die die KI im Dokumenttext findet."""
        text = (text or "")[:AI_MAX_CHARS]
        if len(text.strip()) < 20:
            return []
        system = ("Du analysierst OCR-Text deutscher Geschäftsdokumente. Antworte ausschließlich mit JSON "
                  'im Format {"lieferscheinnummern": ["..."]}.')
        what = ("die eigene Lieferscheinnummer dieses Lieferscheins (auch Beleg-Nr., Liefer-Nr.)" if kind == "ls"
                else "alle Lieferscheinnummern, auf die sich diese Rechnung bezieht (auch Beleg-Nr., Liefer-Nr.)")
        user = (f"Dokumentart: {'Lieferschein' if kind == 'ls' else 'Rechnung'}.\n"
                f"Gib {what} zurück. Keine Rechnungs-, Kunden-, Bestell-, Artikel- oder Auftragsnummern, "
                "keine Datumsangaben. Nummern genau so übernehmen, wie sie im Text stehen. "
                "Wenn keine vorhanden: leere Liste.\n\n--- TEXT ---\n" + text)
        res = self.chat_json(system, user, self._key("ext-" + kind, text))
        out = []
        for n in (res or {}).get("lieferscheinnummern", []) or []:
            n = re.sub(r"\s+", "", str(n)).upper().strip(".,;:")
            if n and sum(c.isdigit() for c in n) >= 4 and n not in out:
                out.append(n)
        return out

    def verify_pair(self, inv_text, ls_text):
        """Prüft, ob Lieferschein und Rechnung zusammengehören. Rückgabe dict oder None."""
        inv_text, ls_text = (inv_text or "")[:AI_MAX_CHARS], (ls_text or "")[:AI_MAX_CHARS]
        system = ("Du prüfst, ob ein Lieferschein zu einer Rechnung gehört. Antworte ausschließlich mit JSON "
                  '{"passt": true|false, "sicherheit": 0.0-1.0, "nummer": "gemeinsame Lieferscheinnummer oder null", '
                  '"begruendung": "kurz, deutsch, max. 20 Wörter"}.')
        user = ("Gehört der Lieferschein zur Rechnung? Achte auf Lieferscheinnummern, Positionen, Mengen, "
                "Lieferadresse, Datum und Lieferant. OCR-Fehler wie LI/11 oder O/0 sind möglich.\n\n"
                "--- RECHNUNG ---\n" + inv_text + "\n\n--- LIEFERSCHEIN ---\n" + ls_text)
        res = self.chat_json(system, user, self._key("pair", inv_text, ls_text))
        if not isinstance(res, dict) or "passt" not in res:
            return None
        try:
            conf = float(res.get("sicherheit", 0))
        except (TypeError, ValueError):
            conf = 0.0
        return {"passt": bool(res.get("passt")), "sicherheit": max(0.0, min(conf, 1.0)),
                "nummer": res.get("nummer") or None, "begruendung": str(res.get("begruendung") or "")[:200]}

    def test(self):
        res = self.chat_json("Antworte ausschließlich mit JSON.", 'Gib {"ok": true} zurück.')
        if res is None:
            raise AIError(self.errors[-1] if self.errors else "keine Antwort")
        return res


class OCR:
    """Texterkennung über Mistral OCR. Liefert den Text je Seite; Ergebnisse werden zwischengespeichert."""

    def __init__(self, budget=None):
        self.cfg = ocr_cfg()
        self.budget = self.cfg["max_calls"] if budget is None else budget
        self.calls = 0
        self.errors = []

    @property
    def enabled(self):
        return self.cfg["enabled"]

    def pages(self, pdf_bytes):
        if not self.cfg["enabled"] or not pdf_bytes:
            return None
        key = "ocr:" + hashlib.sha256(self.cfg["model"].encode() + pdf_bytes).hexdigest()[:40]
        with AI._lock:
            hit = AI._load_cache().get(key)
        if hit is not None:
            return hit
        if self.calls >= self.budget:
            return None
        self.calls += 1
        import base64
        body = {"model": self.cfg["model"], "include_image_base64": False,
                "document": {"type": "document_url",
                             "document_url": "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode()}}
        try:
            r = requests.post(self.cfg["url"], json=body, timeout=max(AI_TIMEOUT, 120),
                              headers={"Authorization": f"Bearer {self.cfg['key']}", "Content-Type": "application/json"})
            if r.status_code >= 400:
                raise AIError(f"HTTP {r.status_code}: {r.text[:200]}")
            pages = sorted(r.json().get("pages", []), key=lambda p: p.get("index", 0))
            result = [str(p.get("markdown") or "")[:8000] for p in pages]
        except (requests.RequestException, AIError, ValueError) as e:
            self.errors.append(f"OCR: {e}")
            return None
        with AI._lock:
            AI._load_cache()[key] = result
            AI._save_cache()
        return result

    def text(self, pdf_bytes):
        p = self.pages(pdf_bytes)
        return None if p is None else "\n\n".join(p)

    def test(self):
        import pypdf
        w = pypdf.PdfWriter()
        w.add_blank_page(width=200, height=200)
        w.add_metadata({"/Title": uuid.uuid4().hex})  # eindeutig, damit nicht aus dem Cache
        buf = io.BytesIO()
        w.write(buf)
        res = self.pages(buf.getvalue())
        if res is None:
            raise AIError(self.errors[-1] if self.errors else "keine Antwort")
        return res


# --------------------------------------------------------------------------- Prüfung zusammengeführter Dokumente
def _norm_page_text(t):
    return re.sub(r"\s+", " ", (t or "").upper()).strip()


def _dhash(img, size=16):
    """Differenz-Hash eines Seitenbildes (size*size Bit) für den Bildvergleich."""
    g = img.convert("L").resize((size + 1, size))
    px = list(g.getdata())
    bits = 0
    for y in range(size):
        row = px[y * (size + 1):(y + 1) * (size + 1)]
        for x in range(size):
            bits = (bits << 1) | (1 if row[x] > row[x + 1] else 0)
    return bits


def analyze_pdf(pdf_bytes):
    """Liefert je Seite: Text, Lieferscheinnummern und Bild-Hash."""
    import pypdf
    import pypdfium2 as pdfium
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    texts = []
    for p in reader.pages:
        try:
            texts.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001
            texts.append("")
    pdf = pdfium.PdfDocument(pdf_bytes)
    pages = []
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            w = page.get_width() or 595
            img = page.render(scale=120 / w).to_pil()
            t = texts[i] if i < len(texts) else ""
            pages.append({"n": i + 1, "text": _norm_page_text(t), "nums": sorted(extract_numbers(t)),
                          "hash": _dhash(img)})
    finally:
        pdf.close()
    return pages


def find_duplicate_pages(pages):
    """Vergleicht alle Seiten eines Dokuments. Rückgabe: {seite: (original_seite, stufe, grund)}."""
    dups = {}
    for j in range(len(pages)):
        best = None
        for i in range(j):
            if pages[i]["n"] in dups:  # nur mit Originalen vergleichen
                continue
            a, b = pages[i]["text"], pages[j]["text"]
            ham = bin(pages[i]["hash"] ^ pages[j]["hash"]).count("1")
            if len(a) >= 40 and len(b) >= 40:
                sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
                if sm.quick_ratio() < 0.85:
                    continue
                ratio = sm.ratio()
                if ratio >= 0.95:
                    cand = (pages[i]["n"], "sicher", f"Text zu {round(ratio * 100)} % gleich wie Seite {pages[i]['n']}")
                elif ratio >= 0.85:
                    cand = (pages[i]["n"], "möglich", f"Text zu {round(ratio * 100)} % ähnlich wie Seite {pages[i]['n']}")
                else:
                    continue
            elif ham <= 6:  # Seiten ohne Textebene: nur Bildvergleich
                cand = (pages[i]["n"], "möglich", f"Seitenbild nahezu gleich wie Seite {pages[i]['n']} (kein Text erkannt)")
            else:
                continue
            if best is None or (cand[1] == "sicher" and best[1] != "sicher"):
                best = cand
        if best:
            dups[pages[j]["n"]] = best
    return dups


def _is_ls_page(page):
    head = page["text"][:400]
    return bool(page["nums"]) and page["n"] > 1 and "RECHNUNG" not in head


AUDIT_JOBS = {}


def _run_audit(job_id, start, end, use_ocr=False):
    job = AUDIT_JOBS[job_id]
    try:
        pl = Paperless()
        t_inv = pl.id_by_name("/api/document_types/", TYPE_INVOICE)
        merged_tag = pl.id_by_name("/api/tags/", MERGED_TAG)
        corr = pl.correspondents()
        docs = [d for d in pl.find_docs({t_inv} if t_inv else set(), start, end) if is_merged_doc(d, merged_tag)]
        docs.sort(key=lambda d: str(d.get("created")), reverse=True)
        if len(docs) > AUDIT_MAX_DOCS:
            job["truncated"] = len(docs)
            docs = docs[:AUDIT_MAX_DOCS]
        job["total"] = len(docs)
        ocr = OCR() if use_ocr else None
        job["ocr_docs"] = 0
        results = []
        for d in docs:
            entry = {**summarize(d, corr), "pages": [], "dups": {}, "error": None}
            try:
                pdf = pl.download(d["id"])
                with open(os.path.join(job["dir"], f"{d['id']}.pdf"), "wb") as f:
                    f.write(pdf)
                pages = analyze_pdf(pdf)
                if ocr and any(len(p["text"]) < 40 for p in pages):
                    ocr_pages = ocr.pages(pdf)
                    if ocr_pages:
                        job["ocr_docs"] += 1
                        for p in pages:
                            if len(p["text"]) < 40 and p["n"] - 1 < len(ocr_pages):
                                t = ocr_pages[p["n"] - 1]
                                p["text"], p["nums"], p["ocr"] = _norm_page_text(t), sorted(extract_numbers(t)), True
                entry["pages"] = [{"n": p["n"], "nums": p["nums"], "ls": _is_ls_page(p), "ocr": p.get("ocr", False)}
                                  for p in pages]
                entry["dups"] = find_duplicate_pages(pages)
                entry["_pages"] = pages
            except Exception as e:  # noqa: BLE001
                entry["error"] = str(e)[:200]
            results.append(entry)
            job["done"] += 1
        # dokumentübergreifend: dieselbe Lieferscheinnummer auf Lieferschein-Seiten mehrerer Dokumente
        where = {}
        for e in results:
            for p in e.get("_pages", []):
                if _is_ls_page(p):
                    for n in p["nums"]:
                        where.setdefault(ocr_norm(n), []).append((e["id"], p["n"], n))
        for e in results:
            e["cross"] = []
            for key, places in where.items():
                docs_with = {x[0] for x in places}
                if e["id"] in docs_with and len(docs_with) > 1:
                    mine = sorted({x[1] for x in places if x[0] == e["id"]})
                    others = {}
                    for x in places:
                        if x[0] != e["id"]:
                            others.setdefault(x[0], set()).add(x[1])
                    others = [(k, sorted(v)) for k, v in others.items()]
                    e["cross"].append({"num": places[0][2], "pages": mine, "others": others})
            # gleiche Nummer auf nicht zusammenhängenden Lieferschein-Seiten im selben Dokument
            e["repeat"] = []
            for key, places in where.items():
                mine = sorted({x[1] for x in places if x[0] == e["id"]})
                if len(mine) > 1 and any(b - a > 1 for a, b in zip(mine, mine[1:])):
                    e["repeat"].append({"num": places[0][2], "pages": mine})
            e.pop("_pages", None)
            e["issues"] = len(e["dups"]) + len(e["cross"]) + len(e["repeat"]) + (1 if e["error"] else 0)
        if ocr:
            job["ocr_errors"] = ocr.errors[-3:]
        titles = {e["id"]: e["title"] for e in results}
        job["titles"] = titles
        job["results"] = results
        job["status"] = "fertig"
    except Exception as e:  # noqa: BLE001
        job["status"] = "fehler"
        job["error"] = str(e)[:300]


def _cleanup_audit_jobs(ttl=3 * 3600):
    now = time.time()
    for k in [k for k, v in AUDIT_JOBS.items() if now - v["ts"] > ttl]:
        shutil.rmtree(AUDIT_JOBS[k]["dir"], ignore_errors=True)
        AUDIT_JOBS.pop(k, None)


# --------------------------------------------------------------------------- Duplikate
DUP_MAX_DOCS = int(os.environ.get("DUP_MAX_DOCS", "3000"))
_WORD_RE = re.compile(r"[A-Z0-9ÄÖÜß]{2,}")


def shingles(text, n=5):
    """Wortgruppen (n Wörter) eines Textes als Menge von Hashes – unempfindlich gegen Zeilenumbrüche/Abstände."""
    words = _WORD_RE.findall(ocr_norm(text or ""))
    if len(words) < n:
        return {hash(" ".join(words))} if words else set()
    return {hash(" ".join(words[i:i + n])) for i in range(len(words) - n + 1)}


def containment(a, b):
    """Anteil von a, der in b enthalten ist (0..1)."""
    return len(a & b) / len(a) if a else 0.0


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def find_duplicates(docs, merged_ids, type_names, corr):
    """Findet Duplikate. Rückgabe: Liste von Funden {keep, drop:[{doc, reason, score, sure}], kind}."""
    sh = {d["id"]: shingles(f"{d.get('content', '')}") for d in docs}
    by_id = {d["id"]: d for d in docs}
    index = {}
    for did, s in sh.items():
        for h in s:
            index.setdefault(h, []).append(did)
    # Kandidatenpaare über gemeinsame Wortgruppen finden (statt alle Paare zu vergleichen)
    pairs = {}
    for ids in index.values():
        if len(ids) > 60:  # Allerweltsfloskeln (Briefkopf, AGB) überspringen
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                k = (ids[i], ids[j]) if ids[i] < ids[j] else (ids[j], ids[i])
                pairs[k] = pairs.get(k, 0) + 1
    raw = []
    for (a, b), shared in pairs.items():
        if shared < 3:
            continue
        sa, sb = sh[a], sh[b]
        da, db = by_id[a], by_id[b]
        ca, cb = da.get("correspondent"), db.get("correspondent")
        corr_conflict = ca is not None and cb is not None and ca != cb
        am, bm = a in merged_ids, b in merged_ids
        cont_ab, cont_ba, jac = containment(sa, sb), containment(sb, sa), jaccard(sa, sb)
        if am != bm:  # Original vs. zusammengeführtes Dokument
            orig, mer, c = (a, b, cont_ab) if not am else (b, a, cont_ba)
            if c >= 0.8:
                kind = "Original noch vorhanden"
                reason = (f"{type_names.get(by_id[orig].get('document_type'), 'Dokument')} steckt zu {round(c * 100)} % "
                          f"in der zusammengeführten Rechnung")
                raw.append((c, mer, orig, kind, reason, c >= 0.9 and not corr_conflict))
        elif am and bm:  # zwei zusammengeführte Rechnungen
            if jac >= 0.9:
                keep, drop = _prefer(da, db)
                raw.append((jac, keep["id"], drop["id"], "Rechnung doppelt zusammengeführt",
                            f"beide Fassungen zu {round(jac * 100)} % gleich", not corr_conflict))
            elif max(cont_ab, cont_ba) >= 0.9:
                small, big = (a, b) if cont_ab >= cont_ba else (b, a)
                c = max(cont_ab, cont_ba)
                raw.append((c, big, small, "Rechnung doppelt zusammengeführt",
                            f"kleinere Fassung steckt zu {round(c * 100)} % in der größeren", not corr_conflict))
            elif min(len(sa), len(sb)) and max(cont_ab, cont_ba) >= 0.5 and not corr_conflict:
                keep, drop = _prefer(da, db)
                raw.append((max(cont_ab, cont_ba), keep["id"], drop["id"], "teilweise gleich – bitte prüfen",
                            f"zu {round(max(cont_ab, cont_ba) * 100)} % gleich, enthalten aber verschiedene Seiten",
                            False))
        else:  # zwei normale Dokumente
            same_type = da.get("document_type") == db.get("document_type")
            if jac >= 0.9 and same_type:
                keep, drop = _prefer(da, db)
                raw.append((jac, keep["id"], drop["id"], "doppeltes Dokument",
                            f"Inhalt zu {round(jac * 100)} % gleich", jac >= 0.95 and not corr_conflict))
    # Entscheidungen: beste zuerst; ein Dokument wird höchstens einmal zum Löschen vorgeschlagen und
    # ein zu behaltendes Dokument nie gelöscht
    raw.sort(key=lambda r: -r[0])
    dropped, kept, findings = set(), set(), {}
    for score, keep, drop, kind, reason, sure in raw:
        if drop in dropped or drop in kept or keep in dropped:
            continue
        dropped.add(drop)
        kept.add(keep)
        f = findings.setdefault(keep, {"keep": keep, "drop": []})
        f["drop"].append({"id": drop, "kind": kind, "reason": reason, "score": round(score * 100), "sure": sure})
    out = []
    for f in findings.values():
        f["drop"].sort(key=lambda x: -x["score"])
        out.append(f)
    out.sort(key=lambda f: str(by_id[f["keep"]].get("created")), reverse=True)
    return out


def _prefer(a, b):
    """Welches von zwei gleichen Dokumenten bleibt: mehr Seiten, sonst mehr Tags, sonst das ältere (kleinere ID)."""
    key = lambda d: (d.get("page_count") or 0, len(d.get("tags") or []), -d["id"])  # noqa: E731
    return (a, b) if key(a) >= key(b) else (b, a)


def mark_already_merged(invoices, deliveries, threshold=0.8):
    """Markiert Rechnungen und Lieferscheine, deren Inhalt schon in einer zusammengeführten Rechnung steckt.
    Rechnungen gelten dann als bereits ergänzt (werden nicht erneut angeboten). Rückgabe: (anzahl, {ls_id: (ls, rechnung)})."""
    merged = [i for i in invoices if i.get("_merged")]
    if not merged:
        return 0, {}
    msh = {m["id"]: shingles(m.get("content")) for m in merged}

    def owner(doc):
        s = shingles(doc.get("content"))
        if len(s) < 8:  # zu wenig Text für einen sicheren Vergleich
            return None
        for m in merged:
            if m["id"] == doc["id"]:
                continue
            if doc.get("correspondent") and m.get("correspondent") and doc["correspondent"] != m["correspondent"]:
                continue
            if containment(s, msh[m["id"]]) >= threshold:
                return m["id"]
        return None
    count = 0
    for inv in invoices:
        if not inv.get("_merged") and owner(inv):
            inv["_merged"] = True
            count += 1
    ls_dups = {}
    for ls in deliveries:
        o = owner(ls)
        if o:
            ls_dups[ls["id"]] = (ls, o)
    return count, ls_dups


def _run_dups(job_id, start, end, type_ids):
    job = AUDIT_JOBS[job_id]
    try:
        pl = Paperless()
        types = {t["id"]: t["name"] for t in pl.doc_types()}
        merged_tag = pl.id_by_name("/api/tags/", MERGED_TAG)
        corr = pl.correspondents()
        job["phase"] = "Dokumente laden"
        docs = pl.find_docs(type_ids, start, end)
        if len(docs) > DUP_MAX_DOCS:
            job["truncated"] = len(docs)
            docs = sorted(docs, key=lambda d: str(d.get("created")), reverse=True)[:DUP_MAX_DOCS]
        job["total"] = len(docs)
        job["phase"] = "vergleichen"
        merged_ids = {d["id"] for d in docs if is_merged_doc(d, merged_tag)}
        findings = find_duplicates(docs, merged_ids, types, corr)
        by_id = {d["id"]: d for d in docs}

        def info(did):
            d = by_id[did]
            return {**summarize(d, corr), "type": types.get(d.get("document_type"), "–"),
                    "pages": d.get("page_count"), "merged": did in merged_ids,
                    "tag_count": len(d.get("tags") or [])}
        for f in findings:
            f["keep_doc"] = info(f["keep"])
            for x in f["drop"]:
                x["doc"] = info(x["id"])
        job["done"] = len(docs)
        job["findings"] = findings
        job["checked"] = len(docs)
        job["status"] = "fertig"
    except Exception as e:  # noqa: BLE001
        job["status"] = "fehler"
        job["error"] = str(e)[:300]


# --------------------------------------------------------------------------- Aufräumen: Korrespondenten
_LEGAL_FORMS = {"gmbh", "mbh", "co", "kg", "ag", "ug", "haftungsbeschraenkt", "ek", "ohg", "gbr", "se", "kgaa", "eg",
                "ev", "inc", "ltd", "llc", "limited", "corp", "sarl", "bv", "nv", "spa", "srl", "und", "the", "fa",
                "firma", "gesellschaft", "mit", "beschraenkter", "haftung", "kommanditgesellschaft"}
_GENERIC = {"elektro", "bau", "service", "services", "handel", "technik", "gruppe", "group", "holding", "deutschland",
            "germany", "online", "shop", "versand", "partner", "solutions", "systeme", "systems", "international",
            "stadt", "gemeinde", "verwaltung", "bank", "sparkasse", "versicherung", "autohaus", "apotheke", "praxis"}
_TRANSLIT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "é": "e", "è": "e", "á": "a", "à": "a"})


def corr_tokens(name):
    """Name vereinheitlichen: klein, Umlaute umschreiben, Satzzeichen und Rechtsformen entfernen."""
    s = (name or "").lower().translate(_TRANSLIT)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return [t for t in s.split() if t not in _LEGAL_FORMS and len(t) > 1]


def corr_similarity(a, b):
    """Rückgabe (score, grund) für zwei Korrespondenten-Namen oder (0, '')."""
    ta, tb = corr_tokens(a), corr_tokens(b)
    ka, kb = " ".join(ta), " ".join(tb)
    if not ka or not kb:
        return 0, ""
    if ka == kb:
        return 1.0, "gleicher Name (Schreibweise/Rechtsform)"
    if "".join(ta) == "".join(tb):
        return 0.97, "gleicher Name (Leerzeichen/Bindestriche)"
    ratio = difflib.SequenceMatcher(None, ka, kb).ratio()
    if ratio >= 0.88 and min(len(ka), len(kb)) >= 5:
        return ratio, f"sehr ähnlich geschrieben ({round(ratio * 100)} %)"
    small, big = (set(ta), set(tb)) if len(ta) <= len(tb) else (set(tb), set(ta))
    if small < big and any(len(t) >= 4 and t not in _GENERIC for t in small):
        return 0.8, "Name in anderem enthalten"
    return 0, ""


def find_corr_groups(corrs):
    """Gruppiert doppelte/ähnliche Korrespondenten. corrs: Liste aus der Paperless-API."""
    ids = [c["id"] for c in corrs]
    by_id = {c["id"]: c for c in corrs}
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    edges = {}
    for i in range(len(corrs)):
        for j in range(i + 1, len(corrs)):
            a, b = corrs[i], corrs[j]
            score, why = corr_similarity(a["name"], b["name"])
            if score >= 0.8:
                edges[(a["id"], b["id"])] = (score, why)
                parent[find(a["id"])] = find(b["id"])
    groups = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        sure = all(edges.get((x, y), edges.get((y, x), (1, "")))[0] >= 0.97
                   for k, x in enumerate(members) for y in members[k + 1:]
                   if (x, y) in edges or (y, x) in edges)
        reasons = sorted({w for (x, y), (sc, w) in edges.items() if x in members and y in members})
        # Ziel: meiste Dokumente, sonst kürzester Name, sonst kleinste ID
        target = max(members, key=lambda m: (by_id[m].get("document_count") or 0, -len(by_id[m]["name"]), -m))
        out.append({"members": sorted(members, key=lambda m: (-(by_id[m].get("document_count") or 0), by_id[m]["name"])),
                    "target": target, "sure": sure, "reasons": reasons})
    out.sort(key=lambda g: (not g["sure"], by_id[g["target"]]["name"].lower()))
    return out


# --------------------------------------------------------------------------- Suche nach Rechnungen zu Lieferscheinen
def search_variants(num):
    """Suchbegriffe für die Textsuche in Paperless: Nummer, OCR-Form und die letzten 5 Ziffern."""
    out = []
    for v in (num.upper(), ocr_norm(num)):
        if len(v) >= 4 and v not in out:
            out.append(v)
    tail = ocr_norm(num)[-5:]
    if len(ocr_norm(num)) >= 7 and tail.isdigit() and tail not in out:
        out.append(tail)  # fängt Fälle wie "4LI123456" in der Rechnung bei Suche nach "411123456"
    return out


def search_invoices_for(pl, ls, nums, type_ids, before, after, unlimited, same_corr, limit=25):
    """Sucht Dokumente, deren Text eine der Nummern enthält. Rückgabe: Liste (dokument, nummer, ocr_tolerant)."""
    ldate = dt.date.fromisoformat(ls["date"])
    start = None if unlimited else ldate - dt.timedelta(days=before)
    end = None if unlimited else ldate + dt.timedelta(days=after)
    corr_id = ls.get("corr_id") if same_corr else None
    cands = {}
    if same_corr and corr_id:
        for d in pl.find_docs(type_ids, start, end, corr_id=corr_id):
            cands[d["id"]] = d
    else:
        for n in nums:
            for v in search_variants(n):
                for d in pl.find_docs(type_ids, start, end, content=v):
                    cands[d["id"]] = d
    hits = []
    for d in cands.values():
        if d["id"] == ls["id"]:
            continue
        text = f"{d.get('title', '')}\n{d.get('content', '')}".upper()
        for n in nums:
            if contains_number(text, n.upper()):
                hits.append((d, n, False))
                break
            if contains_number(text.translate(OCR_MAP), ocr_norm(n)):
                hits.append((d, n, True))
                break
    hits.sort(key=lambda h: abs((_date(h[0]) - ldate).days))
    return hits[:limit]


# --------------------------------------------------------------------------- Web-Oberfläche
# --------------------------------------------------------------------------- Anmeldung
# Benutzer wird beim ersten Aufruf in der Oberfläche angelegt und in DATA_DIR/benutzer.json gespeichert
# (Passwort nur als Hash). Alternativ fest über APP_USER/APP_PASSWORD. LOGIN_REQUIRED=false schaltet die
# Anmeldung ab (nur sinnvoll, wenn ein vorgeschalteter Proxy wie Authentik den Zugang schützt).
USERS_FILE = os.environ.get("USERS_FILE", os.path.join(DATA_DIR, "benutzer.json"))
LOGIN_REQUIRED = os.environ.get("LOGIN_REQUIRED", "true").strip().lower() not in ("0", "false", "no", "off")
_FAILED = {}
_FAILED_LOCK = threading.Lock()


def _secret_key():
    env = os.environ.get("SECRET_KEY", "").strip()
    if env:
        return env
    path = os.path.join(DATA_DIR, "secret.key")
    try:
        with open(path, encoding="utf-8") as f:
            key = f.read().strip()
            if len(key) >= 32:
                return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(key)
        os.chmod(path, 0o600)
    except OSError:
        pass  # ohne beschreibbares Datenverzeichnis gilt der Schlüssel nur bis zum Neustart
    return key


app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_NAME="paperless_merge", PERMANENT_SESSION_LIFETIME=dt.timedelta(days=30))


def load_account():
    """Das eine Benutzerkonto: aus APP_USER/APP_PASSWORD oder aus benutzer.json. None = noch keins angelegt."""
    if APP_USER and APP_PASSWORD:
        return {"username": APP_USER, "env": True,
                "version": hashlib.sha256(APP_PASSWORD.encode()).hexdigest()[:16]}
    try:
        with open(USERS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("username") and data.get("password_hash"):
            return {"username": data["username"], "password_hash": data["password_hash"], "env": False,
                    "version": hashlib.sha256(data["password_hash"].encode()).hexdigest()[:16]}
    except (OSError, ValueError):
        pass
    return None


def save_account(username, password):
    os.makedirs(os.path.dirname(USERS_FILE) or ".", exist_ok=True)
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"username": username, "password_hash": generate_password_hash(password),
                   "created": dt.datetime.now().isoformat(timespec="seconds")}, f, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, USERS_FILE)


def check_credentials(account, username, password):
    if not account or not hmac.compare_digest(username.strip().lower(), account["username"].lower()):
        return False
    if account["env"]:
        return hmac.compare_digest(password, APP_PASSWORD)
    return check_password_hash(account["password_hash"], password)


def validate_new_account(username, password, password2):
    if not re.fullmatch(r"[A-Za-z0-9._@-]{3,40}", username or ""):
        return "Benutzername: 3–40 Zeichen, erlaubt sind Buchstaben, Ziffern und . _ - @"
    if len(password or "") < 8:
        return "Das Passwort muss mindestens 8 Zeichen lang sein."
    if password != password2:
        return "Die beiden Passwörter stimmen nicht überein."
    return None


def _too_many_failures(ip):
    now = time.time()
    with _FAILED_LOCK:
        recent = [t for t in _FAILED.get(ip, []) if now - t < 600]
        _FAILED[ip] = recent
        return len(recent) >= 5


def _note_failure(ip):
    with _FAILED_LOCK:
        _FAILED.setdefault(ip, []).append(time.time())


def _safe_next(target):
    return target if target and target.startswith("/") and not target.startswith("//") else None


def logged_in_user():
    if not LOGIN_REQUIRED:
        return None
    acc = load_account()
    if acc and session.get("user") == acc["username"] and session.get("v") == acc["version"]:
        return acc["username"]
    return None


def require_auth(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not LOGIN_REQUIRED:
            return f(*a, **kw)
        acc = load_account()
        wants_html = request.method == "GET" and "text/html" in (request.headers.get("Accept") or "text/html")
        if acc is None:
            return redirect(url_for("setup")) if wants_html or request.method == "POST" else ("", 401)
        if session.get("user") != acc["username"] or session.get("v") != acc["version"]:
            if not wants_html and request.method != "POST":
                return "", 401
            return redirect(url_for("login", next=request.full_path.rstrip("?") if request.method == "GET" else None))
        return f(*a, **kw)
    return wrapper


BASE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Paperless Merge</title><style>
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
input[type=date],input[type=number],input[type=search],input[type=text],input[type=password],select{width:100%;padding:10px 12px;font:inherit;color:var(--text);
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
.ctab{width:100%;border-collapse:collapse;margin-top:10px;font-size:.9rem}
.ctab th{text-align:left;font-size:.75rem;color:var(--muted);font-weight:600;padding:4px 6px;border-bottom:1px solid var(--line)}
.ctab td{padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}
.ctab input{width:18px;height:18px;accent-color:var(--accent)}
select[multiple]{min-height:180px}
.dupcase{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.4fr);gap:18px}
@media (max-width:800px){.dupcase{grid-template-columns:1fr}}
.dupcol.keep{border-right:1px solid var(--line);padding-right:14px}
@media (max-width:800px){.dupcol.keep{border-right:0;padding-right:0;border-bottom:1px solid var(--line);padding-bottom:12px}}
.duprow{display:flex;gap:12px;align-items:flex-start;padding:10px;border-radius:10px;cursor:pointer}
.duprow:hover{background:var(--surface2)}
.duprow.gone{opacity:.5}
.dc{display:flex;gap:12px;align-items:center;margin-top:8px}
.authbox{max-width:440px;margin:6vh auto 0}
.authbox h3{font-size:1.25rem}
.authbox input{margin-bottom:12px}
code{word-break:break-all}
.pagegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px;margin-top:12px}
.pg{border:2px solid var(--line);border-radius:10px;padding:6px;background:var(--surface2);display:flex;flex-direction:column;gap:4px;cursor:pointer}
.pg img{width:100%;aspect-ratio:1/1.414;object-fit:cover;border-radius:6px;background:#fff}
.pg.dup{border-color:var(--bad);background:var(--bad-bg)}
.pg.maybe{border-color:var(--warn);background:var(--warn-bg)}
.pgl{font-size:.78rem}.pgd{font-size:.72rem;color:var(--muted)}.pgc{font-size:.8rem;display:flex;gap:6px;align-items:center}
.ai{display:inline-block;font-size:.68rem;font-weight:700;padding:0 6px;border-radius:6px;background:var(--accent-soft);color:var(--accent);margin-left:4px}
.ulist{display:flex;flex-direction:column;gap:2px;margin-top:8px}
.urow{display:flex;gap:12px;align-items:center;padding:8px 6px;border-radius:10px;cursor:pointer}
.urow:hover{background:var(--surface2)}
.urow .num{margin-left:6px}
.lshead{display:flex;gap:14px;align-items:flex-start}
.nomatch{padding:12px 0 4px 32px}
.nums{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:6px;font-size:.82rem}
.nums .nl{color:var(--muted);margin-right:2px}
.num{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:.8rem;padding:1px 8px;border-radius:6px;
  background:var(--surface2);border:1px solid var(--line);color:var(--text)}
.num.hit{background:var(--ok-bg);border-color:var(--ok);color:var(--ok);font-weight:600}
.num.hit::before{content:"✓ "}
.none{color:var(--muted);font-style:italic;font-size:.8rem}
li .num{margin-left:6px}
header{flex-wrap:wrap}
.home{display:flex;align-items:center;gap:12px;color:inherit;text-decoration:none}
.hnav{margin-left:auto;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.hbtn{padding:7px 14px;border-radius:10px;border:1px solid var(--line);background:var(--surface2);color:var(--text);
  text-decoration:none;font-size:.88rem;font-weight:600}
.hbtn:hover{border-color:var(--accent);color:var(--accent)}
.hbtn.on{background:var(--accent-soft);border-color:var(--accent);color:var(--accent)}
.subnav{display:flex;gap:4px;flex-wrap:wrap;padding:0 clamp(12px,2vw,32px);border-bottom:1px solid var(--line);background:var(--surface)}
.subnav a{padding:10px 14px;text-decoration:none;color:var(--muted);font-weight:600;font-size:.9rem;border-bottom:2px solid transparent}
.subnav a.on{color:var(--accent);border-bottom-color:var(--accent)}
.subnav a:hover{color:var(--text)}
.ver{padding:4px 12px;border-radius:999px;background:var(--accent-soft);color:var(--accent);font-size:.8rem;font-weight:600}
ul{padding-left:18px}
</style></head><body>
<header><a href="{{ url_for('index') }}" class="home"><div class="logo">PM</div><div><h1>Paperless Merge</h1><small>Zusammenführen &amp; Aufräumen</small></div></a>
<nav class="hnav">{% if show_nav %}<a class="hbtn {{ 'on' if area == 'merge' }}" href="{{ url_for('index') }}">📎 Zusammenführen</a>
<a class="hbtn {{ 'on' if area == 'clean' }}" href="{{ url_for('cleanup_home') }}">🧹 Aufräumen</a>
<a class="hbtn {{ 'on' if area == 'settings' }}" href="{{ url_for('settings') }}">⚙ Einstellungen</a>
{% if paperless_link %}<a class="hbtn" href="{{ paperless_link }}" target="_blank" rel="noopener">Paperless öffnen ↗</a>{% endif %}
{% if current_user %}<a class="hbtn" href="{{ url_for('logout') }}" title="Abmelden">👤 {{ current_user }} · Abmelden</a>{% endif %}{% endif %}
<span class="ver">v{{ version }}</span></nav></header>
{% if show_nav and tabs %}<div class="subnav">{% for ep, label in tabs %}<a class="{{ 'on' if ep == active_tab }}" href="{{ url_for(ep) }}">{{ label }}</a>{% endfor %}</div>{% endif %}
<main>
{% block body %}{% endblock %}</main></body></html>"""


# Bereiche der Oberfläche: Zusammenführen und Aufräumen sind getrennt, jeweils mit eigenen Unterseiten
AREAS = {
    "merge": {"index", "preview", "review", "execute", "search_ls", "search_merge", "mark_done", "audit", "audit_start",
              "audit_job", "audit_fix", "cleanup_titles"},
    "clean": {"cleanup_home", "corr_check", "corr_apply", "dups", "dups_start", "dups_job", "dups_delete"},
    "settings": {"settings", "change_password"},
}
AREA_TABS = {
    "merge": [("index", "📎 Rechnung + Lieferschein"), ("audit", "🔎 Zusammengeführte prüfen")],
    "clean": [("cleanup_home", "Übersicht"), ("corr_check", "👥 Korrespondenten"), ("dups", "📄 Duplikate")],
}
TAB_EPS = {  # welche Seiten zu welchem Reiter gehören
    "index": {"index", "preview", "review", "execute", "search_ls", "search_merge", "mark_done", "cleanup_titles"},
    "audit": {"audit", "audit_start", "audit_job", "audit_fix"},
    "cleanup_home": {"cleanup_home"},
    "corr_check": {"corr_check", "corr_apply"},
    "dups": {"dups", "dups_start", "dups_job", "dups_delete"},
}


def page(body, **ctx):
    ctx.setdefault("version", VERSION)
    ctx.setdefault("paperless_link", PAPERLESS_BROWSER_URL)
    ctx.setdefault("current_user", logged_in_user())
    ctx.setdefault("show_nav", True)
    ep = request.endpoint or ""
    area = next((a for a, eps in AREAS.items() if ep in eps), "")
    ctx.setdefault("area", area)
    ctx.setdefault("tabs", AREA_TABS.get(area, []))
    ctx.setdefault("active_tab", next((t for t, eps in TAB_EPS.items() if ep in eps), ep))
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
   <p class="meta">Nach dem Zusammenführen bleibt in Paperless nur das neue Dokument übrig – Rechnung und Lieferscheine
     wandern in den Papierkorb (dort wiederherstellbar). So entstehen keine Duplikate.</p>
   <label class="switch"><input type="checkbox" name="hide_merged" value="1" checked>
     Bereits ergänzte Rechnungen ausblenden</label>
   {% if ai.enabled or ocr.enabled %}<label class="switch"><input type="checkbox" name="use_ai" value="1" checked>
     🤖 KI-Unterstützung nutzen</label>
   <p class="meta">{% if ocr.enabled %}Texterkennung: <code>{{ ocr.model }}</code>{% endif %}
     {% if ocr.enabled and ai.enabled %} · {% endif %}{% if ai.enabled %}Auswertung: <code>{{ ai.model }}</code>{% endif %}
     · <a href="{{ url_for('settings') }}">Einstellungen</a></p>
   {% else %}<p class="meta">🤖 KI-Unterstützung ist nicht eingerichtet – <a href="{{ url_for('settings') }}">jetzt einrichten</a>.</p>{% endif %}
   <p class="meta">Verwendete Lieferscheine bekommen das Tag „{{ done_tag }}“, die Rechnungen das Tag „{{ merged_tag }}“.
     Beide werden beim nächsten Lauf nicht mehr angeboten – auch ältere zusammengeführte Rechnungen werden erkannt.</p>
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
{% if info %}<div class="card ok">{{ info }}</div>{% endif %}
{% if ai_stats %}<div class="card {{ 'err' if ai_stats.errors and not ai_stats.calls and not ai_stats.ocr_calls }}">🤖 <b>KI-Unterstützung:</b>
  {% if ai_stats.use_ocr %}OCR: {{ ai_stats.ocr_docs }} Dokument(e) neu erkannt, {{ ai_stats.ocr_nums }} Nummer(n) gefunden ({{ ai_stats.ocr_calls }} Anfrage(n)){% endif %}
  {% if ai_stats.use_ocr and ai_stats.use_ai %} · {% endif %}
  {% if ai_stats.use_ai %}Auswertung: {{ ai_stats.calls }} Anfrage(n) · {{ ai_stats.extracted }} Nummer(n) ergänzt · {{ ai_stats.verified }} unsichere Vorschläge geprüft
  ({{ ai_stats.upgraded }} bestätigt, {{ ai_stats.rejected }} verworfen){% endif %}
  {% if ai_stats.budget_hit %}<br><span class="meta">Höchstzahl an Anfragen erreicht (siehe Einstellungen) – nicht alles wurde geprüft.</span>{% endif %}
  {% for e in ai_stats.errors %}<br><span class="meta" style="color:var(--bad)">Fehler: {{ e }}</span>{% endfor %}</div>{% endif %}
<div class="card"><h3>2 · Zuordnungen prüfen</h3>
  <div class="meta">Zeitraum {{ start.strftime('%d.%m.%Y') }} – {{ end.strftime('%d.%m.%Y') }}</div>
  <div class="stats">
    <div class="stat"><b>{{ plan.groups|length }}</b><span class="meta">mit Vorschlag</span></div>
    <div class="stat"><b>{{ plan.without|length }}</b><span class="meta">Rechnungen ohne</span></div>
    <div class="stat"><b>{{ plan.unassigned|length }}</b><span class="meta">Lieferscheine frei</span></div>
    {% if plan.hidden %}<div class="stat"><b>{{ plan.hidden }}</b><span class="meta">bereits ergänzt (ausgeblendet)</span></div>{% endif %}
    {% if plan.contained %}<div class="stat"><b>{{ plan.contained|length }}</b><span class="meta">LS schon enthalten</span></div>{% endif %}
  </div>
  {% if plan.orig_dups or plan.contained %}<p class="meta">⚠ In Paperless liegen noch Originale, die schon in einer zusammengeführten
    Rechnung stecken ({{ plan.orig_dups }} Rechnung(en), {{ plan.contained|length }} Lieferschein(e)). Sie werden nicht erneut
    zusammengeführt. Aufräumen über <a href="{{ url_for('dups') }}">🧹 Duplikate</a>.</p>{% endif %}
  <div>
  </div>
  Originale: <b>werden nach dem Zusammenführen in den Papierkorb verschoben</b><br>
  Korrespondenten: {% for c in sel_corrs %}<span class="pill tag">{{ c }}</span>{% else %}<span class="meta">alle</span>{% endfor %}<br>
  Zusätzliche Tags: {% for t in extra_tags %}<span class="pill" style="background:{{ t.color or '#6366f1' }};color:{{ t.text_color or '#fff' }}">{{ t.name }}</span>{% else %}<span class="meta">keine</span>{% endfor %}
  <p class="meta">Reihenfolge im neuen PDF: Rechnung zuerst, danach die Lieferscheine nach Datum.
  Das neue Dokument übernimmt Titel, Korrespondent und Dokumententyp „{{ type_invoice }}“ der Rechnung sowie <b>alle Tags der Rechnung und der zugehörigen Lieferscheine</b>.
  <span class="badge sicher">sicher</span> Nummer gefunden ·
  <span class="badge mittel">mittel</span> Nummer gefunden, aber Auffälligkeit ·
  <span class="badge unsicher">unsicher</span> nur Korrespondent/Datum, standardmäßig abgewählt ·
  <span class="num hit">Nummer</span> = darüber wurde zugeordnet</p>
</div>

{% macro group(g) %}
<div class="card">
  <div class="grp-head">
    <input type="checkbox" name="inv" value="{{ g.inv.id }}" class="inv" {{ 'checked' if g.checked }}>
    <a href="{{ link(g.inv.id) }}" target="_blank"><img class="th" loading="lazy" src="{{ url_for('thumb', doc_id=g.inv.id) }}"></a>
    <div><span class="tag">{{ type_invoice }}</span> {% if g.inv.merged %}<span class="badge mittel">bereits ergänzt</span> {% endif %}<b>{{ g.inv.title }}</b><br>
      <span class="meta">{{ g.inv.date|de }} · {{ g.inv.correspondent }}{% if g.inv.asn %} · ASN {{ g.inv.asn }}{% endif %} ·
      <a href="{{ link(g.inv.id) }}" target="_blank">{{ "in Paperless öffnen" if public else "PDF ansehen" }}</a></span>
      <div class="nums"><span class="nl">Lieferschein-Nr. in der Rechnung:</span>
        {% for x in g.inv.nums %}<span class="num {{ 'hit' if x.hit }}">{{ x.n }}{% if x.ai %}<span class="ai">{{ x.ai }}</span>{% endif %}</span>{% else %}<span class="none">keine gefunden</span>{% endfor %}</div></div>
  </div>
  {% if g['items'] %}<div class="items">{% endif %}
  {% for i in g['items'] %}
  <div class="row">
    <input type="checkbox" name="ls_{{ g.inv.id }}" value="{{ i.id }}" class="ls" data-inv="{{ g.inv.id }}" {{ 'checked' if i.checked }}>
    <a href="{{ link(i.id) }}" target="_blank"><img class="th" loading="lazy" src="{{ url_for('thumb', doc_id=i.id) }}"></a>
    <div><span class="badge {{ i.level }}">{{ i.level }}</span>{% if i.ai %}<span class="ai">🤖 KI</span>{% endif %} <b>{{ i.title }}</b><br>
      <span class="meta">{{ i.date|de }} · {{ i.correspondent }} · {{ i.reason }}</span>
      <div class="nums"><span class="nl">Erkannte Nr.:</span>
        {% for x in i.nums %}<span class="num {{ 'hit' if x.hit }}">{{ x.n }}{% if x.ai %}<span class="ai">{{ x.ai }}</span>{% endif %}</span>{% else %}<span class="none">keine erkannt</span>{% endfor %}
        {% if i.level == 'unsicher' %}<button type="submit" class="linkbtn" formaction="{{ url_for('search_ls') }}"
          name="find_ls" value="{{ i.id }}" data-search1>🔍 Rechnung in Paperless suchen</button>{% endif %}</div></div>
  </div>
  {% endfor %}
  {% if g['items'] %}</div>{% endif %}
  {% if plan.unassigned %}
  <p class="meta" style="margin:12px 0 0 32px">Weitere Lieferscheine manuell hinzufügen (Strg/Cmd für Mehrfachauswahl):</p>
  <select name="add_{{ g.inv.id }}" multiple size="3" class="add" data-inv="{{ g.inv.id }}">
    {% for u in plan.unassigned %}<option value="{{ u.id }}">{{ u.date|de }} · {{ u.correspondent }} · {{ u.title }}{% if u.nums %} · Nr. {{ u.nums|map(attribute='n')|join(', ') }}{% endif %}</option>{% endfor %}
  </select>
  {% endif %}
</div>
{% endmacro %}

<div class="groups">{% for g in plan.groups %}{{ group(g) }}{% endfor %}</div>
{% if plan.without %}
<details class="card"><summary><b>{{ plan.without|length }} Rechnungen ohne gefundenen Lieferschein</b> (manuell zuordenbar)</summary>
<div class="groups">{% for g in plan.without %}{{ group(g) }}{% endfor %}</div></details>
{% endif %}
{% if plan.contained %}
<details class="card"><summary><b>{{ plan.contained|length }} Lieferschein(e) stecken schon in einer ergänzten Rechnung</b></summary>
<p class="meta">Die Nummer dieser Lieferscheine steht in einer Rechnung, die bereits mit Lieferscheinen zusammengeführt wurde –
  meist doppelt eingescannte oder wiederhergestellte Lieferscheine. Sie werden nicht erneut vorgeschlagen.
  Anhaken und markieren, damit sie auch künftig nicht mehr auftauchen.</p>
<div class="ulist">{% for c in plan.contained %}<label class="urow"><input type="checkbox" name="done_ls" value="{{ c.id }}" class="donels" checked>
  <span><a href="{{ link(c.id) }}" target="_blank">{{ c.title }}</a>
  <span class="meta">{{ c.date|de }} · {{ c.correspondent }} → enthalten in
    <a href="{{ link(c.in_inv.id) }}" target="_blank">{{ c.in_inv.title }}</a> ({{ c.in_inv.date|de }})</span>
  {% for x in c.nums %}<span class="num {{ 'hit' if x.hit }}">{{ x.n }}</span>{% endfor %}</span></label>{% endfor %}</div>
<div style="margin-top:12px"><button type="submit" class="sec" formaction="{{ url_for('mark_done') }}" data-markdone>
  Ausgewählte als zugeordnet markieren (Tag „{{ done_tag }}“)</button></div>
</details>
{% endif %}
{% if plan.unassigned %}
<details class="card" {{ 'open' if open_unassigned }}><summary><b>{{ plan.unassigned|length }} nicht zugeordnete Lieferscheine</b></summary>
<p class="meta">Lieferscheine anhaken und in der ganzen Paperless-Instanz nach Rechnungen suchen lassen, die deren Nummer enthalten.</p>
<div class="ulist">{% for u in plan.unassigned %}<label class="urow"><input type="checkbox" name="find_ls" value="{{ u.id }}" class="findls">
  <span><a href="{{ link(u.id) }}" target="_blank">{{ u.title }}</a>
  <span class="meta">{{ u.date|de }} · {{ u.correspondent }}</span>
  {% for x in u.nums %}<span class="num">{{ x.n }}</span>{% else %}<span class="none">keine Nr. erkannt</span>{% endfor %}</span></label>{% endfor %}</div>
<div style="margin-top:12px;display:flex;gap:12px;flex-wrap:wrap;align-items:center">
  <button type="button" class="linkbtn" id="findall">alle auswählen</button>
  <button type="submit" class="sec" formaction="{{ url_for('search_ls') }}" data-search>🔍 Ausgewählte in Paperless suchen</button>
  <button type="submit" class="sec" formaction="{{ url_for('mark_done') }}" name="mark_src" value="find" data-search>
    Ausgewählte als erledigt markieren</button></div>
</details>
{% endif %}

<div class="card bar"><a class="btn sec" href="{{ url_for('index') }}">← Abbrechen</a>
  <button type="submit">3 · Ausgewählte zusammenführen</button></div>
</form>
<script>
document.querySelectorAll('.ls,.add').forEach(el => el.addEventListener('change', () => {
  const inv = document.querySelector('.inv[value="' + el.dataset.inv + '"]');
  if (el.checked || (el.selectedOptions && el.selectedOptions.length)) inv.checked = true;
}));
const fa = document.getElementById('findall');
if (fa) fa.addEventListener('click', () => document.querySelectorAll('.findls').forEach(c => c.checked = true));
document.getElementById('f').addEventListener('submit', e => {
  if (e.submitter && e.submitter.hasAttribute('data-markdone')) {
    if (!document.querySelectorAll('.donels:checked').length) { alert('Nichts ausgewählt.'); e.preventDefault(); }
    return;
  }
  if (e.submitter && e.submitter.hasAttribute('data-search1')) {
    document.querySelectorAll('.findls').forEach(c => c.checked = false);
    return;
  }
  if (e.submitter && e.submitter.hasAttribute('data-search')) {
    if (!document.querySelectorAll('.findls:checked').length) { alert('Bitte mindestens einen Lieferschein anhaken.'); e.preventDefault(); }
    return;
  }
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

SEARCH = """
{% if error %}<div class="card err">{{ error }}</div>{% endif %}
<form class="card" method="post" action="{{ url_for('search_ls') }}">
 <input type="hidden" name="plan" value="{{ plan_id }}"><input type="hidden" name="run" value="1">
 {% for l in lss %}<input type="hidden" name="find_ls" value="{{ l.id }}">{% endfor %}
 <h3>🔍 Rechnungen zu Lieferscheinen suchen</h3>
 <p class="meta" style="margin-top:0">Durchsucht die ganze Paperless-Instanz nach Dokumenten, deren Text die Lieferscheinnummer enthält
   (auch OCR-tolerant, z. B. LI ↔ 11).</p>
 <div class="cols">
  <section>
   <h3>Lieferscheine &amp; gesuchte Nummern</h3>
   {% for l in lss %}
   <div class="lshead" style="margin-bottom:14px">
     <a href="{{ link(l.id) }}" target="_blank"><img class="th" loading="lazy" src="{{ url_for('thumb', doc_id=l.id) }}"></a>
     <div style="flex:1;min-width:0"><b>{{ l.title }}</b><br><span class="meta">{{ l.date|de }} · {{ l.correspondent }}</span>
       <label class="f" style="margin-top:8px">Gesuchte Nummern (mehrere mit Komma trennen)</label>
       <input type="text" name="nums_{{ l.id }}" value="{{ nums[l.id]|join(', ') }}" placeholder="z. B. 4LI123456"></div>
   </div>
   {% endfor %}
  </section>
  <section>
   <h3>Vorgaben</h3>
   <label class="f">In diesen Dokumententypen suchen</label>
   <div class="chips">{% for t in types %}<label class="chip plain"><input type="checkbox" name="stype" value="{{ t.id }}"
     {{ 'checked' if t.id in sel_types }}><span>{{ t.name }}</span></label>{% endfor %}</div>
   <div class="grid2" style="margin-top:14px">
     <div><label class="f">Tage vor dem Lieferscheindatum</label><input type="number" name="before" value="{{ before }}" min="0" max="3650"></div>
     <div><label class="f">Tage nach dem Lieferscheindatum</label><input type="number" name="after" value="{{ after }}" min="0" max="3650"></div>
   </div>
   <label class="switch"><input type="checkbox" name="unlimited" value="1" {{ 'checked' if unlimited }}> ohne Zeitbegrenzung suchen</label>
   <label class="switch"><input type="checkbox" name="same_corr" value="1" {{ 'checked' if same_corr }}> nur beim gleichen Korrespondenten suchen</label>
   <p class="meta">Lieferscheine ohne Korrespondent werden immer bei allen Korrespondenten gesucht.</p>
  </section>
 </div>
 <div style="margin-top:20px;display:flex;gap:12px;flex-wrap:wrap">
   <button>Suchen</button><a class="btn sec" href="{{ url_for('review', plan_id=plan_id) }}">← Zurück zur Prüfseite</a></div>
</form>

{% if results is not none %}
<form method="post" action="{{ url_for('search_merge') }}" id="sf">
<input type="hidden" name="sid" value="{{ sid }}">
<div class="groups">
{% for l in lss %}{% set hits = results[l.id] %}
<div class="card">
  <div class="grp-head">
    <a href="{{ link(l.id) }}" target="_blank"><img class="th" loading="lazy" src="{{ url_for('thumb', doc_id=l.id) }}"></a>
    <div><span class="tag">Lieferschein</span> <b>{{ l.title }}</b><br>
      <span class="meta">{{ l.date|de }} · {{ l.correspondent }}</span>
      <div class="nums"><span class="nl">Gesucht:</span>{% for n in nums[l.id] %}<span class="num">{{ n }}</span>{% else %}<span class="none">keine Nummer</span>{% endfor %}</div></div>
  </div>
  <div class="items">
  {% for h in hits %}
  <label class="row" style="cursor:pointer">
    <input type="radio" name="target_{{ l.id }}" value="{{ h.id }}" {{ 'checked' if hits|length == 1 }}>
    <a href="{{ link(h.id) }}" target="_blank"><img class="th" loading="lazy" src="{{ url_for('thumb', doc_id=h.id) }}"></a>
    <div><span class="tag">{{ h.type }}</span> {% if h.merged %}<span class="badge mittel">bereits ergänzt</span> {% endif %}<b>{{ h.title }}</b><br>
      <span class="meta">{{ h.date|de }} · {{ h.correspondent }} · {{ h.delta }}</span>
      <div class="nums"><span class="nl">Gefunden:</span><span class="num hit">{{ h.match }}</span>
        {% if h.fuzzy %}<span class="meta">(OCR-tolerant)</span>{% endif %}</div></div>
  </label>
  {% else %}
  <div class="nomatch meta">Keine passende Rechnung gefunden. Nummer prüfen oder die Vorgaben erweitern.</div>
  {% endfor %}
  {% if hits %}<label class="row" style="cursor:pointer"><input type="radio" name="target_{{ l.id }}" value="" {{ 'checked' if hits|length != 1 }}>
    <span class="meta">nicht zuordnen</span></label>{% endif %}
  </div>
</div>
{% endfor %}
</div>
<div class="card bar"><span class="meta">Rechnung zuerst, danach die Lieferscheine – Tags wie auf der Startseite gewählt, Originale in den Papierkorb.</span>
  <button type="submit">Ausgewählte zusammenführen</button></div>
</form>
<script>
document.getElementById('sf').addEventListener('submit', e => {
  const n = [...document.querySelectorAll('#sf input[type=radio]:checked')].filter(r => r.value).length;
  if (!n) { alert('Keine Rechnung ausgewählt.'); e.preventDefault(); return; }
  if (!confirm(n + ' Lieferschein(e) jetzt mit der gewählten Rechnung zusammenführen?')) e.preventDefault();
});
</script>
{% endif %}
"""

AUDIT_FORM = """
{% if error %}<div class="card err">{{ error }}</div>{% endif %}
<form class="card" method="post" action="{{ url_for('audit_start') }}">
  <h3>🔎 Zusammengeführte Rechnungen prüfen</h3>
  <p class="meta" style="margin-top:0">Prüft alle Rechnungen, die schon mit Lieferscheinen zusammengeführt wurden
    (Tag „{{ merged_tag }}“ oder Paperless-Datei „…_merged.pdf“): Sind Seiten doppelt enthalten? Steckt derselbe
    Lieferschein in mehreren Rechnungen? Gefundene Doppelungen lassen sich anschließend gezielt entfernen.</p>
  <div class="grid2">
    <div><label class="f">Rechnungsdatum von</label><input type="date" name="start" value="{{ start }}" required></div>
    <div><label class="f">Rechnungsdatum bis</label><input type="date" name="end" value="{{ end }}" required></div>
  </div>
  <p class="meta">Es werden höchstens {{ max_docs }} Dokumente geprüft (die neuesten zuerst). Jede PDF wird dafür einmal geladen.</p>
  {% if ocr_enabled %}<label class="switch"><input type="checkbox" name="use_ocr" value="1" checked>
    📄 Seiten ohne Text mit der Texterkennung ({{ ocr_model }}) lesen</label>
  <p class="meta">Nur Dokumente mit Seiten ohne Textebene werden gesendet – höchstens so viele wie in den Einstellungen festgelegt.</p>{% endif %}
  <div style="margin-top:16px"><button>Prüfung starten</button></div>
</form>
"""

AUDIT_PROGRESS = """
<meta http-equiv="refresh" content="2">
<div class="card"><h3>{{ title or '🔎 Prüfung läuft …' }}</h3>
  <p>{% if job.phase %}{{ job.phase }} · {% endif %}{{ job.done }} von {{ job.total if job.total else '?' }} Dokumenten geprüft.</p>
  <div style="height:10px;border-radius:99px;background:var(--surface2);overflow:hidden">
    <div style="height:100%;width:{{ (100 * job.done / job.total) if job.total else 3 }}%;background:linear-gradient(90deg,var(--accent),var(--accent2))"></div></div>
  <p class="meta">Die Seite aktualisiert sich automatisch.</p></div>
"""

AUDIT_RESULT = """
{% if job.status == 'fehler' %}<div class="card err">Fehler bei der Prüfung: {{ job.error }}</div>{% endif %}
<form method="post" action="{{ url_for('audit_fix', job_id=job_id) }}" id="af">
<div class="card"><h3>🔎 Prüfergebnis</h3>
  <div class="meta">Zeitraum {{ job.start|de }} – {{ job.end|de }}</div>
  <div class="stats">
    <div class="stat"><b>{{ job.results|length }}</b><span class="meta">geprüft</span></div>
    <div class="stat"><b>{{ flagged|length }}</b><span class="meta">mit Auffälligkeiten</span></div>
    <div class="stat"><b>{{ dup_total }}</b><span class="meta">doppelte Seiten</span></div>
  </div>
  {% if job.ocr_docs %}<p class="meta">📄 {{ job.ocr_docs }} Dokument(e) mit Seiten ohne Text wurden per Texterkennung gelesen.</p>{% endif %}
  {% for e in job.ocr_errors or [] %}<p class="meta" style="color:var(--bad)">{{ e }}</p>{% endfor %}
  {% if job.truncated %}<p class="meta">Achtung: {{ job.truncated }} Dokumente gefunden, geprüft wurden die neuesten {{ job.results|length }}.
    Für den Rest den Zeitraum verkleinern.</p>{% endif %}
  <p class="meta"><span class="badge unsicher">sicher doppelt</span> Seite ist (fast) identisch mit einer früheren Seite – zum Löschen vorausgewählt ·
    <span class="badge mittel">möglich</span> sehr ähnlich, bitte selbst ansehen – nicht vorausgewählt.
    Die erste Seite (Rechnung) wird nie zum Löschen vorgeschlagen. Paperless legt beim Entfernen eine neue Dokumentversion an.</p>
  <label class="switch"><input type="checkbox" id="showall"> auch unauffällige Dokumente anzeigen</label>
</div>
{% if not flagged %}<div class="card ok">Keine Auffälligkeiten gefunden – jeder Lieferschein ist nur einmal enthalten.</div>{% endif %}
<div class="groups">
{% for e in job.results %}
<div class="card {{ '' if e.issues else 'clean' }}" {{ '' if e.issues else 'hidden' }}>
  <div class="grp-head" style="align-items:flex-start">
    <div style="flex:1;min-width:0"><span class="tag">{{ type_invoice }}</span> <b>{{ e.title }}</b>
      {% if e.id in fixed %}<span class="badge sicher">korrigiert</span>{% endif %}<br>
      <span class="meta">{{ e.date|de }} · {{ e.correspondent }} · {{ e.pages|length }} Seiten ·
        <a href="{{ link(e.id) }}" target="_blank">{{ "in Paperless öffnen" if public else "PDF ansehen" }}</a></span>
      {% if e.error %}<div class="meta" style="color:var(--bad)">Konnte nicht geprüft werden: {{ e.error }}</div>{% endif %}
      {% for c in e.cross %}<div class="nums"><span class="badge mittel">auch woanders</span>
        LS-Nr. <span class="num">{{ c.num }}</span> (Seite {{ c.pages|join(', ') }}) steckt auch in
        {% for o in c.others %}<a href="{{ link(o[0]) }}" target="_blank">{{ titles.get(o[0], o[0]) }}</a> (S. {{ o[1]|join(', ') }}){{ ', ' if not loop.last }}{% endfor %}</div>{% endfor %}
      {% for r in e.repeat %}<div class="nums"><span class="badge mittel">mehrfach</span>
        LS-Nr. <span class="num">{{ r.num }}</span> auf den Seiten {{ r.pages|join(', ') }}</div>{% endfor %}
    </div>
  </div>
  {% if e.pages %}
  <div class="pagegrid">
  {% for p in e.pages %}{% set d = e.dups.get(p.n) %}
    <label class="pg {{ 'dup' if d and d[1] == 'sicher' else ('maybe' if d else '') }}">
      <img loading="lazy" src="{{ url_for('audit_page', job_id=job_id, doc_id=e.id, n=p.n) }}">
      <div class="pgl"><b>S. {{ p.n }}</b>{% if p.nums %} · {{ p.nums|join(', ') }}{% endif %}{% if p.ocr %}<span class="ai">OCR</span>{% endif %}</div>
      {% if d %}<div class="pgd">{{ d[2] }}</div>{% endif %}
      {% if p.n > 1 %}<span class="pgc"><input type="checkbox" name="del_{{ e.id }}" value="{{ p.n }}" class="delp"
        {{ 'checked' if d and d[1] == 'sicher' and e.id not in fixed }}> löschen</span>{% endif %}
    </label>
  {% endfor %}
  </div>
  {% endif %}
</div>
{% endfor %}
</div>
<div class="card bar"><a class="btn sec" href="{{ url_for('audit') }}">← Neue Prüfung</a>
  <button type="submit">Markierte Seiten entfernen</button></div>
</form>
<script>
document.getElementById('showall').addEventListener('change', e =>
  document.querySelectorAll('.card.clean').forEach(c => c.hidden = !e.target.checked));
document.getElementById('af').addEventListener('submit', e => {
  const n = document.querySelectorAll('.delp:checked').length;
  if (!n) { alert('Keine Seite markiert.'); e.preventDefault(); return; }
  if (!confirm(n + ' Seite(n) aus den Dokumenten entfernen? Paperless legt dafür neue Versionen an.')) e.preventDefault();
});
</script>
"""

SETTINGS = """
{% for m in msgs %}<div class="card {{ m[0] }}">{{ m[1] }}</div>{% endfor %}
<form class="card" method="post" action="{{ url_for('settings') }}" autocomplete="off">
 <h3>⚙ Einstellungen – KI-Zugänge</h3>
 <p class="meta" style="margin-top:0">Texterkennung und Auswertung lassen sich getrennt einrichten – z. B. Mistral für die
   Texterkennung und OpenAI für die Auswertung. Beide sind optional. Die Zugänge werden auf dem Server in
   <code>{{ settings_file }}</code> gespeichert{% if not writable %} – <b style="color:var(--bad)">das Verzeichnis ist nicht beschreibbar,
   bitte in der docker-compose.yml das Volume <code>./data:/data</code> einbinden</b>{% endif %}.
   {% if not protected %}<br><b>Achtung:</b> Die Anmeldung ist abgeschaltet (<code>LOGIN_REQUIRED=false</code>) – der Zugang muss anderweitig geschützt sein.{% endif %}</p>
 <div class="cols">
  <section>
   <h3>📄 Texterkennung (OCR)</h3>
   <p class="meta" style="margin-top:0">Liest Dokumente neu, bei denen die Texterkennung von Paperless keine Lieferscheinnummer liefert,
     und Seiten ohne Text bei der Prüfung. Das PDF des betroffenen Dokuments wird dafür an den Anbieter gesendet.</p>
   <label class="f">Anbieter</label>
   <select name="ocr_provider">
     <option value="off" {{ 'selected' if st.ocr.provider == 'off' }}>aus</option>
     <option value="mistral" {{ 'selected' if st.ocr.provider == 'mistral' }}>Mistral OCR</option></select>
   <label class="f" style="margin-top:12px">API-Schlüssel</label>
   <input type="password" name="ocr_api_key" value="" placeholder="{{ ('gespeichert ' ~ mask(st.ocr.api_key)) if st.ocr.api_key else 'Schlüssel eingeben' }}">
   {% if st.ocr.api_key %}<label class="switch"><input type="checkbox" name="ocr_clear" value="1"> gespeicherten Schlüssel löschen</label>{% endif %}
   <p class="meta">Leer lassen = Schlüssel bleibt unverändert. Ist für die Auswertung ebenfalls Mistral gewählt, wird dieser Schlüssel dort mitgenutzt.</p>
   <div class="grid2">
     <div><label class="f">Modell</label><input type="text" name="ocr_model" value="{{ st.ocr.model }}" placeholder="{{ ocr_default }}"></div>
     <div><label class="f">Höchstzahl Dokumente je Durchlauf</label><input type="number" name="ocr_max_calls" value="{{ st.ocr.max_calls }}" min="1" max="500"></div>
   </div>
   <p class="meta">Status: {% if ocr.enabled %}<span class="badge sicher">aktiv</span> {{ ocr.model }}{% else %}<span class="badge unsicher">aus</span>{% endif %}</p>
   <button type="submit" class="sec" name="action" value="test_ocr">Speichern &amp; Texterkennung testen</button>
  </section>
  <section>
   <h3>🧠 Auswertung (KI)</h3>
   <p class="meta" style="margin-top:0">Ergänzt fehlende Lieferscheinnummern und prüft unsichere Vorschläge anhand des Textes
     (Nummern, Positionen, Mengen). Es wird nur Text gesendet, keine PDFs.</p>
   <label class="f">Anbieter</label>
   <select name="ai_provider" id="aiprov">
     <option value="off" {{ 'selected' if st.ai.provider == 'off' }}>aus</option>
     <option value="openai" {{ 'selected' if st.ai.provider == 'openai' }}>OpenAI</option>
     <option value="mistral" {{ 'selected' if st.ai.provider == 'mistral' }}>Mistral</option>
     <option value="custom" {{ 'selected' if st.ai.provider == 'custom' }}>eigene Adresse (OpenAI-kompatibel, z. B. Ollama)</option></select>
   <div id="aibase" {{ '' if st.ai.provider == 'custom' else 'hidden' }}>
     <label class="f" style="margin-top:12px">Adresse der API</label>
     <input type="text" name="ai_base_url" value="{{ st.ai.base_url }}" placeholder="http://192.168.1.60:11434/v1"></div>
   <label class="f" style="margin-top:12px">API-Schlüssel</label>
   <input type="password" name="ai_api_key" value="" placeholder="{{ ('gespeichert ' ~ mask(st.ai.api_key)) if st.ai.api_key else 'Schlüssel eingeben' }}">
   {% if st.ai.api_key %}<label class="switch"><input type="checkbox" name="ai_clear" value="1"> gespeicherten Schlüssel löschen</label>{% endif %}
   <p class="meta">Leer lassen = Schlüssel bleibt unverändert.</p>
   <div class="grid2">
     <div><label class="f">Modell</label><input type="text" name="ai_model" value="{{ st.ai.model }}" placeholder="{{ ai_defaults.get(st.ai.provider, '') }}" id="aimodel"></div>
     <div><label class="f">Höchstzahl Anfragen je Durchlauf</label><input type="number" name="ai_max_calls" value="{{ st.ai.max_calls }}" min="1" max="1000"></div>
   </div>
   <p class="meta">Status: {% if ai.enabled %}<span class="badge sicher">aktiv</span> {{ ai.model }} · {{ ai.base_url }}{% else %}<span class="badge unsicher">aus</span>{% endif %}</p>
   <button type="submit" class="sec" name="action" value="test_ai">Speichern &amp; Auswertung testen</button>
  </section>
 </div>
 <div style="margin-top:22px"><button type="submit" name="action" value="save">Speichern</button></div>
</form>
{% if login_required %}
<form class="card" method="post" action="{{ url_for('change_password') }}" autocomplete="off">
  <h3>🔐 Anmeldung</h3>
  {% if account_env %}<p class="meta">Der Zugang ist über <code>APP_USER</code> / <code>APP_PASSWORD</code> in der docker-compose.yml
    festgelegt und kann hier nicht geändert werden.</p>
  {% else %}
  <p class="meta" style="margin-top:0">Angemeldet als <b>{{ current_user }}</b>. Nach einer Änderung muss man sich überall neu anmelden.</p>
  <div class="grid2">
    <div><label class="f">Neuer Benutzername (optional)</label><input type="text" name="username" value="" placeholder="{{ current_user }}" autocomplete="username"></div>
    <div><label class="f">Aktuelles Passwort</label><input type="password" name="current" required autocomplete="current-password"></div>
    <div><label class="f">Neues Passwort</label><input type="password" name="password" required minlength="8" autocomplete="new-password"></div>
    <div><label class="f">Neues Passwort wiederholen</label><input type="password" name="password2" required minlength="8" autocomplete="new-password"></div>
  </div>
  <div style="margin-top:14px"><button class="sec">Zugang ändern</button></div>
  {% endif %}
</form>
{% endif %}
<script>
const defaults = {{ ai_defaults|tojson }};
document.getElementById('aiprov').addEventListener('change', e => {
  document.getElementById('aibase').hidden = e.target.value !== 'custom';
  document.getElementById('aimodel').placeholder = defaults[e.target.value] || '';
});
</script>
"""

SETUP = """
<form class="card authbox" method="post" action="{{ url_for('setup') }}" autocomplete="off">
  <h3>👋 Willkommen – Zugang anlegen</h3>
  <p class="meta" style="margin-top:0">Beim ersten Start wird ein Benutzer für diese Oberfläche angelegt.
    Danach ist eine Anmeldung nötig. Das Passwort wird nur verschlüsselt (als Hash) gespeichert.</p>
  {% if error %}<div class="card err" style="margin:0 0 12px">{{ error }}</div>{% endif %}
  {% if not writable %}<div class="card err" style="margin:0 0 12px">Das Datenverzeichnis <code>{{ data_dir }}</code> ist nicht beschreibbar.
    Bitte in der docker-compose.yml das Volume <code>./data:/data</code> einbinden und den Container neu starten.</div>{% endif %}
  <label class="f">Benutzername</label>
  <input type="text" name="username" value="{{ username }}" required autofocus minlength="3" maxlength="40" autocomplete="username">
  <label class="f">Passwort (mindestens 8 Zeichen)</label>
  <input type="password" name="password" required minlength="8" autocomplete="new-password">
  <label class="f">Passwort wiederholen</label>
  <input type="password" name="password2" required minlength="8" autocomplete="new-password">
  <button {{ 'disabled' if not writable }}>Zugang anlegen</button>
</form>
"""

LOGIN = """
<form class="card authbox" method="post" action="{{ url_for('login') }}">
  <h3>🔐 Anmelden</h3>
  {% if info %}<div class="card ok" style="margin:0 0 12px">{{ info }}</div>{% endif %}
  {% if error %}<div class="card err" style="margin:0 0 12px">{{ error }}</div>{% endif %}
  <input type="hidden" name="next" value="{{ next or '' }}">
  <label class="f">Benutzername</label>
  <input type="text" name="username" value="{{ username }}" required autofocus autocomplete="username">
  <label class="f">Passwort</label>
  <input type="password" name="password" required autocomplete="current-password">
  <label class="switch" style="margin:0 0 16px"><input type="checkbox" name="remember" value="1" checked> angemeldet bleiben (30 Tage)</label>
  <button>Anmelden</button>
  <p class="meta">Passwort vergessen? Die Datei <code>benutzer.json</code> im Datenverzeichnis löschen –
    beim nächsten Aufruf kann ein neuer Zugang angelegt werden.</p>
</form>
"""

DUPS_FORM = """
{% if error %}<div class="card err">{{ error }}</div>{% endif %}
<form class="card" method="post" action="{{ url_for('dups_start') }}">
  <h3>🧹 Duplikate prüfen</h3>
  <p class="meta" style="margin-top:0">Sucht Dokumente, die doppelt in Paperless liegen – z. B. Original-Rechnungen und
    -Lieferscheine, die schon in einer zusammengeführten Rechnung stecken, doppelt zusammengeführte Rechnungen oder
    doppelt eingescannte Dokumente. Verglichen wird der Text der Dokumente. Es wird noch nichts gelöscht.</p>
  <div class="grid2">
    <div><label class="f">Dokumentdatum von</label><input type="date" name="start" value="{{ start }}" required></div>
    <div><label class="f">Dokumentdatum bis</label><input type="date" name="end" value="{{ end }}" required></div>
  </div>
  <label class="f" style="margin-top:14px">Dokumententypen</label>
  <div class="chips">{% for t in types %}<label class="chip plain"><input type="checkbox" name="type" value="{{ t.id }}"
    {{ 'checked' if t.id in sel }}><span>{{ t.name }}</span></label>{% endfor %}</div>
  <div style="margin-top:18px"><button>Duplikate suchen</button></div>
</form>
"""

DUPS_RESULT = """
{% if job.status == 'fehler' %}<div class="card err">Fehler: {{ job.error }}</div>{% endif %}
{% if info %}<div class="card ok">{{ info }}</div>{% endif %}
<form method="post" action="{{ url_for('dups_delete', job_id=job_id) }}" id="df">
<div class="card"><h3>🧹 Duplikate</h3>
  <div class="meta">Zeitraum {{ job.start|de }} – {{ job.end|de }}</div>
  <div class="stats">
    <div class="stat"><b>{{ job.checked }}</b><span class="meta">Dokumente verglichen</span></div>
    <div class="stat"><b>{{ job.findings|length }}</b><span class="meta">Fälle</span></div>
    <div class="stat"><b>{{ total_drop }}</b><span class="meta">mögliche Duplikate</span></div>
  </div>
  {% if job.truncated %}<p class="meta">Achtung: {{ job.truncated }} Dokumente gefunden, verglichen wurden die neuesten {{ job.checked }}.</p>{% endif %}
  <p class="meta">Links steht jeweils das Dokument, das <b>bleibt</b>, rechts die Duplikate. <span class="badge unsicher">sicher</span>
    = zum Löschen vorausgewählt, <span class="badge mittel">prüfen</span> = bitte selbst ansehen. Gelöschte Dokumente landen
    im Paperless-Papierkorb und lassen sich dort wiederherstellen.</p>
</div>
{% if not job.findings %}<div class="card ok">Keine Duplikate gefunden.</div>{% endif %}
{% for f in job.findings %}
<div class="card dupcase">
  <div class="dupcol keep">
    <div class="meta" style="font-weight:700;color:var(--ok)">✓ bleibt</div>
    {{ doccard(f.keep_doc) }}
  </div>
  <div class="dupcol">
    {% for x in f.drop %}
    <label class="duprow {{ 'gone' if x.id in deleted }}">
      {% if x.id in deleted %}<span class="badge sicher">gelöscht</span>{% else %}
      <input type="checkbox" name="del" value="{{ x.id }}" class="deld" {{ 'checked' if x.sure }}>{% endif %}
      <div style="flex:1;min-width:0">
        <span class="badge {{ 'unsicher' if x.sure else 'mittel' }}">{{ 'sicher' if x.sure else 'prüfen' }}</span>
        <b>{{ x.kind }}</b> <span class="meta">– {{ x.reason }}</span>
        {{ doccard(x.doc) }}
      </div>
    </label>
    {% endfor %}
  </div>
</div>
{% endfor %}
<div class="card bar"><a class="btn sec" href="{{ url_for('dups') }}">← Neue Suche</a>
  <button type="submit">Markierte Duplikate löschen</button></div>
</form>
<script>
document.getElementById('df').addEventListener('submit', e => {
  const n = document.querySelectorAll('.deld:checked').length;
  if (!n) { alert('Nichts markiert.'); e.preventDefault(); return; }
  if (!confirm(n + ' Dokument(e) in den Paperless-Papierkorb verschieben?')) e.preventDefault();
});
</script>
"""

DOCCARD = """{% macro doccard(d) %}<div class="dc">
  <a href="{{ link(d.id) }}" target="_blank"><img class="th" loading="lazy" src="{{ url_for('thumb', doc_id=d.id) }}"></a>
  <div style="min-width:0"><span class="tag">{{ d.type }}</span>{% if d.merged %} <span class="ai">zusammengeführt</span>{% endif %}
    <b>{{ d.title }}</b><br>
    <span class="meta">{{ d.date|de }} · {{ d.correspondent }}{% if d.pages %} · {{ d.pages }} S.{% endif %}{% if d.asn %} · ASN {{ d.asn }}{% endif %}
      · ID {{ d.id }} · <a href="{{ link(d.id) }}" target="_blank">{{ "in Paperless öffnen" if public else "PDF ansehen" }}</a></span></div>
</div>{% endmacro %}"""

CLEAN_HOME = """
<div class="card"><h3>🧹 Aufräumen</h3>
  <p class="meta" style="margin-top:0">Werkzeuge, um den Bestand in Paperless sauber zu halten – unabhängig vom Zusammenführen.
    Vor jeder Änderung wird alles zur Kontrolle angezeigt.</p></div>
<div class="groups">
  <div class="card"><h3>👥 Korrespondenten</h3>
    <p class="meta">Findet doppelte und ähnliche Korrespondenten (Schreibweisen, Rechtsformen, Tippfehler), führt ausgewählte
      zusammen – alle Dokumente gehen an einen Korrespondenten, die leeren werden gelöscht – und entfernt unbenutzte.</p>
    <a class="btn" href="{{ url_for('corr_check') }}">Korrespondenten prüfen →</a></div>
  <div class="card"><h3>📄 Duplikate</h3>
    <p class="meta">Findet doppelte Dokumente – z. B. Originale neben zusammengeführten Rechnungen oder doppelte Scans –
      zeigt sie nebeneinander an und verschiebt sie auf Wunsch in den Papierkorb.</p>
    <a class="btn" href="{{ url_for('dups') }}">Duplikate prüfen →</a></div>
</div>
"""

CORR_CHECK = """
{% if error %}<div class="card err">{{ error }}</div>{% endif %}
<form method="post" action="{{ url_for('corr_apply') }}" id="cf">
<input type="hidden" name="plan" value="{{ plan_id }}">
<div class="card"><h3>👥 Korrespondenten prüfen</h3>
  <div class="stats">
    <div class="stat"><b>{{ total }}</b><span class="meta">Korrespondenten</span></div>
    <div class="stat"><b>{{ groups|length }}</b><span class="meta">Gruppen doppelt/ähnlich</span></div>
    <div class="stat"><b>{{ unused|length }}</b><span class="meta">ohne Dokumente</span></div>
  </div>
  <p class="meta">Je Gruppe den <b>Ziel-Korrespondenten</b> wählen (◉) und die Korrespondenten anhaken, die darin aufgehen sollen.
    Deren Dokumente werden dem Ziel zugewiesen, danach werden sie gelöscht. Der Name des Ziels lässt sich dabei anpassen.
    <span class="badge unsicher">gleich</span> = vorausgewählt, <span class="badge mittel">ähnlich</span> = bitte selbst entscheiden.</p>
</div>
{% if not groups %}<div class="card ok">Keine doppelten oder ähnlichen Korrespondenten gefunden.</div>{% endif %}
<div class="groups">
{% for g in groups %}{% set gi = loop.index0 %}
<div class="card">
  <input type="hidden" name="groups" value="{{ gi }}">
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <span class="badge {{ 'unsicher' if g.sure else 'mittel' }}">{{ 'gleich' if g.sure else 'ähnlich' }}</span>
    <span class="meta">{{ g.reasons|join(' · ') }}</span></div>
  <table class="ctab">
    <tr><th>Ziel</th><th>zusammen&shy;führen</th><th>Korrespondent</th><th>Dok.</th><th></th></tr>
    {% for m in g.rows %}
    <tr>
      <td><input type="radio" name="t_{{ gi }}" value="{{ m.id }}" {{ 'checked' if m.id == g.target }} class="tsel" data-g="{{ gi }}"></td>
      <td><input type="checkbox" name="m_{{ gi }}" value="{{ m.id }}" class="msel" data-g="{{ gi }}" {{ 'checked' if m.pre }}></td>
      <td><b>{{ m.name }}</b>{% if m.match %}<br><span class="meta">Zuordnung: {{ m.match }}</span>{% endif %}</td>
      <td>{{ m.document_count }}</td>
      <td class="meta">{{ m.why }}</td>
    </tr>
    {% endfor %}
  </table>
  <label class="f" style="margin-top:10px">Name des Ziel-Korrespondenten</label>
  <input type="text" name="n_{{ gi }}" value="{{ g.target_name }}" maxlength="128">
</div>
{% endfor %}
</div>

<div class="card"><h3>✋ Manuell zusammenführen</h3>
  <p class="meta" style="margin-top:0">Für Fälle, die nicht automatisch erkannt wurden: Korrespondenten auswählen (Strg/Cmd für Mehrfachauswahl)
    und ein Ziel festlegen.</p>
  <div class="grid2">
    <div><label class="f">Diese Korrespondenten …</label>
      <select name="manual_src" multiple size="8">{% for c in all %}<option value="{{ c.id }}">{{ c.name }} ({{ c.document_count }})</option>{% endfor %}</select></div>
    <div><label class="f">… zusammenführen in</label>
      <select name="manual_target"><option value="">– Ziel wählen –</option>{% for c in all %}<option value="{{ c.id }}">{{ c.name }} ({{ c.document_count }})</option>{% endfor %}</select></div>
  </div>
</div>

{% if unused %}
<details class="card"><summary><b>{{ unused|length }} Korrespondenten ohne Dokumente</b> – löschen?</summary>
  <p class="meta">Achtung: Werden sie in Mail-Regeln oder Workflows von Paperless verwendet, gehen diese Einstellungen verloren.
    Deshalb nicht vorausgewählt.</p>
  <button type="button" class="linkbtn" id="allunused">alle auswählen</button>
  <div class="ulist">{% for c in unused %}<label class="urow"><input type="checkbox" name="unused" value="{{ c.id }}" class="usel">
    <span><b>{{ c.name }}</b>{% if c.match %} <span class="meta">· Zuordnung: {{ c.match }}</span>{% endif %}</span></label>{% endfor %}</div>
</details>
{% endif %}

<div class="card"><label class="switch" style="margin:0"><input type="checkbox" name="keep_match" value="1" checked>
  Suchbegriffe der gelöschten Korrespondenten in das Ziel übernehmen</label>
  <p class="meta">Gilt, wenn Ziel und Quelle die Zuordnung „irgendein Wort“ oder „alle Wörter“ verwenden – sonst gehen die
    Zuordnungsregeln der gelöschten Korrespondenten verloren.</p></div>
<div class="card bar"><span class="meta" id="csum">Nichts ausgewählt.</span>
  <button type="submit">Ausgewählte zusammenführen &amp; leere löschen</button></div>
</form>
<script>
const NAMES = {{ names|tojson }};
function summary() {
  let moves = 0, dels = 0, parts = [];
  document.querySelectorAll('input[name=groups]').forEach(h => {
    const g = h.value, t = document.querySelector('.tsel[data-g="' + g + '"]:checked');
    const src = [...document.querySelectorAll('.msel[data-g="' + g + '"]:checked')].filter(c => !t || c.value !== t.value);
    if (t && src.length) { dels += src.length; parts.push(src.map(c => NAMES[c.value]).join(', ') + ' → ' + NAMES[t.value]); }
  });
  const ms = [...document.querySelector('select[name=manual_src]').selectedOptions].map(o => o.value);
  const mt = document.querySelector('select[name=manual_target]').value;
  const msrc = ms.filter(v => v !== mt);
  if (mt && msrc.length) { dels += msrc.length; parts.push(msrc.map(v => NAMES[v]).join(', ') + ' → ' + NAMES[mt]); }
  const un = document.querySelectorAll('.usel:checked').length;
  document.getElementById('csum').textContent = (parts.length || un) ?
    (dels + ' Korrespondent(en) zusammenführen' + (un ? ', ' + un + ' unbenutzte löschen' : '')) : 'Nichts ausgewählt.';
  return {parts, dels, un};
}
document.getElementById('cf').addEventListener('change', summary);
const au = document.getElementById('allunused');
if (au) au.addEventListener('click', () => { document.querySelectorAll('.usel').forEach(c => c.checked = true); summary(); });
document.querySelectorAll('.tsel').forEach(r => r.addEventListener('change', () => {
  const m = document.querySelector('.msel[data-g="' + r.dataset.g + '"][value="' + r.value + '"]'); if (m) m.checked = true;
  const n = document.querySelector('input[name="n_' + r.dataset.g + '"]'); if (n) n.value = NAMES[r.value];
}));
summary();
document.getElementById('cf').addEventListener('submit', e => {
  const s = summary();
  if (!s.parts.length && !s.un) { alert('Nichts ausgewählt.'); e.preventDefault(); return; }
  if (!confirm('Folgendes ausführen?\\n\\n' + s.parts.join('\\n') + (s.un ? '\\n\\n' + s.un + ' unbenutzte Korrespondenten löschen' : '')
      + '\\n\\nDokumente werden umgehängt, die zusammengeführten Korrespondenten danach gelöscht.')) e.preventDefault();
});
</script>
"""

CORR_RESULT = """
<div class="card {{ 'err' if errors else 'ok' }}"><h3>✓ Korrespondenten aufgeräumt</h3>
  {{ moved }} Dokument(e) umgehängt · {{ deleted }} Korrespondent(en) gelöscht{% if renamed %} · {{ renamed }} umbenannt{% endif %}
  {% if errors %} · {{ errors|length }} Fehler{% endif %}</div>
{% for l in lines %}<div class="card">✔ {{ l }}</div>{% endfor %}
{% for e in errors %}<div class="card err">✘ {{ e }}</div>{% endfor %}
<div class="card"><a class="btn" href="{{ url_for('corr_check') }}">Erneut prüfen</a></div>
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
  <p style="margin-top:14px;display:flex;gap:12px;flex-wrap:wrap">
    {% if back %}<a class="btn sec" href="{{ url_for('review', plan_id=back) }}">← Zurück zur Prüfseite</a>{% endif %}
    <a class="btn" href="{{ url_for('index') }}">Neuer Durchlauf</a></p></div>
"""


AUDIT_FIXED = """
<div class="card {{ 'err' if errors else 'ok' }}"><h3>✓ Korrektur</h3>
  {{ done|length }} Dokument(e) an Paperless übergeben{% if errors %}, {{ errors|length }} Fehler{% endif %}.
  <p class="meta">Paperless erzeugt die bereinigten Dokumente im Hintergrund als neue Version – das dauert einen Moment.</p></div>
{% for d in done %}<div class="card">✔ {{ d.title }}: Seite(n) {{ d.pages|join(', ') }} entfernt</div>{% endfor %}
{% for e in errors %}<div class="card err">✘ {{ e }}</div>{% endfor %}
<div class="card" style="display:flex;gap:12px;flex-wrap:wrap">
  <a class="btn sec" href="{{ url_for('audit_job', job_id=job_id) }}">← Zurück zum Prüfergebnis</a>
  <a class="btn" href="{{ url_for('audit') }}">Neue Prüfung</a></div>
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
            if t["name"] in (DONE_TAG, MERGED_TAG):
                continue
            t["preselect"] = t["name"].lower() in DEFAULT_TAGS
            tags.append(t)
    except Exception as e:  # noqa: BLE001
        error = error or f"Tags/Korrespondenten konnten nicht geladen werden: {e}"
    return page(INDEX, error=error, start=first_prev.isoformat(), end=today.isoformat(),
                lookback=DEFAULT_LOOKBACK, done_tag=DONE_TAG, merged_tag=MERGED_TAG, tags=tags, corrs=corrs,
                ai=ai_cfg(), ocr=ocr_cfg())


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
    delete_originals = True  # immer: nach dem Zusammenführen darf nur das neue Dokument übrig bleiben
    corr_ids = {int(x) for x in request.form.getlist("corr") if x.isdigit()}
    corr_none = request.form.get("corr_none") == "1"
    hide_merged = request.form.get("hide_merged") == "1"
    use_ai = (ai_cfg()["enabled"] or ocr_cfg()["enabled"]) and request.form.get("use_ai") == "1"

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
        merged_tag = pl.id_by_name("/api/tags/", MERGED_TAG)
        for d in invoices:
            d["_merged"] = is_merged_doc(d, merged_tag)
        orig_dups, ls_dups = mark_already_merged(invoices, deliveries)
        wanted = {int(x) for x in request.form.getlist("tags") if x.isdigit()}
        extra_tags = [t for t in pl.tags() if t["id"] in wanted] if wanted else []
    except Exception as e:  # noqa: BLE001
        return index(error=f"Fehler bei der Abfrage von Paperless: {e}")

    deliveries = [d for d in deliveries if d["id"] not in ls_dups]
    plan = build_plan(invoices, deliveries, corr, start, hide_merged=hide_merged)
    inv_by_id = {i["id"]: i for i in invoices}
    for lid, (ls_doc, inv_id) in ls_dups.items():
        plan["contained"].append({**summarize(ls_doc, corr), "in_inv": summarize(inv_by_id[inv_id], corr), "match": None,
                                  "nums": [{"n": x, "hit": False, "ai": ""} for x in sorted(delivery_numbers(ls_doc))]})
    plan["orig_dups"] = orig_dups
    ai_stats = None
    if use_ai:
        plan, ai_stats = ai_enhance(invoices, deliveries, corr, start, hide_merged, plan)
    plan["groups"] = [g for g in plan["groups"] if g["inv"]["date"] <= end.isoformat()]
    plan["without"] = [g for g in plan["without"] if g["inv"]["date"] <= end.isoformat()]

    plan_id = uuid.uuid4().hex
    stored = {"ts": time.time(), "plan": plan, "delete_originals": delete_originals,
              "extra_tags": [t["id"] for t in extra_tags],
              "view": {"start": start, "end": end, "extra_tags": extra_tags, "ai_stats": ai_stats,
                       "sel_corrs": [corr.get(c, f"#{c}") for c in
                                     sorted(corr_ids, key=lambda c: corr.get(c, "").lower())]}}
    with PLANS_LOCK:
        PLANS[plan_id] = stored
    return render_review(plan_id, stored)


def render_review(plan_id, stored, open_unassigned=False, info=None):
    v = stored["view"]
    return page(PREVIEW, plan=stored["plan"], plan_id=plan_id, start=v["start"], end=v["end"],
                delete_originals=stored["delete_originals"], public=PAPERLESS_PUBLIC_URL, link=doc_link,
                type_invoice=TYPE_INVOICE, extra_tags=v["extra_tags"], sel_corrs=v["sel_corrs"],
                open_unassigned=open_unassigned, done_tag=DONE_TAG, info=info, ai_stats=v.get("ai_stats"))


def _get_plan(plan_id):
    _cleanup_plans()
    with PLANS_LOCK:
        stored = PLANS.get(plan_id or "")
        if stored:
            stored["ts"] = time.time()  # Prüfseite bleibt gültig, solange daran gearbeitet wird
        return stored


@app.route("/review/<plan_id>")
@require_auth
def review(plan_id):
    stored = _get_plan(plan_id)
    if not stored or "plan" not in stored:
        return index(error="Der Vorschlag ist abgelaufen oder wurde schon ausgeführt. Bitte neu suchen.")
    return render_review(plan_id, stored, open_unassigned=True)


@app.route("/mark-done", methods=["POST"])
@require_auth
def mark_done():
    plan_id = request.form.get("plan", "")
    stored = _get_plan(plan_id)
    if not stored or "plan" not in stored:
        return index(error="Der Vorschlag ist abgelaufen oder wurde schon ausgeführt. Bitte neu suchen.")
    p = stored["plan"]
    allowed = {c["id"] for c in p.get("contained", [])} | {u["id"] for u in p["unassigned"]}
    field = "find_ls" if request.form.get("mark_src") == "find" else "done_ls"
    ids = [i for i in dict.fromkeys(int(x) for x in request.form.getlist(field) if x.isdigit()) if i in allowed]
    if not ids:
        return render_review(plan_id, stored)
    try:
        pl = Paperless()
        pl.modify_tags(ids, add=[pl.ensure_tag(DONE_TAG)])
    except Exception as e:  # noqa: BLE001
        return render_review(plan_id, stored, info=f"Markieren fehlgeschlagen: {e}")
    with PLANS_LOCK:
        p["contained"] = [c for c in p.get("contained", []) if c["id"] not in ids]
        p["unassigned"] = [u for u in p["unassigned"] if u["id"] not in ids]
    return render_review(plan_id, stored, info=f"{len(ids)} Lieferschein(e) mit „{DONE_TAG}“ markiert.")


@app.route("/search-ls", methods=["POST"])
@require_auth
def search_ls():
    plan_id = request.form.get("plan", "")
    stored = _get_plan(plan_id)
    if not stored or "plan" not in stored:
        return index(error="Der Vorschlag ist abgelaufen oder wurde schon ausgeführt. Bitte neu suchen.")
    p = stored["plan"]
    pool = {i["id"]: i for g in p["groups"] + p["without"] for i in g["items"]}
    pool.update({u["id"]: u for u in p["unassigned"]})
    lss = [pool[i] for i in dict.fromkeys(int(x) for x in request.form.getlist("find_ls") if x.isdigit()) if i in pool]
    if not lss:
        return render_review(plan_id, stored, open_unassigned=True)
    run = request.form.get("run") == "1"

    try:
        pl = Paperless()
        types = pl.doc_types()
    except Exception as e:  # noqa: BLE001
        return index(error=f"Fehler bei der Abfrage von Paperless: {e}")
    type_names = {t["id"]: t["name"] for t in types}

    if run:
        sel_types = {int(x) for x in request.form.getlist("stype") if x.isdigit()}
        before = max(0, int(request.form.get("before") or 0))
        after = max(0, int(request.form.get("after") or 0))
        unlimited = request.form.get("unlimited") == "1"
        same_corr = request.form.get("same_corr") == "1"
        nums = {}
        for l in lss:
            raw = request.form.get(f"nums_{l['id']}", "")
            nums[l["id"]] = list(dict.fromkeys(x.strip().upper() for x in re.split(r"[,;\s]+", raw) if x.strip()))
    else:
        wanted = {n.lower() for n in SEARCH_DOCTYPES}
        sel_types = {t["id"] for t in types if t["name"].lower() in wanted}
        before, after = SEARCH_DAYS_BEFORE, SEARCH_DAYS_AFTER
        unlimited, same_corr = SEARCH_UNLIMITED, SEARCH_SAME_CORR
        nums = {l["id"]: [x["n"] for x in l.get("nums", [])] for l in lss}

    ctx = dict(plan_id=plan_id, lss=lss, nums=nums, types=types, sel_types=sel_types, before=before,
               after=after, unlimited=unlimited, same_corr=same_corr, link=doc_link, results=None, sid=None,
               error=None)
    if not run:
        return page(SEARCH, **ctx)
    if not sel_types:
        ctx["error"] = "Bitte mindestens einen Dokumententyp auswählen."
        return page(SEARCH, **ctx)

    try:
        corr = pl.correspondents()
        merged_tag = pl.id_by_name("/api/tags/", MERGED_TAG)
        results, docs = {}, {}
        for l in lss:
            hits = []
            if nums[l["id"]]:
                for d, n, fuzzy in search_invoices_for(pl, l, nums[l["id"]], sel_types, before, after,
                                                       unlimited, same_corr and bool(l.get("corr_id"))):
                    d["_merged"] = is_merged_doc(d, merged_tag)
                    summ = summarize(d, corr)
                    docs[d["id"]] = summ
                    delta = (_date(d) - dt.date.fromisoformat(l["date"])).days
                    hits.append({**summ, "type": type_names.get(d.get("document_type"), "–"), "match": n,
                                 "fuzzy": fuzzy,
                                 "delta": "gleicher Tag" if delta == 0 else
                                 f"{abs(delta)} Tag(e) {'nach' if delta > 0 else 'vor'} dem Lieferschein"})
            results[l["id"]] = hits
    except Exception as e:  # noqa: BLE001
        ctx["error"] = f"Fehler bei der Suche in Paperless: {e}"
        return page(SEARCH, **ctx)

    sid = uuid.uuid4().hex
    with PLANS_LOCK:
        PLANS[sid] = {"ts": time.time(), "search": True, "plan_id": plan_id, "docs": docs,
                      "ls": {l["id"]: l for l in lss},
                      "delete_originals": stored["delete_originals"], "extra_tags": stored["extra_tags"]}
    ctx.update(results=results, sid=sid)
    return page(SEARCH, **ctx)


@app.route("/search-ls/merge", methods=["POST"])
@require_auth
def search_merge():
    with PLANS_LOCK:
        st = PLANS.pop(request.form.get("sid", ""), None)
    if not st or not st.get("search"):
        return index(error="Das Suchergebnis ist abgelaufen oder wurde schon ausgeführt. Bitte neu suchen.")
    targets = {}
    for lid in st["ls"]:
        v = request.form.get(f"target_{lid}", "")
        if v.isdigit() and int(v) in st["docs"]:
            targets.setdefault(int(v), []).append(lid)
    jobs = [(st["docs"][doc_id], sorted(lids, key=lambda x: (st["ls"][x]["date"], x)))
            for doc_id, lids in targets.items()]
    if not jobs:
        return index(error="Es wurde keine Rechnung ausgewählt.")
    done, errors = perform_merges(jobs, st["ls"], st["delete_originals"], st["extra_tags"])

    # verwendete Lieferscheine aus der Prüfseite entfernen; betroffene Rechnungen dort sperren
    merged_ls = {lid for _, lids in jobs for lid in lids}
    used_docs = set(targets)
    with PLANS_LOCK:
        plan_st = PLANS.get(st["plan_id"])
        if plan_st and "plan" in plan_st:
            p = plan_st["plan"]
            p["unassigned"] = [u for u in p["unassigned"] if u["id"] not in merged_ls]
            for g in p["groups"] + p["without"]:
                g["items"] = [i for i in g["items"] if i["id"] not in merged_ls]
            if any(g["inv"]["id"] in used_docs for g in p["groups"] + p["without"]):
                PLANS.pop(st["plan_id"], None)  # Rechnung hat sich geändert -> Prüfseite neu erstellen
    return page(RESULT, done=done, errors=errors, auto_clean=AUTO_CLEAN_TITLES,
                back=st["plan_id"] if st["plan_id"] in PLANS else None)


def perform_merges(jobs, known_ls, delete_originals, extra):
    """Führt die Zusammenführungen aus. jobs: [(rechnung_summary, [lieferschein_ids])].
    Die Originale (Rechnung und Lieferscheine) gehen immer in den Papierkorb – in Paperless bleibt je Rechnung
    genau ein Dokument übrig. delete_originals wird nur noch aus Kompatibilitätsgründen angenommen.

    Markierung, damit nichts doppelt zusammengeführt wird:
      - Lieferscheine bekommen DONE_TAG (immer, auch wenn sie in den Papierkorb gehen),
      - die neue Rechnung und eine erhalten gebliebene Original-Rechnung bekommen MERGED_TAG.
    """
    pl = Paperless()
    done, errors = [], []
    try:
        done_tag_id = pl.ensure_tag(DONE_TAG)
        merged_tag_id = pl.ensure_tag(MERGED_TAG)
    except Exception as e:  # noqa: BLE001
        return [], [f"Tags „{DONE_TAG}“ / „{MERGED_TAG}“ konnten nicht angelegt werden: {e}"]
    for inv, ls_ids in jobs:
        # Das neue Dokument übernimmt die Tags der Rechnung (Paperless liest sie beim Auslösen des
        # Zusammenführens). Tags der Lieferscheine, Zusatz-Tags und MERGED_TAG daher vorher an die Rechnung.
        wanted = []
        for t in [*(t for lid in ls_ids for t in known_ls[lid].get("tags", [])), *extra, merged_tag_id]:
            if t not in wanted and t not in inv.get("tags", []) and t != done_tag_id:
                wanted.append(t)
        add_now = wanted
        ls_tagged = False
        try:
            pl.modify_tags(ls_ids, add=[done_tag_id])
            ls_tagged = True
            if add_now:
                pl.modify_tags([inv["id"]], add=add_now)
            if clean_title(inv["title"]) != inv["title"]:  # sonst entsteht "(merged) (merged)"
                pl.send("PATCH", f"/api/documents/{inv['id']}/", {"title": clean_title(inv["title"])})
            pl.merge([inv["id"]] + ls_ids, inv["id"], True)  # Originale immer in den Papierkorb
            done.append({"title": clean_title(inv["title"]), "count": len(ls_ids)})
        except Exception as e:  # noqa: BLE001
            errors.append(f"{inv['title']}: {e}")
            try:  # nichts zusammengeführt -> alles wieder wie vorher
                if add_now:
                    pl.modify_tags([inv["id"]], remove=add_now)
                if ls_tagged:
                    pl.modify_tags(ls_ids, remove=[done_tag_id])
            except Exception:  # noqa: BLE001
                pass
            continue
    if done and AUTO_CLEAN_TITLES:
        threading.Thread(target=_auto_clean_titles, args=(len(done),), daemon=True).start()
    return done, errors


@app.route("/execute", methods=["POST"])
@require_auth
def execute():
    with PLANS_LOCK:
        stored = PLANS.pop(request.form.get("plan", ""), None)
    if not stored or "plan" not in stored:
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

    done, errors = perform_merges(jobs, known_ls, stored["delete_originals"], stored.get("extra_tags", []))
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


@app.route("/einrichten", methods=["GET", "POST"])
def setup():
    if not LOGIN_REQUIRED or load_account() is not None:
        return redirect(url_for("index"))
    error, username = None, ""
    writable = os.access(os.path.dirname(USERS_FILE) or ".", os.W_OK) or not os.path.exists(os.path.dirname(USERS_FILE) or ".")
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        error = validate_new_account(username, request.form.get("password", ""), request.form.get("password2", ""))
        if not error:
            try:
                save_account(username, request.form["password"])
            except OSError as e:
                error = f"Zugang konnte nicht gespeichert werden ({e}). Ist das Volume ./data:/data eingebunden?"
        if not error:
            acc = load_account()
            session.clear()
            session.permanent = True
            session.update(user=acc["username"], v=acc["version"])
            return redirect(url_for("index"))
    return page(SETUP, error=error, username=username, writable=writable, data_dir=DATA_DIR, show_nav=False)


@app.route("/anmelden", methods=["GET", "POST"])
def login():
    if not LOGIN_REQUIRED:
        return redirect(url_for("index"))
    acc = load_account()
    if acc is None:
        return redirect(url_for("setup"))
    nxt = _safe_next(request.values.get("next"))
    error, username = None, ""
    if request.method == "POST":
        ip = request.remote_addr or "?"
        username = request.form.get("username", "").strip()
        if _too_many_failures(ip):
            error = "Zu viele Fehlversuche – bitte 10 Minuten warten."
        elif check_credentials(acc, username, request.form.get("password", "")):
            session.clear()
            session.permanent = request.form.get("remember") == "1"
            session.update(user=acc["username"], v=acc["version"])
            return redirect(nxt or url_for("index"))
        else:
            _note_failure(ip)
            time.sleep(1)
            error = "Benutzername oder Passwort falsch."
    info = "Du wurdest abgemeldet." if request.args.get("abgemeldet") else None
    return page(LOGIN, error=error, info=info, username=username, next=nxt, show_nav=False)


@app.route("/abmelden")
def logout():
    session.clear()
    return redirect(url_for("login", abgemeldet=1))


@app.route("/einstellungen/zugang", methods=["POST"])
@require_auth
def change_password():
    acc = load_account()
    msgs = []
    if acc is None or acc["env"]:
        msgs.append(("err", "Der Zugang kann hier nicht geändert werden."))
    elif not check_credentials(acc, acc["username"], request.form.get("current", "")):
        time.sleep(1)
        msgs.append(("err", "Das aktuelle Passwort ist falsch."))
    else:
        username = request.form.get("username", "").strip() or acc["username"]
        error = validate_new_account(username, request.form.get("password", ""), request.form.get("password2", ""))
        if error:
            msgs.append(("err", error))
        else:
            try:
                save_account(username, request.form["password"])
                new = load_account()
                session.update(user=new["username"], v=new["version"])
                msgs.append(("ok", "✓ Zugang geändert. Andere Geräte müssen sich neu anmelden."))
            except OSError as e:
                msgs.append(("err", f"Speichern nicht möglich: {e}"))
    return settings(msgs)


@app.route("/einstellungen", methods=["GET", "POST"])
@require_auth
def settings(msgs=None):
    msgs = list(msgs or [])
    if request.method == "POST" and request.endpoint == "settings":
        f = request.form
        cur = load_settings()
        new = {"ai": dict(cur["ai"]), "ocr": dict(cur["ocr"])}
        new["ocr"]["provider"] = f.get("ocr_provider") if f.get("ocr_provider") in ("off", *OCR_PROVIDERS) else "off"
        new["ocr"]["model"] = f.get("ocr_model", "").strip()
        new["ai"]["provider"] = f.get("ai_provider") if f.get("ai_provider") in ("off", "custom", *AI_PROVIDERS) else "off"
        new["ai"]["model"] = f.get("ai_model", "").strip()
        new["ai"]["base_url"] = f.get("ai_base_url", "").strip() if new["ai"]["provider"] == "custom" else ""
        for sec in ("ai", "ocr"):
            try:
                new[sec]["max_calls"] = max(1, int(f.get(f"{sec}_max_calls") or new[sec]["max_calls"]))
            except ValueError:
                pass
            if f.get(f"{sec}_clear") == "1":
                new[sec]["api_key"] = ""
            if f.get(f"{sec}_api_key", "").strip():
                new[sec]["api_key"] = f.get(f"{sec}_api_key").strip()
        try:
            save_settings(new)
            msgs.append(("ok", "✓ Einstellungen gespeichert."))
        except OSError as e:
            msgs.append(("err", f"Speichern nicht möglich ({e}). Ist das Volume ./data:/data eingebunden?"))
        action = f.get("action")
        if action == "test_ocr":
            o = OCR(budget=1)
            if not o.enabled:
                msgs.append(("err", "Texterkennung ist nicht aktiv – Anbieter und Schlüssel prüfen."))
            else:
                try:
                    o.test()
                    msgs.append(("ok", f"✓ Texterkennung funktioniert ({o.cfg['model']})."))
                except AIError as e:
                    msgs.append(("err", f"Texterkennung fehlgeschlagen: {e}"))
        elif action == "test_ai":
            a = AI(budget=1)
            if not a.cfg["enabled"]:
                msgs.append(("err", "Auswertung ist nicht aktiv – Anbieter, Schlüssel und Modell prüfen."))
            else:
                try:
                    a.test()
                    msgs.append(("ok", f"✓ Auswertung funktioniert ({a.cfg['model']} · {a.cfg['base_url']})."))
                except AIError as e:
                    msgs.append(("err", f"Auswertung fehlgeschlagen: {e}"))
    st = load_settings()
    writable = os.access(os.path.dirname(SETTINGS_FILE) or ".", os.W_OK)
    return page(SETTINGS, msgs=msgs, st=st, ai=ai_cfg(st), ocr=ocr_cfg(st), mask=mask_key,
                ai_defaults={k: v[1] for k, v in AI_PROVIDERS.items()}, ocr_default=OCR_PROVIDERS["mistral"][1],
                settings_file=SETTINGS_FILE, writable=writable, protected=LOGIN_REQUIRED,
                login_required=LOGIN_REQUIRED, account_env=bool(APP_USER and APP_PASSWORD))


@app.route("/audit")
@require_auth
def audit(error=None):
    today = dt.date.today()
    return page(AUDIT_FORM, error=error, start=(today - dt.timedelta(days=365)).isoformat(), end=today.isoformat(),
                max_docs=AUDIT_MAX_DOCS, merged_tag=MERGED_TAG, ocr_enabled=ocr_cfg()["enabled"],
                ocr_model=ocr_cfg()["model"])


@app.route("/audit/start", methods=["POST"])
@require_auth
def audit_start():
    _cleanup_audit_jobs()
    try:
        start = dt.date.fromisoformat(request.form["start"])
        end = dt.date.fromisoformat(request.form["end"])
    except (KeyError, ValueError):
        return audit(error="Ungültige Datumsangabe.")
    if end < start:
        return audit(error="Das Enddatum liegt vor dem Startdatum.")
    job_id = uuid.uuid4().hex
    AUDIT_JOBS[job_id] = {"ts": time.time(), "status": "läuft", "done": 0, "total": 0, "results": [],
                          "start": start.isoformat(), "end": end.isoformat(), "fixed": set(),
                          "dir": tempfile.mkdtemp(prefix="pm-audit-")}
    use_ocr = request.form.get("use_ocr") == "1" and ocr_cfg()["enabled"]
    threading.Thread(target=_run_audit, args=(job_id, start, end, use_ocr), daemon=True).start()
    return redirect(url_for("audit_job", job_id=job_id))


@app.route("/audit/<job_id>")
@require_auth
def audit_job(job_id, info=None):
    job = AUDIT_JOBS.get(job_id)
    if not job:
        return audit(error="Die Prüfung ist abgelaufen. Bitte neu starten.")
    job["ts"] = time.time()
    if job["status"] == "läuft":
        return page(AUDIT_PROGRESS, job=job)
    flagged = [e for e in job["results"] if e["issues"]]
    dup_total = sum(len(e["dups"]) for e in job["results"])
    return page(AUDIT_RESULT, job=job, job_id=job_id, flagged=flagged, dup_total=dup_total,
                titles=job.get("titles", {}), fixed=job["fixed"], link=doc_link, public=PAPERLESS_PUBLIC_URL,
                type_invoice=TYPE_INVOICE)


@app.route("/audit/<job_id>/page/<int:doc_id>/<int:n>.png")
@require_auth
def audit_page(job_id, doc_id, n):
    job = AUDIT_JOBS.get(job_id)
    if not job:
        abort(404)
    png = os.path.join(job["dir"], f"{doc_id}-{n}.png")
    if not os.path.exists(png):
        src = os.path.join(job["dir"], f"{doc_id}.pdf")
        if not os.path.exists(src):
            abort(404)
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(src)
        try:
            if not 1 <= n <= len(pdf):
                abort(404)
            pg = pdf[n - 1]
            pg.render(scale=260 / (pg.get_width() or 595)).to_pil().save(png, "PNG")
        finally:
            pdf.close()
    with open(png, "rb") as f:
        return Response(f.read(), content_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.route("/audit/<job_id>/fix", methods=["POST"])
@require_auth
def audit_fix(job_id):
    job = AUDIT_JOBS.get(job_id)
    if not job or job["status"] != "fertig":
        return audit(error="Die Prüfung ist abgelaufen. Bitte neu starten.")
    pl = Paperless()
    done, errors = [], []
    for e in job["results"]:
        pages = sorted({int(x) for x in request.form.getlist(f"del_{e['id']}") if x.isdigit()})
        pages = [p for p in pages if 1 < p <= len(e["pages"])]
        if not pages or e["id"] in job["fixed"]:
            continue
        try:
            pl.remove_pages(e["id"], pages, len(e["pages"]))
            job["fixed"].add(e["id"])
            done.append({"title": e["title"], "count": len(pages), "pages": pages})
        except Exception as ex:  # noqa: BLE001
            errors.append(f"{e['title']}: {ex}")
    return page(AUDIT_FIXED, done=done, errors=errors, job_id=job_id)


MATCH_NAMES = {0: "keine", 1: "irgendein Wort", 2: "alle Wörter", 3: "exakt", 4: "regulärer Ausdruck",
               5: "ungenaues Wort", 6: "automatisch"}


def _match_info(c):
    algo = c.get("matching_algorithm")
    name = MATCH_NAMES.get(algo, "")
    if algo in (None, 0) or not name:
        return ""
    return f"{name}{(': ' + c['match']) if c.get('match') and algo not in (6,) else ''}"


@app.route("/aufraeumen")
@require_auth
def cleanup_home():
    return page(CLEAN_HOME)


@app.route("/aufraeumen/korrespondenten")
@require_auth
def corr_check(error=None):
    _cleanup_plans()
    try:
        corrs = Paperless().correspondents_full()
    except Exception as e:  # noqa: BLE001
        return page(CLEAN_HOME + '<div class="card err">Fehler bei der Abfrage von Paperless: {{ e }}</div>', e=str(e))
    by_id = {c["id"]: c for c in corrs}
    groups = find_corr_groups(corrs)
    for g in groups:
        tname = by_id[g["target"]]["name"]
        g["target_name"] = tname
        g["rows"] = []
        for m in g["members"]:
            c = by_id[m]
            sc, why = (1.0, "Ziel") if m == g["target"] else corr_similarity(c["name"], tname)
            if m != g["target"] and not why:
                why = "ähnlich über andere Gruppenmitglieder"
            g["rows"].append({"id": m, "name": c["name"], "document_count": c.get("document_count") or 0,
                              "match": _match_info(c), "why": why if m != g["target"] else "",
                              "pre": m == g["target"] or sc >= 0.97})
    grouped = {m for g in groups for m in g["members"]}
    unused = [{**c, "match": _match_info(c)} for c in corrs if not c.get("document_count") and c["id"] not in grouped]
    plan_id = uuid.uuid4().hex
    with PLANS_LOCK:
        PLANS[plan_id] = {"ts": time.time(), "corr": True, "groups": groups, "ids": set(by_id),
                          "unused": {c["id"] for c in unused}, "names": {c["id"]: c["name"] for c in corrs},
                          "match": {c["id"]: (c.get("matching_algorithm"), c.get("match") or "") for c in corrs}}
    return page(CORR_CHECK, error=error, plan_id=plan_id, groups=groups, unused=unused, total=len(corrs),
                all=corrs, names={str(k): v["name"] for k, v in by_id.items()})


@app.route("/aufraeumen/korrespondenten/ausfuehren", methods=["POST"])
@require_auth
def corr_apply():
    with PLANS_LOCK:
        st = PLANS.pop(request.form.get("plan", ""), None)
    if not st or not st.get("corr"):
        return corr_check(error="Die Prüfung ist abgelaufen oder wurde schon ausgeführt. Bitte erneut prüfen.")
    f, names = request.form, st["names"]
    jobs = []  # (ziel, [quellen], neuer_name)
    for gi in f.getlist("groups"):
        if not gi.isdigit() or int(gi) >= len(st["groups"]):
            continue
        g = st["groups"][int(gi)]
        t = f.get(f"t_{gi}", "")
        if not t.isdigit() or int(t) not in g["members"]:
            continue
        target = int(t)
        srcs = [int(x) for x in f.getlist(f"m_{gi}") if x.isdigit() and int(x) in g["members"] and int(x) != target]
        new_name = (f.get(f"n_{gi}") or "").strip()[:128]
        if srcs or (new_name and new_name != names[target]):
            jobs.append((target, srcs, new_name))
    mt = f.get("manual_target", "")
    msrc = [int(x) for x in f.getlist("manual_src") if x.isdigit() and int(x) in st["ids"]]
    if mt.isdigit() and int(mt) in st["ids"]:
        msrc = [x for x in msrc if x != int(mt)]
        if msrc:
            jobs.append((int(mt), msrc, ""))
    # Plausibilität: kein Korrespondent darf gleichzeitig Ziel und zu löschende Quelle sein
    targets = {t for t, _, _ in jobs}
    seen = set()
    for t, srcs, _ in jobs:
        for s_ in srcs:
            if s_ in targets or s_ in seen:
                return corr_check(error=f"„{names[s_]}“ ist mehrfach bzw. gleichzeitig als Ziel ausgewählt. "
                                        "Es wurde nichts geändert.")
            seen.add(s_)
    unused = [int(x) for x in f.getlist("unused") if x.isdigit() and int(x) in st["unused"] and int(x) not in targets]

    pl = Paperless()
    moved = deleted = renamed = 0
    lines, errors = [], []
    for target, srcs, new_name in jobs:
        n_moved, gone = 0, []
        for s_ in srcs:
            try:
                ids = pl.doc_ids_of_correspondent(s_)
                if ids:
                    pl.set_correspondent(ids, target)
                    n_moved += len(ids)
                if pl.doc_ids_of_correspondent(s_):
                    errors.append(f"„{names[s_]}“ hat noch Dokumente (fehlende Berechtigung?) – nicht gelöscht.")
                    continue
                pl.delete_correspondent(s_)
                gone.append(names[s_])
            except Exception as e:  # noqa: BLE001
                errors.append(f"„{names[s_]}“: {e}")
        moved += n_moved
        deleted += len(gone)
        tname = names[target]
        # Suchbegriffe der gelöschten Korrespondenten übernehmen (nur bei "irgendein Wort"/"alle Wörter")
        t_algo, t_match = st["match"].get(target, (None, ""))
        if f.get("keep_match") == "1" and gone and t_algo in (1, 2):
            words = t_match.split()
            for s_ in srcs:
                algo, m = st["match"].get(s_, (None, ""))
                if names[s_] in gone and algo in (1, 2):
                    words += [w for w in m.split() if w.lower() not in {x.lower() for x in words}]
            if " ".join(words) != t_match:
                try:
                    pl.set_correspondent_match(target, " ".join(words))
                    lines.append(f"Suchbegriffe für „{names[target]}“ ergänzt: {' '.join(words)}")
                except Exception as e:  # noqa: BLE001
                    errors.append(f"Suchbegriffe für „{names[target]}“: {e}")
        if new_name and new_name != tname:
            try:
                pl.rename_correspondent(target, new_name)
                renamed += 1
                tname = f"{new_name} (vorher „{names[target]}“)"
            except Exception as e:  # noqa: BLE001
                errors.append(f"Umbenennen von „{names[target]}“ nach „{new_name}“: {e}")
        if gone or n_moved:
            lines.append(f"{', '.join(gone) or '–'} → {tname}: {n_moved} Dokument(e) umgehängt, "
                         f"{len(gone)} Korrespondent(en) gelöscht")
        elif new_name and new_name != names[target]:
            lines.append(f"„{names[target]}“ umbenannt in „{new_name}“")
    gone_unused = []
    for u in unused:
        try:
            if pl.doc_ids_of_correspondent(u):
                errors.append(f"„{names[u]}“ hat inzwischen Dokumente – nicht gelöscht.")
                continue
            pl.delete_correspondent(u)
            gone_unused.append(names[u])
        except Exception as e:  # noqa: BLE001
            errors.append(f"„{names[u]}“: {e}")
    if gone_unused:
        deleted += len(gone_unused)
        lines.append(f"Unbenutzte gelöscht: {', '.join(gone_unused)}")
    return page(CORR_RESULT, moved=moved, deleted=deleted, renamed=renamed, lines=lines, errors=errors)


@app.route("/duplikate")
@require_auth
def dups(error=None):
    today = dt.date.today()
    try:
        types = Paperless().doc_types()
    except Exception as e:  # noqa: BLE001
        types, error = [], error or f"Fehler bei der Abfrage von Paperless: {e}"
    sel = {t["id"] for t in types if t["name"].lower() in (TYPE_INVOICE.lower(), TYPE_DELIVERY.lower())}
    return page(DUPS_FORM, error=error, types=types, sel=sel,
                start=(today - dt.timedelta(days=365)).isoformat(), end=today.isoformat())


@app.route("/duplikate/start", methods=["POST"])
@require_auth
def dups_start():
    _cleanup_audit_jobs()
    try:
        start = dt.date.fromisoformat(request.form["start"])
        end = dt.date.fromisoformat(request.form["end"])
    except (KeyError, ValueError):
        return dups(error="Ungültige Datumsangabe.")
    type_ids = {int(x) for x in request.form.getlist("type") if x.isdigit()}
    if not type_ids:
        return dups(error="Bitte mindestens einen Dokumententyp auswählen.")
    job_id = uuid.uuid4().hex
    AUDIT_JOBS[job_id] = {"ts": time.time(), "status": "läuft", "done": 0, "total": 0, "kind": "dups",
                          "start": start.isoformat(), "end": end.isoformat(), "deleted": set(), "findings": [],
                          "dir": tempfile.mkdtemp(prefix="pm-dups-")}
    threading.Thread(target=_run_dups, args=(job_id, start, end, type_ids), daemon=True).start()
    return redirect(url_for("dups_job", job_id=job_id))


@app.route("/duplikate/<job_id>")
@require_auth
def dups_job(job_id, info=None):
    job = AUDIT_JOBS.get(job_id)
    if not job or job.get("kind") != "dups":
        return dups(error="Das Ergebnis ist abgelaufen. Bitte neu suchen.")
    job["ts"] = time.time()
    if job["status"] == "läuft":
        return page(AUDIT_PROGRESS, job=job, title="🧹 Duplikate werden gesucht …")
    total_drop = sum(len(f["drop"]) for f in job["findings"])
    return page(DOCCARD + DUPS_RESULT, job=job, job_id=job_id, total_drop=total_drop, deleted=job["deleted"],
                info=info, link=doc_link, public=PAPERLESS_PUBLIC_URL)


@app.route("/duplikate/<job_id>/loeschen", methods=["POST"])
@require_auth
def dups_delete(job_id):
    job = AUDIT_JOBS.get(job_id)
    if not job or job.get("kind") != "dups" or job["status"] != "fertig":
        return dups(error="Das Ergebnis ist abgelaufen. Bitte neu suchen.")
    allowed = {x["id"] for f in job["findings"] for x in f["drop"]}
    keeps = {f["keep"] for f in job["findings"]}
    ids = [i for i in dict.fromkeys(int(x) for x in request.form.getlist("del") if x.isdigit())
           if i in allowed and i not in keeps and i not in job["deleted"]]
    if not ids:
        return dups_job(job_id, info="Nichts gelöscht.")
    try:
        Paperless().delete_docs(ids)
        job["deleted"].update(ids)
        info = f"✓ {len(ids)} Dokument(e) in den Paperless-Papierkorb verschoben."
    except Exception as e:  # noqa: BLE001
        info = f"Löschen fehlgeschlagen: {e}"
    return dups_job(job_id, info=info)


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
