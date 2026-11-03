# paperless-merge

**Aktuelle Version: 0.7** · [Änderungen](#versionen)

Erweiterung für **Paperless-ngx**, die Rechnungen und die dazugehörigen Lieferscheine findet und
zu **einem PDF** zusammenführt:

- **Rechnung immer als erste Seite**, danach die Lieferscheine nach Datum
- das neue Dokument läuft als Dokumententyp **Rechnung**, übernimmt Korrespondent und Speicherpfad der Rechnung
  sowie **alle Tags der Rechnung und der zugehörigen Lieferscheine**
- **nichts passiert ohne Kontrolle** – alle Zuordnungen werden vorher auf einer Prüfseite angezeigt
- **nichts wird doppelt zusammengeführt** – bearbeitete Rechnungen und Lieferscheine werden markiert und beim nächsten Lauf ausgeschlossen
- frei wählbarer **Zeitraum**, Auswahl der **Korrespondenten** und der **Tags** für die neuen Dokumente
- OCR-tolerante Erkennung der Lieferscheinnummer (z. B. `LI` ↔ `11`)
- **Suche in der ganzen Paperless-Instanz** nach Rechnungen zu Lieferscheinen, die nicht zugeordnet werden konnten

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
  - [4. Rechnungen zu Lieferscheinen suchen](#4-rechnungen-zu-lieferscheinen-suchen)
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
| Bereits ergänzte Rechnungen ausblenden | Rechnungen, die schon mit Lieferscheinen zusammengeführt wurden, werden nicht erneut angeboten – siehe [Schutz vor doppeltem Zusammenführen](#schutz-vor-doppeltem-zusammenführen). Standard: an. |

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
gefundenen Lieferschein, **freie** Lieferscheine, ggf. **bereits ergänzte (ausgeblendete)** Rechnungen und
**schon enthaltene** Lieferscheine, gewählte Korrespondenten, zusätzliche Tags und ob die Originale gelöscht werden.

Darunter steht jede Rechnung als eigene Karte (auf breiten Bildschirmen mehrere nebeneinander):
- **Vorschaubild** und Link **PDF ansehen** (bzw. *in Paperless öffnen*, wenn `PAPERLESS_PUBLIC_URL` gesetzt ist)
- Datum, Korrespondent und ggf. ASN
- **Lieferschein-Nr. in der Rechnung:** alle Lieferscheinnummern, die im Text der Rechnung gefunden wurden
- darunter die vorgeschlagenen Lieferscheine mit Bewertung und Begründung:

| Markierung | Bedeutung | Vorauswahl |
|---|---|---|
| 🟢 **sicher** | Lieferscheinnummer in der Rechnung gefunden (oder die Rechnung nennt eine Nummer, die im Lieferschein steht) – auch OCR-tolerant | angehakt |
| 🟠 **mittel** | Nummer gefunden, aber Rechnung und Lieferschein haben **unterschiedliche Korrespondenten** | angehakt – bitte prüfen |
| 🔴 **unsicher** | keine Nummer gefunden, nur gleicher Korrespondent und passendes Datum | **nicht** angehakt |

Bei jedem vorgeschlagenen Lieferschein steht außerdem **Erkannte Nr.:** – die Nummern, die im Lieferschein
erkannt wurden. Die Nummer, über die Rechnung und Lieferschein zugeordnet wurden, ist **grün mit ✓**
markiert, bei der Rechnung und beim Lieferschein. So ist auf einen Blick zu sehen:
- welche Nummer die Zuordnung ausgelöst hat (auch wenn sie OCR-bedingt unterschiedlich geschrieben ist, z. B. `4LI123456` ↔ `411123456`),
- welche Nummern in der Rechnung stehen, zu denen **kein** Lieferschein gefunden wurde (graue Nummer ohne ✓),
- bei welchen Lieferscheinen gar keine Nummer erkannt wurde (*keine erkannt*).

Auch in der Liste der nicht zugeordneten Lieferscheine und im Auswahlfeld zum manuellen Hinzufügen
stehen die erkannten Nummern.

**Lieferscheine, die schon in einer ergänzten Rechnung stecken**
Steht die Nummer eines Lieferscheins in einer Rechnung, die bereits mit Lieferscheinen zusammengeführt wurde
(z. B. ein doppelt eingescannter oder aus dem Papierkorb wiederhergestellter Lieferschein), wird er **nicht**
erneut vorgeschlagen. Er erscheint im eingeklappten Abschnitt *… stecken schon in einer ergänzten Rechnung*
mit Verweis auf die Rechnung. Mit **Ausgewählte als zugeordnet markieren** bekommt er das Tag `LS-zugeordnet`
und taucht künftig gar nicht mehr auf.

**Nicht zugeordnete Lieferscheine** können dort ebenfalls angehakt und mit **Ausgewählte als erledigt markieren**
dauerhaft ausgeschlossen werden – z. B. Lieferscheine, zu denen es nie eine Rechnung geben wird.

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

### 4. Rechnungen zu Lieferscheinen suchen
Lieferscheine, zu denen im gewählten Zeitraum keine Rechnung gefunden wurde, können gezielt in der
**ganzen Paperless-Instanz** gesucht werden – auch weit außerhalb des Zeitraums und auch in Dokumenten,
die schon einmal zusammengeführt wurden.

**Starten**
- Auf der Prüfseite unter **nicht zugeordnete Lieferscheine** die gewünschten Lieferscheine anhaken
  (*alle auswählen* markiert alle) und **🔍 Ausgewählte in Paperless suchen** klicken.
- Bei Lieferscheinen mit nur einem *unsicheren* Vorschlag steht direkt der Knopf **🔍 Rechnung in Paperless suchen**.

**Vorgaben** (vorbelegt mit den Werten aus der `docker-compose.yml`, siehe [Einstellungen](#einstellungen))
| Vorgabe | Bedeutung |
|---|---|
| Gesuchte Nummern | je Lieferschein vorbelegt mit den erkannten Nummern – **änderbar**, z. B. wenn die OCR die Nummer falsch gelesen hat. Mehrere Nummern mit Komma trennen. |
| Dokumententypen | in welchen Typen gesucht wird (Standard: *Rechnung*), z. B. zusätzlich *Gutschrift* |
| Tage vor / nach dem Lieferscheindatum | Zeitfenster rund um das Lieferscheindatum (Standard: 30 Tage vorher bis 365 Tage nachher) |
| ohne Zeitbegrenzung suchen | ignoriert das Zeitfenster und durchsucht alle Dokumente |
| nur beim gleichen Korrespondenten suchen | schränkt auf den Korrespondenten des Lieferscheins ein. Lieferscheine ohne Korrespondent werden immer bei allen gesucht. |

**Ergebnis**
- Für jeden Lieferschein werden die gefundenen Dokumente angezeigt – mit Typ, Datum, Korrespondent,
  Abstand zum Lieferscheindatum und der **gefundenen Nummer** (grün, ggf. mit Hinweis *OCR-tolerant*).
- Die Treffer sind nach Nähe zum Lieferscheindatum sortiert. Gibt es genau einen Treffer, ist er vorausgewählt,
  sonst steht die Auswahl auf *nicht zuordnen*.
- Vorgaben ändern und **Suchen** startet die Suche neu.
- **Ausgewählte zusammenführen** führt jeden Lieferschein mit dem gewählten Dokument zusammen – nach denselben
  Regeln wie oben: Rechnung zuerst, alle Tags, Originale wie auf der Startseite gewählt, „(merged)“ wird bereinigt.
  Mehrere Lieferscheine zur selben Rechnung werden in einem Durchgang angehängt.
- Danach geht es mit **← Zurück zur Prüfseite** weiter; die zugeordneten Lieferscheine sind dort entfernt.
  Wurde eine Rechnung verwendet, die auch auf der Prüfseite steht, muss die Prüfseite neu erstellt werden.

---

## Was mit den Originalen passiert
| | Originale in den Papierkorb (Standard) | Originale behalten |
|---|---|---|
| Rechnung + Lieferscheine | wandern in den **Paperless-Papierkorb** und sind dort wiederherstellbar | bleiben unverändert erhalten |
| Lieferscheine | bekommen das Tag **`LS-zugeordnet`** (auch im Papierkorb – wichtig, falls sie wiederhergestellt werden) | bekommen das Tag **`LS-zugeordnet`** |
| Rechnung | neues Dokument bekommt das Tag **`LS-ergänzt`** | neues Dokument **und** Original-Rechnung bekommen das Tag **`LS-ergänzt`** |
| Tags von Lieferscheinen und Zusatz-Tags | am neuen Dokument | am neuen Dokument – die Original-Rechnung behält nur ihre eigenen Tags |

### Tags des neuen Dokuments
Das zusammengeführte Dokument bekommt **alle Tags zusammen**:
1. die Tags der **Rechnung**,
2. die Tags **aller zugehörigen Lieferscheine**,
3. die auf der Startseite gewählten **Zusatz-Tags** (z. B. *Posteingang*).

Dazu kommt das Tag `LS-ergänzt` (siehe unten). Doppelte Tags werden nur einmal gesetzt. Das Tag `LS-zugeordnet` wird nie übernommen.
Technisch werden die Tags kurz vor dem Zusammenführen an die Rechnung gehängt, weil Paperless die
Metadaten des neuen Dokuments von der Rechnung übernimmt. Bleiben die Originale erhalten, werden diese
Tags danach an der Original-Rechnung wieder entfernt.

Für den ersten Lauf empfiehlt es sich, die Originale zu **behalten** und einen kurzen Zeitraum zu wählen.

### Schutz vor doppeltem Zusammenführen
Damit eine Rechnung nicht bei jedem Lauf erneut mit ihren Lieferscheinen zusammengeführt wird, werden beim
Zusammenführen **immer** zwei Tags gesetzt (beide werden bei Bedarf automatisch angelegt):

| Tag | an | Wirkung beim nächsten Lauf |
|---|---|---|
| `LS-zugeordnet` | jedem verwendeten Lieferschein | Lieferschein wird nicht mehr gesucht |
| `LS-ergänzt` | der neuen Rechnung (und einer erhaltenen Original-Rechnung) | Rechnung wird ausgeblendet |

Zusätzlich erkennt die Erweiterung auch **ältere** zusammengeführte Rechnungen ohne Tag – Paperless legt das
zusammengeführte PDF immer mit dem Dateinamen `…_merged.pdf` an. Das gilt auch für Dokumente, die direkt in
Paperless zusammengeführt wurden.

Ausgeblendete Rechnungen werden trotzdem für die Zuordnung verwendet: Steht die Nummer eines Lieferscheins in
einer schon ergänzten Rechnung, wird dieser Lieferschein keiner anderen Rechnung zugeschlagen, sondern als
*schon enthalten* angezeigt. *Unsichere* Vorschläge (nur Korrespondent + Datum) gehen nie an ausgeblendete Rechnungen.

Soll an eine schon ergänzte Rechnung bewusst ein weiterer Lieferschein angehängt werden:
- auf der Startseite **Bereits ergänzte Rechnungen ausblenden** abschalten – die Rechnungen erscheinen dann mit dem
  Hinweis *bereits ergänzt*, oder
- den Lieferschein über [Rechnungen zu Lieferscheinen suchen](#4-rechnungen-zu-lieferscheinen-suchen) zuordnen –
  auch dort sind solche Rechnungen mit *bereits ergänzt* gekennzeichnet.

Um eine Rechnung oder einen Lieferschein wieder freizugeben, einfach das Tag in Paperless entfernen.

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
6. **Bereits ergänzt:** Lieferscheine mit `LS-zugeordnet` werden gar nicht geladen. Rechnungen mit `LS-ergänzt`
   oder Dateiname `…_merged.pdf` werden nicht angeboten, binden aber Lieferscheine mit eindeutigem Nummerntreffer.
7. **Eindeutigkeit:** Jeder Lieferschein wird höchstens **einer** Rechnung zugeordnet – der mit dem besten
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
| `MERGED_TAG` | `LS-ergänzt` | Tag für Rechnungen, an die Lieferscheine angehängt wurden (wird bei Bedarf automatisch angelegt) |
| `FALLBACK_DAYS` | `45` | Zeitfenster für *unsichere* Vorschläge: Lieferschein max. so viele Tage vor der Rechnung |
| `DEFAULT_LOOKBACK_DAYS` | `60` | Vorbelegung für „Lieferscheine zusätzlich Tage vorher“ |
| `VERIFY_SSL` | `true` | bei HTTPS mit selbstsigniertem Zertifikat auf `false` setzen |
| `SEARCH_DOCTYPES` | `Rechnung` | Suche zu Lieferscheinen: vorausgewählte Dokumententypen, kommagetrennt |
| `SEARCH_DAYS_BEFORE` | `30` | Suche zu Lieferscheinen: Tage vor dem Lieferscheindatum |
| `SEARCH_DAYS_AFTER` | `365` | Suche zu Lieferscheinen: Tage nach dem Lieferscheindatum |
| `SEARCH_UNLIMITED` | `false` | Suche zu Lieferscheinen: standardmäßig ohne Zeitbegrenzung |
| `SEARCH_SAME_CORR` | `true` | Suche zu Lieferscheinen: standardmäßig nur beim gleichen Korrespondenten |
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

### Version 0.7 (03.11.2026)
- **Schutz vor doppeltem Zusammenführen:** Lieferscheine bekommen beim Zusammenführen immer das Tag `LS-zugeordnet` (auch wenn die Originale in den Papierkorb gehen), Rechnungen das neue Tag `LS-ergänzt` (neues Dokument und ggf. erhaltene Original-Rechnung)
- bereits ergänzte Rechnungen werden beim nächsten Lauf ausgeblendet – erkannt am Tag oder am Paperless-Dateinamen `…_merged.pdf`, also auch ältere Zusammenführungen
- Lieferscheine, deren Nummer schon in einer ergänzten Rechnung steht, werden nicht erneut vorgeschlagen und gesondert angezeigt
- neue Knöpfe zum dauerhaften Ausschließen: *Ausgewählte als zugeordnet markieren* und *Ausgewählte als erledigt markieren*
- Option *Bereits ergänzte Rechnungen ausblenden* auf der Startseite; Kennzeichnung *bereits ergänzt* auch in der Suche
- interne Tags `LS-zugeordnet` / `LS-ergänzt` werden bei der Tag-Auswahl nicht mehr angeboten

### Version 0.6 (03.11.2026)
- neue Funktion: **Rechnungen zu Lieferscheinen suchen** – nicht zugeordnete Lieferscheine auswählen und in der ganzen Paperless-Instanz nach Dokumenten suchen, die deren Nummer enthalten
- Suchvorgaben (Dokumententypen, Zeitfenster, ohne Zeitbegrenzung, gleicher Korrespondent) mit Standardwerten aus der `docker-compose.yml`
- gesuchte Nummern pro Lieferschein änderbar; Suche OCR-tolerant
- Treffer direkt auswählen und zusammenführen; Suchknopf auch bei *unsicheren* Vorschlägen
- Rückkehr zur Prüfseite nach der Suche

### Version 0.5 (03.11.2026)
- Prüfseite zeigt bei jeder Rechnung die darin gefundenen Lieferscheinnummern
- bei jedem Lieferschein werden die erkannten Nummern angezeigt
- die Nummer, über die zugeordnet wurde, ist grün mit ✓ markiert; Nummern ohne passenden Lieferschein bleiben grau
- erkannte Nummern auch bei nicht zugeordneten Lieferscheinen und im Auswahlfeld zum manuellen Hinzufügen

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
