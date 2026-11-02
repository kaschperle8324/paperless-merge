# paperless-merge

Erweiterung für **Paperless-ngx**: sucht Rechnungen und die passenden Lieferscheine in einem
wählbaren Zeitraum, zeigt die Zuordnung zur Kontrolle an und fügt sie erst nach Bestätigung zu
einem PDF zusammen – **Rechnung als erste Seite**, das neue Dokument läuft als Typ **Rechnung**.

## Einmalig: GitHub einrichten
1. Auf GitHub ein neues Repository `paperless-merge` anlegen (privat oder öffentlich).
2. Den Inhalt dieses Ordners hochladen (Web-Upload oder `git push`).
3. Unter **Actions** startet der Build automatisch und legt das Image unter
   `ghcr.io/<dein-name>/paperless-merge:latest` ab (amd64 + arm64).
4. **Privates Repo:** Das Image ist dann ebenfalls privat. Entweder unter
   *Profil → Packages → paperless-merge → Package settings* auf **Public** stellen
   oder auf dem Server einmal anmelden:
   `docker login ghcr.io -u <dein-name>` (Passwort = Personal Access Token mit `read:packages`).

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
1. **Zeitraum** (Rechnungsdatum von/bis) eingeben und **Tags** für die neuen Dokumente wählen
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
