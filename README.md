# paperless-merge

Erweiterung für **Paperless-ngx**: sucht Rechnungen und die passenden Lieferscheine in einem
wählbaren Zeitraum, zeigt die Zuordnung zur Kontrolle an und fügt sie erst nach Bestätigung zu
einem PDF zusammen – **Rechnung als erste Seite**, das neue Dokument läuft als Typ **Rechnung**.


## Auf dem Docker-Host
Nur die `docker-compose.yml` anlegen, `DEIN-GITHUB-NAME`, `PAPERLESS_URL` und
`PAPERLESS_TOKEN` eintragen, dann:

```bash
docker compose up -d
```

Aufruf: `http://<server-ip>:8085`

**Update:** Änderung auf GitHub pushen → Action baut neu → auf dem Server
`docker compose pull && docker compose up -d`.

Der Token-Benutzer braucht in Paperless Rechte zum Anzeigen, Ändern, Hinzufügen und Löschen von Dokumenten.

## Bedienung
1. **Zeitraum** (Rechnungsdatum von/bis) eingeben, optional **Korrespondenten** auswählen
   (nichts gewählt = alle; Lieferscheine ohne Korrespondent können mit durchsucht werden) und **Tags** für die neuen Dokumente wählen
   (alle vorhandenen Paperless-Tags, „Posteingang“ ist vorausgewählt). „Lieferscheine zusätzlich X Tage vorher“
   erweitert die Suche nach hinten, weil die Lieferung meist vor der Rechnung liegt.
2. **Prüfseite**: Jede Rechnung mit Vorschaubild und vorgeschlagenen Lieferscheinen.
   - grün *sicher*: Lieferscheinnummer in der Rechnung gefunden (oder umgekehrt)
   - orange *mittel*: Nummer gefunden, aber anderer Korrespondent
   - rot *unsicher*: nur gleicher Korrespondent + Datum → standardmäßig **abgewählt**
   - Häkchen setzen/entfernen, weitere Lieferscheine manuell zuordnen, PDFs per Klick ansehen.
   - Oben stehen die Tags, die die neuen Dokumente zusätzlich bekommen (Standard: Posteingang).
3. **Zusammenführen** → Sicherheitsabfrage → Paperless erstellt die PDFs im Hintergrund.

## Originale
- Haken „in den Papierkorb verschieben“ (Standard): Originale landen im Paperless-Papierkorb
  und sind dort wiederherstellbar.
- Ohne Haken: Originale bleiben, verwendete Lieferscheine bekommen das Tag `LS-zugeordnet`
  und werden nicht erneut angeboten.
- Paperless hängt „(merged)“ an den Titel an – auf der Ergebnisseite gibt es einen Knopf zum Entfernen.

## Erkennung der Lieferscheinnummer
Nummern werden hinter Begriffen wie „Lieferschein“, „Lieferschein-Nr.“, „LS-Nr.“, „Lieferung“,
„Beleg-Nr.“/„Belegnummer“ und „Delivery Note“ gelesen und im jeweils anderen Dokument gesucht.
Der Vergleich ist OCR-tolerant: typische Verwechslungen wie **LI ↔ 11**, O ↔ 0, S ↔ 5, B ↔ 8 werden
als gleich gewertet (z. B. `4LI123456` = `411123456`).

## Einstellungen (Umgebungsvariablen)
| Variable | Standard | Bedeutung |
|---|---|---|
| `PAPERLESS_URL` | `http://paperless:8000` | Adresse von Paperless aus Sicht des Containers |
| `PAPERLESS_TOKEN` | – | API-Token |
| `PAPERLESS_PUBLIC_URL` | leer | optional: Links öffnen direkt in Paperless statt PDF-Ansicht |
| `DOCTYPE_INVOICE` | `Rechnung` | Dokumententyp der Rechnungen |
| `DOCTYPE_DELIVERY` | `Lieferschein` | Dokumententyp der Lieferscheine |
| `DEFAULT_TAGS` | `Posteingang` | vorausgewählte Tags für neue Dokumente (kommagetrennt) |
| `DONE_TAG` | `LS-zugeordnet` | Tag für bereits verwendete Lieferscheine |
| `FALLBACK_DAYS` | `45` | unsichere Vorschläge: Lieferschein max. so viele Tage vor der Rechnung |
| `DEFAULT_LOOKBACK_DAYS` | `60` | Vorbelegung im Formular |
| `VERIFY_SSL` | `true` | bei selbstsigniertem Zertifikat auf `false` |
| `APP_USER` / `APP_PASSWORD` | leer | optionaler Login für die Oberfläche |
