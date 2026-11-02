# paperless-merge

**Aktuelle Version: 0.4** · [Änderungen](#versionen)

Erweiterung für **Paperless-ngx**, die Rechnungen und die dazugehörigen Lieferscheine findet und
zu **einem PDF** zusammenführt:

- **Rechnung immer als erste Seite**, danach die Lieferscheine nach Datum
- das neue Dokument läuft als Dokumententyp **Rechnung**, übernimmt Korrespondent und Speicherpfad der Rechnung
  sowie **alle Tags der Rechnung und der zugehörigen Lieferscheine**
- **nichts passiert ohne Kontrolle** – alle Zuordnungen werden vorher auf einer Prüfseite angezeigt
- frei wählbarer **Zeitraum**, Auswahl der **Korrespondenten** und der **Tags** für die neuen Dokumente
- OCR-tolerante Erkennung der Lieferscheinnummer (z. B. `LI` ↔ `11`)

Die Erweiterung läuft als eigener Docker-Container neben Paperless und arbeitet ausschließlich über
die Paperless-API. Paperless selbst wird nicht verändert.

---

## Inhalt
- [Voraussetzungen](#voraussetzungen)
- [Installation](#installation)
- [Aktualisieren](#aktualisieren)
- [Bedienung](#bedienung)
  - [1. Startseite](#1-startseite)
  - [2. Prüfseite](#2-prüfseite)
  - [3. Zusammenführen und Ergebnis](#3-zusammenführen-und-ergebnis)
- [Was mit den Originalen passiert](#was-mit-den-originalen-passiert)
- [Wie die Zuordnung funktioniert](#wie-die-zuordnung-funktioniert)
- [Einstellungen](#einstellungen)
- [Fehlerbehebung](#fehlerbehebung)
- [Versionen](#versionen)

---

## Voraussetzungen
- Paperless-ngx (Version 2.x) mit den Dokumententypen **Rechnung** und **Lieferschein**
  (andere Namen über `DOCTYPE_INVOICE` / `DOCTYPE_DELIVERY` einstellbar)
- ein **API-Token**: in Paperless oben rechts auf das Profil → *API-Auth-Token*
- der Benutzer, zu dem der Token gehört, braucht Rechte zum **Anzeigen, Ändern, Hinzufügen und Löschen** von Dokumenten

## Installation
Das fertige Image liegt unter `ghcr.io/kaschperle8324/paperless-merge:latest` (amd64 + arm64).
Es wird nur eine `docker-compose.yml` benötigt – in Portainer einfach als neuen Stack einfügen:

```yaml
services:
  paperless-merge:
    image: ghcr.io/kaschperle8324/paperless-merge:latest
    container_name: paperless-merge
    restart: unless-stopped
    ports:
      - "8085:8080"
    environment:
      PAPERLESS_URL: "http://192.168.1.50:8000"   # Adresse von Paperless (IP:Port oder Containername)
      PAPERLESS_TOKEN: "HIER_TOKEN_EINTRAGEN"     # API-Auth-Token aus Paperless
      DEFAULT_TAGS: "Posteingang"                 # vorausgewählte Tags für neue Dokumente
      TZ: "Europe/Berlin"
```

`PAPERLESS_URL` und `PAPERLESS_TOKEN` anpassen und starten:

```bash
docker compose up -d
```

Aufruf im Browser: **`http://<server-ip>:8085`** – die laufende Version steht oben rechts in der Oberfläche.

Statt `latest` kann auch eine feste Version verwendet werden, z. B.
`ghcr.io/kaschperle8324/paperless-merge:0.3`. Dann wird erst aktualisiert, wenn die Versionsnummer
in der Compose-Datei geändert wird.

> **Hinweis:** Die Oberfläche hat standardmäßig keinen Login. Entweder nur im Heimnetz betreiben,
> hinter einen Reverse-Proxy mit Anmeldung hängen oder `APP_USER` / `APP_PASSWORD` setzen.

## Aktualisieren
```bash
docker compose pull && docker compose up -d
```
**Portainer:** *Stacks* → Stack öffnen → *Editor* → **Update the stack** → „Re-pull image and redeploy“ aktivieren → *Update*.
Danach die Seite im Browser mit **Strg + F5** neu laden. Ob die neue Version läuft, zeigt die
Versionsnummer oben rechts in der Oberfläche.

---

## Bedienung
Die Oberfläche nutzt die volle Bildschirmbreite, passt sich an Handy und Tablet an und schaltet
automatisch in den **Dark Mode**, wenn das Gerät dunkel eingestellt ist.

### 1. Startseite
Auf der Startseite werden nur Einstellungen getroffen – **es wird noch nichts verändert**.

**Zeitraum & Optionen**
| Feld | Bedeutung |
|---|---|
| Rechnungsdatum von / bis | Es werden Rechnungen mit Dokumentdatum in diesem Zeitraum gesucht. Vorbelegt: Anfang des Vormonats bis heute. |
| Lieferscheine zusätzlich Tage vorher | Lieferscheine liegen meist vor der Rechnung. Hier wird festgelegt, wie viele Tage vor dem Startdatum noch Lieferscheine berücksichtigt werden (Standard 60). |
| Originale in den Papierkorb verschieben | Siehe [Was mit den Originalen passiert](#was-mit-den-originalen-passiert). Standard: an. |

**Korrespondenten**
- Alle Korrespondenten aus Paperless werden als anklickbare Felder mit der Anzahl ihrer Dokumente angezeigt.
- **Nichts ausgewählt = alle Korrespondenten** werden durchsucht.
- Ab mehr als 12 Einträgen erscheint ein **Suchfeld**; *Auswahl leeren* hebt alle Markierungen auf.
- **Lieferscheine ohne Korrespondent mit durchsuchen** (Standard: an): Lieferscheine, denen in Paperless
  kein Korrespondent zugeordnet ist, werden ebenfalls berücksichtigt und über ihre Nummer zugeordnet.

**Tags für die neuen Dokumente**
- Alle vorhandenen Paperless-Tags erscheinen in ihren Paperless-Farben.
- Die ausgewählten Tags bekommt das zusammengeführte Dokument **zusätzlich** zu den Tags, die Rechnung und
  Lieferscheine ohnehin schon haben (siehe [Tags des neuen Dokuments](#tags-des-neuen-dokuments)).
- Vorausgewählt sind die Tags aus `DEFAULT_TAGS` (Standard: *Posteingang*).
- Ab mehr als 12 Tags erscheint ein Suchfeld.

Mit **Suchen & Vorschläge anzeigen** geht es zur Prüfseite.

**Titel bereinigen** (unten auf der Startseite): entfernt jederzeit nachträglich jedes „(merged)“ aus allen
Dokumenttiteln in Paperless – auch mehrfache wie „Rechnung 4711 (merged) (merged)“. Vor dem Ausführen
kommt eine Sicherheitsabfrage.

### 2. Prüfseite
Oben steht eine Übersicht: Zeitraum, Anzahl der Rechnungen **mit Vorschlag**, Rechnungen **ohne**
gefundenen Lieferschein, **freie** Lieferscheine, gewählte Korrespondenten, zusätzliche Tags und ob
die Originale gelöscht werden.

Darunter steht jede Rechnung als eigene Karte (auf breiten Bildschirmen mehrere nebeneinander):
- **Vorschaubild** und Link **PDF ansehen** (bzw. *in Paperless öffnen*, wenn `PAPERLESS_PUBLIC_URL` gesetzt ist)
- Datum, Korrespondent und ggf. ASN
- darunter die vorgeschlagenen Lieferscheine mit Bewertung und Begründung:

| Markierung | Bedeutung | Vorauswahl |
|---|---|---|
| 🟢 **sicher** | Lieferscheinnummer in der Rechnung gefunden (oder die Rechnung nennt eine Nummer, die im Lieferschein steht) – auch OCR-tolerant | angehakt |
| 🟠 **mittel** | Nummer gefunden, aber Rechnung und Lieferschein haben **unterschiedliche Korrespondenten** | angehakt – bitte prüfen |
| 🔴 **unsicher** | keine Nummer gefunden, nur gleicher Korrespondent und passendes Datum | **nicht** angehakt |

**Anpassen**
- Haken an der **Rechnung** entfernen → diese Rechnung wird nicht bearbeitet.
- Haken an einem **Lieferschein** entfernen → dieser Lieferschein bleibt außen vor.
- **Weitere Lieferscheine manuell hinzufügen:** Im Auswahlfeld unter jeder Rechnung stehen alle noch nicht
  zugeordneten Lieferscheine (Mehrfachauswahl mit Strg / Cmd). Das Anhaken eines Lieferscheins setzt
  automatisch auch den Haken an der Rechnung.
- **Rechnungen ohne gefundenen Lieferschein** und **nicht zugeordnete Lieferscheine** stehen eingeklappt
  am Ende der Seite – auch dort kann manuell zugeordnet werden.

Die Leiste mit **Abbrechen** und **Ausgewählte zusammenführen** bleibt beim Scrollen unten sichtbar.

### 3. Zusammenführen und Ergebnis
- Vor dem Ausführen kommt eine **Sicherheitsabfrage** mit der Anzahl der Rechnungen.
- Ist ein Lieferschein versehentlich **mehreren Rechnungen** zugeordnet, wird **nichts** ausgeführt und ein Hinweis angezeigt.
- Paperless erstellt die neuen PDFs im Hintergrund – sie erscheinen nach ein bis zwei Minuten.
- **Titelzusatz „(merged)“:** Paperless hängt beim Zusammenführen „(merged)“ an den Titel an. Das wird jetzt
  automatisch behandelt:
  - Enthält der Titel der Rechnung schon „(merged)“ (z. B. weil sie früher schon einmal zusammengeführt wurde),
    wird es **vorher entfernt** – der Zusatz kann sich so nicht stapeln.
  - Nach dem Zusammenführen entfernt die Erweiterung „(merged)“ **automatisch** aus den neuen Titeln, sobald
    Paperless die Dokumente angelegt hat (sie prüft dafür bis zu 5 Minuten im Hintergrund).
  - Zusätzlich gibt es auf der Ergebnisseite den Knopf **„(merged)“ jetzt aus allen Titeln entfernen** und auf der
    Startseite den Knopf **Titel bereinigen** für die nachträgliche Bereinigung.
- Ein Vorschlag ist **eine Stunde** gültig und kann nur **einmal** ausgeführt werden – danach neu suchen.

---

## Was mit den Originalen passiert
| | Originale in den Papierkorb (Standard) | Originale behalten |
|---|---|---|
| Rechnung + Lieferscheine | wandern in den **Paperless-Papierkorb** und sind dort wiederherstellbar | bleiben unverändert erhalten |
| Lieferscheine beim nächsten Lauf | – | bekommen das Tag **`LS-zugeordnet`** und werden nicht mehr vorgeschlagen |
| Tags von Lieferscheinen und Zusatz-Tags | am neuen Dokument | am neuen Dokument – die Original-Rechnung behält nur ihre eigenen Tags |

### Tags des neuen Dokuments
Das zusammengeführte Dokument bekommt **alle Tags zusammen**:
1. die Tags der **Rechnung**,
2. die Tags **aller zugehörigen Lieferscheine**,
3. die auf der Startseite gewählten **Zusatz-Tags** (z. B. *Posteingang*).

Doppelte Tags werden nur einmal gesetzt. Das interne Tag `LS-zugeordnet` wird nie übernommen.
Technisch werden die Tags kurz vor dem Zusammenführen an die Rechnung gehängt, weil Paperless die
Metadaten des neuen Dokuments von der Rechnung übernimmt. Bleiben die Originale erhalten, werden diese
Tags danach an der Original-Rechnung wieder entfernt.

Für den ersten Lauf empfiehlt es sich, die Originale zu **behalten** und einen kurzen Zeitraum zu wählen.

## Wie die Zuordnung funktioniert
1. **Nummern finden:** Im OCR-Text werden Nummern hinter Begriffen wie *Lieferschein*, *Lieferschein-Nr.*,
   *Lieferscheinnummer*, *LS-Nr.*, *Lieferung*, *Beleg-Nr.*, *Belegnummer* und *Delivery Note* gelesen.
   Hat ein Lieferschein keine solche Nummer, wird eine lange Ziffernfolge aus dem Titel verwendet.
2. **In beide Richtungen suchen:** Die Lieferscheinnummer wird in der Rechnung gesucht – und Nummern, die die
   Rechnung nennt, im Lieferschein. Führende Nullen werden ignoriert.
3. **OCR-tolerant:** Wird nichts gefunden, folgt ein zweiter Vergleich, der typische Texterkennungsfehler
   als gleich wertet: **L / I / | → 1**, **O / Q → 0**, **S → 5**, **B → 8**, **Z → 2**.
   So passt z. B. `4LI123456` zu `411123456`.
4. **Plausibilität:** Kurze Nummern (unter 5 Ziffern) zählen nur bei gleichem Korrespondenten.
   Unterschiedliche Korrespondenten stufen einen Treffer auf *mittel* herab.
5. **Rückfallebene:** Ohne Nummerntreffer wird ein Lieferschein desselben Korrespondenten vorgeschlagen,
   der bis zu `FALLBACK_DAYS` (Standard 45) Tage vor bis 3 Tage nach der Rechnung datiert ist – immer als *unsicher*.
6. **Eindeutigkeit:** Jeder Lieferschein wird höchstens **einer** Rechnung zugeordnet – der mit dem besten
   Treffer bzw. dem nächstliegenden Datum. Dafür werden intern auch Rechnungen kurz vor und nach dem
   gewählten Zeitraum berücksichtigt, angezeigt werden aber nur Rechnungen aus dem Zeitraum.

---

## Einstellungen
Alle Einstellungen werden als Umgebungsvariablen in der `docker-compose.yml` gesetzt.

| Variable | Standard | Bedeutung |
|---|---|---|
| `PAPERLESS_URL` | `http://paperless:8000` | Adresse von Paperless aus Sicht des Containers (IP:Port oder Containername im selben Docker-Netz) |
| `PAPERLESS_TOKEN` | – | API-Auth-Token |
| `PAPERLESS_PUBLIC_URL` | leer | optional: Adresse, unter der Paperless im Browser erreichbar ist. Dann öffnen die Links direkt in Paperless statt in der PDF-Ansicht |
| `DOCTYPE_INVOICE` | `Rechnung` | Name des Dokumententyps für Rechnungen |
| `DOCTYPE_DELIVERY` | `Lieferschein` | Name des Dokumententyps für Lieferscheine |
| `DEFAULT_TAGS` | `Posteingang` | vorausgewählte Tags für neue Dokumente, mehrere durch Komma getrennt |
| `DONE_TAG` | `LS-zugeordnet` | Tag für bereits verwendete Lieferscheine (wird bei Bedarf automatisch angelegt) |
| `FALLBACK_DAYS` | `45` | Zeitfenster für *unsichere* Vorschläge: Lieferschein max. so viele Tage vor der Rechnung |
| `DEFAULT_LOOKBACK_DAYS` | `60` | Vorbelegung für „Lieferscheine zusätzlich Tage vorher“ |
| `VERIFY_SSL` | `true` | bei HTTPS mit selbstsigniertem Zertifikat auf `false` setzen |
| `AUTO_CLEAN_TITLES` | `true` | „(merged)“ nach dem Zusammenführen automatisch aus den Titeln entfernen |
| `APP_USER` / `APP_PASSWORD` | leer | optionaler Login für die Oberfläche (beide setzen) |
| `TZ` | – | Zeitzone, z. B. `Europe/Berlin` |

---

## Fehlerbehebung
| Meldung / Problem | Ursache und Lösung |
|---|---|
| *Dokumententyp „Rechnung“ oder „Lieferschein“ nicht gefunden* | Die Namen müssen exakt wie in Paperless lauten – sonst `DOCTYPE_INVOICE` / `DOCTYPE_DELIVERY` anpassen. |
| *Fehler bei der Abfrage von Paperless* (Verbindungsfehler) | `PAPERLESS_URL` prüfen. Am einfachsten die IP mit Port eintragen, z. B. `http://192.168.1.50:8000`. |
| *HTTP 401* oder *403* | Token falsch oder dem Benutzer fehlen Rechte. |
| *Tags/Korrespondenten konnten nicht geladen werden* | wie oben – Verbindung oder Token prüfen. |
| Neue Dokumente erscheinen nicht | Paperless verarbeitet im Hintergrund – kurz warten. Bei Problemen in Paperless unter *Dateiaufgaben* nachsehen. |
| Lieferscheine werden nicht gefunden | Prüfen, ob die Nummer im OCR-Text (Paperless → Dokument → *Inhalt*) hinter einem der oben genannten Begriffe steht. Bessere Erkennung: mit 300 dpi scannen, `PAPERLESS_OCR_LANGUAGE: deu` setzen, Dokument in Paperless *erneut verarbeiten*. |
| Vorschlag „abgelaufen“ | Vorschläge gelten eine Stunde und nur für einen Lauf – einfach neu suchen. |
| Alte Oberfläche nach Update | Browser-Cache: Seite mit **Strg + F5** neu laden. |

## Versionen
Die Versionsnummer steht in der Datei `VERSION`, oben rechts in der Oberfläche, im Image-Tag
(`ghcr.io/kaschperle8324/paperless-merge:<version>`) und als Release auf GitHub.

### Version 0.4 (02.11.2026)
- „(merged)“ kann sich bei mehreren Durchläufen nicht mehr stapeln: ein vorhandener Zusatz wird vor dem Zusammenführen aus dem Rechnungstitel entfernt
- „(merged)“ wird nach dem Zusammenführen automatisch aus den neuen Titeln entfernt (abschaltbar mit `AUTO_CLEAN_TITLES`)
- neuer Knopf **Titel bereinigen** auf der Startseite: entfernt nachträglich jedes „(merged)“ aus allen Dokumenttiteln, auch mehrfache
- die Bereinigung erfasst jetzt alle Dokumente mit „(merged)“ im Titel, nicht nur Rechnungen mit dem Zusatz am Ende

### Version 0.3 (02.11.2026)
Erste versionierte Ausgabe mit folgenden Funktionen:
- Suche nach Rechnungen und Lieferscheinen in einem frei wählbaren Zeitraum, mit einstellbarem Vorlauf für Lieferscheine
- Prüfseite mit Vorschaubildern, PDF-Ansicht und Bewertung *sicher* / *mittel* / *unsicher*
- Zuordnungen an- und abwählen, Lieferscheine manuell hinzufügen, Schutz vor Doppelzuordnung, Sicherheitsabfrage
- Zusammenführen mit der **Rechnung als erster Seite**, neues Dokument als Typ *Rechnung*
- neues Dokument erhält **alle Tags von Rechnung und Lieferscheinen** plus frei wählbare Zusatz-Tags (Vorauswahl *Posteingang*)
- Auswahl der **Korrespondenten** (durchsuchbar, mit Dokumentanzahl), Lieferscheine ohne Korrespondent optional
- Originale wahlweise in den Papierkorb oder behalten (dann Tag `LS-zugeordnet`)
- OCR-tolerante Nummernerkennung (`LI` ↔ `11`, `O` ↔ `0` …) und Schlüsselwörter *Beleg-Nr.* / *Belegnummer*
- Knopf zum Entfernen des Titelzusatzes „(merged)“
- modernes Design mit Dark Mode, volle Bildschirmbreite, Versionsanzeige

## Lizenz
Siehe [LICENSE](LICENSE).
