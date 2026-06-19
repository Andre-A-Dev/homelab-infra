---
tags:
  - api
  - bundesagentur
  - jobsuche
  - reverse-engineering
  - python
  - referenz
created: 2026-06-19
updated: 2026-06-19
status: Done
---

# Bundesagentur für Arbeit – Jobsuche API v6 (inoffiziell)

> [!warning] Inoffizielle API
> Diese API ist nicht öffentlich dokumentiert. Alle Erkenntnisse basieren auf
> Reverse Engineering der offiziellen Jobsuche-Website sowie empirischen Tests.
> Änderungen können jederzeit ohne Ankündigung erfolgen. Das offizielle
> `openapi.yaml` auf `bundesAPI/jobsuche-api` (GitHub) dokumentiert noch das
> veraltete v4-Schema – für v6 nicht verlässlich.

---

## Basis-URL

```
GET https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs
```

---

## Authentifizierung

Kein OAuth, kein persönlicher Account. Zwei Header sind erforderlich:

```http
X-API-Key: jobboerse-jobsuche
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36
```

> [!warning] User-Agent-Pflicht
> Der Standard-`python-requests`-User-Agent (`python-requests/x.x`) wird von
> der WAF mit **HTTP 403** geblockt, sobald zusätzliche Filterparameter
> (`arbeitszeit`, `angebotsart`, `veroeffentlichtseit`) gesetzt sind. Ein
> Browser-ähnlicher User-Agent ist daher zwingend erforderlich.
>
> Ohne Filter-Parameter funktioniert der Default-UA (empirisch bestätigt) –
> das Blocking-Verhalten scheint parameterabhängig und möglicherweise
> nicht-deterministisch zu sein.

```python
import requests

HEADERS = {
    "X-API-Key": "jobboerse-jobsuche",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}
```

---

## Request-Parameter

### Pflichtparameter (praktisch)

Technisch gibt es keine harten Pflichtparameter – ein Request ohne `was`/`wo`
liefert HTTP 200 mit generischen Ergebnissen (empirisch: bundesweite Stellen
ohne Geo-Filterung). Für sinnvolle Suchergebnisse sind jedoch erforderlich:

| Parameter | Typ | Beschreibung |
|---|---|---|
| `was` | string | Suchbegriff (Berufsbezeichnung, Tätigkeit). Wird als AND-Suche über mehrere Wörter interpretiert. Leer-String erlaubt (alle Stellen). |
| `wo` | string | Ort: Stadtname **mit Umlaut** (z.B. `Nürnberg`), PLZ (z.B. `90402`) oder Bundesland (z.B. `Bayern`). Alternativ: `koordinaten`-Parameter. |

> [!warning] Ortsnamen ohne Umlaut
> `Nuernberg` statt `Nürnberg` liefert `woOutput.suchmodus: "UNGUELTIG"` und
> 0 Treffer. Immer Umlaute verwenden oder auf `koordinaten` ausweichen.

### Geo-Parameter

| Parameter | Typ | Beschreibung |
|---|---|---|
| `wo` | string | Ortsname, PLZ oder Bundesland (siehe oben) |
| `umkreis` | integer | Suchradius in km um `wo`. Ohne Angabe: API wählt Default (empirisch: ~25 km). |
| `koordinaten` | string | Alternativ zu `wo`: `"lat,lon"` (z.B. `"49.4521,11.0767"`). Liefert `woOutput.suchmodus: "UMKREISSUCHE_MIT_KOORDINATEN"`. |

**`woOutput.suchmodus`-Werte:**

| Wert | Bedeutung |
|---|---|
| `UMKREISSUCHE` | Stadtname oder PLZ korrekt aufgelöst |
| `UMKREISSUCHE_MIT_KOORDINATEN` | Koordinaten-Suche |
| `BUNDESLANDSUCHE` | Bundesland erkannt (kein Radius, gesamtes Bundesland) |
| `UNGUELTIG` | Ort nicht erkannt (0 Ergebnisse) |

### Paginierung

| Parameter | Typ | Beschreibung |
|---|---|---|
| `page` | integer | Seitennummer, 1-basiert |
| `size` | integer | Ergebnisse pro Seite. **Maximum: 250** (bei `size=500` → HTTP 400). Bei `size=100`/`200` werden alle verfügbaren Treffer zurückgegeben (kein künstliches Cap unter 250 beobachtet). |

**Pagination-Verhalten:**
- `maxErgebnisse` gibt die Gesamtanzahl aller Treffer an
- Seiten jenseits der letzten liefern HTTP 200 mit leerem `ergebnisliste`-Array (kein Fehler)
- `ceil(maxErgebnisse / size)` ergibt die letzte gültige Seite

### Filter-Parameter

| Parameter | Typ | Werte | Beschreibung |
|---|---|---|---|
| `arbeitszeit` | string | Siehe Tabelle unten | Arbeitszeitmodell-Filter. Mehrere Werte mit `;` kombinierbar. |
| `angebotsart` | integer | `1` | Angebotstyp. Nur `1` (ARBEIT) liefert Ergebnisse für reguläre Stellen. |
| `veroeffentlichtseit` | integer | 0–100+ | Nur Stellen der letzten N Tage. `0` = heute (0 Ergebnisse beobachtet). API scheint einen internen Cap bei ~30 Tagen zu haben (30/60/100 lieferten identische Trefferzahl). |
| `pav` | string | `"false"` / `"true"` | Privater Arbeitsvermittler. `"false"` = nur direkte Arbeitgeber (empfohlen); `"true"` = nur PAV-Angebote (deutlich weniger Treffer). |
| `berufsfeld` | string | Freitext | Berufsfeld-Filter. Gültige Werte aus `facetten.berufsfeld.counts`. Z.B. `"Informatik"`, `"Elektrotechnik"`, `"Softwareentwicklung und Programmierung"`. |
| `arbeitgeber` | string | Freitext | Arbeitgeberfilter. Leerer `was`-Wert bei Verwendung → HTTP 400. Scheint auf exakten/internen Firmennamen angewiesen zu sein (Siemens/BMW/Bosch/Continental lieferten 0 Treffer mit umkreis=200). |

**`arbeitszeit`-Enum-Werte:**

| Wert | Bedeutung | Response-Felder |
|---|---|---|
| `vz` | Vollzeit | `arbeitszeitVollzeit: true` |
| `tz` | Teilzeit | `arbeitszeitTeilzeit*`-Felder |
| `snw` | Schicht / Nacht / Wochenende | `arbeitszeitSchichtNachtWochenende: true` |
| `ho` | Home Office | `homeofficemoeglich: true` |
| `mj` | Minijob | `istGeringfuegigeBeschaeftigung: true` (Hypothese) |

> [!warning] `ho`-Filter kaum wirksam
> `arbeitszeit=ho` liefert für `Software Engineer` in Nürnberg 0 Treffer,
> obwohl Items mit `homeofficemoeglich: true` existieren (empirisch). Viele
> Arbeitgeber – besonders im Automotive/Embedded-Bereich – taggen Home-Office
> nicht explizit. Für maximale Trefferzahl `arbeitszeit`-Filter weglassen und
> stattdessen `homeofficemoeglich`/`homeofficetyp` aus dem Response auswerten.

**Kombination:** Mehrere Werte mit `;` trennbar: `vz;ho`, `vz;tz`, `tz;ho`.
Kombination wirkt als OR (nicht AND): `vz;ho` liefert identische Trefferzahl
wie `vz` allein (empirisch bestätigt).

---

## Response-Format

### Top-Level

```json
{
  "ergebnisliste": [...],
  "maxErgebnisse": 94,
  "page": 1,
  "size": 3,
  "woOutput": {...},
  "facetten": {...}
}
```

| Feld | Typ | Beschreibung |
|---|---|---|
| `ergebnisliste` | array | Liste der Stellenangebote (siehe Item-Schema) |
| `maxErgebnisse` | integer | Gesamtzahl aller Treffer (unabhängig von `size`/`page`) |
| `page` | integer | Aktuelle Seite |
| `size` | integer | Angeforderte Seitengröße |
| `woOutput` | object | Aufgelöste Geo-Information (siehe oben) |
| `facetten` | object | Aggregationen für UI-Filter (siehe unten) |

### Item-Schema (`ergebnisliste[]`)

Vollständiges Feldverzeichnis, empirisch ermittelt. `—` = Feld fehlt in diesem
Item (nicht `null`, sondern absent).

| Feld | Typ | Beschreibung |
|---|---|---|
| `referenznummer` | string | Eindeutige ID (z.B. `"10001-1003112368-S"`). Als Dedup-Key geeignet. |
| `stellenangebotsTitel` | string | Stellenbezeichnung |
| `stellenangebotsart` | string | Bisher nur `"ARBEIT"` beobachtet |
| `firma` | string | Arbeitgeberbezeichnung |
| `hauptberuf` | string | Primäre Berufsbezeichnung (normalisiert) |
| `alternativBeruf1` | string | Alternative Berufsbezeichnung |
| `alternativBeruf2` | string | Weitere alternative Berufsbezeichnung |
| `weitereBerufe` | array\<string\> | Weitere Berufsbezeichnungen/Keywords |
| `alleBerufe` | array\<string\> | Alle Berufe inkl. Haupt- und Alternativberufe |
| `stellenlokationen` | array | Standorte (siehe Unter-Schema) |
| `entfernung` | integer | Entfernung in km vom gesuchten Ort |
| `eintrittszeitraum` | object | `{"von": "YYYY-MM-DD"}` – frühestes Eintrittsdatum |
| `datumErsteVeroeffentlichung` | string | Erstveröffentlichung (`YYYY-MM-DD`) |
| `veroeffentlichungszeitraum` | object | `{"von": "YYYY-MM-DD"}` |
| `aenderungsdatum` | string | Letzte Änderung (ISO 8601 mit Zeit) |
| `vertragsdauer` | string | `UNBEFRISTET` / `KEINE_ANGABE` |
| `arbeitszeitVollzeit` | boolean | Vollzeitstelle |
| `arbeitszeitSchichtNachtWochenende` | boolean | Schicht/Nacht/Wochenende |
| `arbeitszeitTeilzeitVormittag` | boolean | Teilzeit vormittags |
| `arbeitszeitTeilzeitNachmittag` | boolean | Teilzeit nachmittags |
| `arbeitszeitTeilzeitAbend` | boolean | Teilzeit abends |
| `arbeitszeitTeilzeitFlexibel` | boolean | Flexible Teilzeit |
| `istGeringfuegigeBeschaeftigung` | boolean | Minijob |
| `homeofficemoeglich` | boolean | Home Office möglich |
| `homeofficetyp` | string | `ANGABE_IN_PROZENT` / `NACH_VEREINBARUNG` |
| `homeofficeprozent` | integer | Anteil Home Office in % (wenn `homeofficetyp=ANGABE_IN_PROZENT`) |
| `verguetungsangabe` | string | `KEINE_ANGABEN` / `JAHRESGEHALT` / weitere Enum-Werte |
| `artDerVerguetung` | string | `GEHALTSSPANNE` / fehlt meist |
| `gehaltsspanneVon` | float | Jahresgehalt von (EUR, wenn `artDerVerguetung=GEHALTSSPANNE`) |
| `gehaltsspanneBis` | float | Jahresgehalt bis (EUR) |
| `chiffrenummer` | string | Chiffre (bei anonymen Anzeigen) |
| `externeURL` | string | Direkt-Link zur Stellenanzeige (oft nicht vorhanden) |
| `arbeitgeberKundennummerHash` | string | Hash der Arbeitgeber-ID (für Dedup über Firma) |

**`stellenlokationen[]`-Unter-Schema:**

```json
{
  "adresse": {
    "strasse": "Dombühler Str.",
    "hausnummer": "2",
    "plz": "90449",
    "ort": "Nürnberg, Mittelfranken",
    "region": "BAYERN",
    "land": "DEUTSCHLAND"
  },
  "breite": 49.421,
  "laenge": 11.032
}
```

`strasse`/`hausnummer` fehlen bei nicht allen Einträgen. `ort` enthält oft
den Regionszusatz (`"Nürnberg, Mittelfranken"`).

### Facetten (`facetten`)

Aggregationen über alle Treffer – nützlich für Filter-UIs oder Statistiken:

| Facette | Beschreibung |
|---|---|
| `befristung` | Befristungsart: `1`=unbefristet, `2`=?, `3`=? (Zählwerte) |
| `verguetung` | Vergütungsangaben: `"jahr"` = Jahresgehalt angegeben |
| `externestellenboersen` | Ob Anzeige von externer Jobbörse: `true`/`false` |
| `behinderung` | Stellen für Menschen mit Behinderung: `false` = keine Einschränkung |
| `pav` | Privater Arbeitsvermittler-Anteil |
| `berufsfeld` | Berufsfelder mit Anzahl (nutzbar als `berufsfeld`-Parameterwerte) |
| `arbeitsort` | Orte mit Anzahl |
| `arbeitsort_plz` | PLZ mit Anzahl |
| `veroeffentlichtseit` | Treffer nach Alter: `1`/`7`/`14`/`28`/`alle` Tage |
| `weitereberufe` | Weitere Berufsbezeichnungen mit Anzahl |

---

## Fehlerformat

Bei Validierungsfehlern (HTTP 400):

```json
{
  "timestamp": "2026-06-19T17:11:30.040228326Z",
  "logref": "98c19",
  "messages": [
    {
      "code": "EINGABEN_UNVOLLSTAENDIG_ODER_FEHLERHAFT",
      "path": "size",
      "detail": "must be less than or equal to 250"
    }
  ]
}
```

| HTTP | Ursache |
|---|---|
| 200 | Erfolg (auch bei 0 Treffern) |
| 400 | Validierungsfehler (z.B. `size > 250`, `arbeitgeber` ohne `was`) |
| 403 | Falscher/fehlender API-Key, oder WAF-Block (User-Agent) |

---

## Rate Limiting

Kein hartes Rate-Limit beobachtet: 10 schnelle Requests ohne Delay lieferten
alle HTTP 200. Response-Header `X-API-BLOCKED: 0` deutet auf ein
Blocking-System hin, das bei moderater Nutzung nicht greift. Kein
`Retry-After`- oder `X-RateLimit-*`-Header sichtbar.

Empfehlung: 1–2 Sekunden Delay zwischen Requests als Courtesy gegenüber der
öffentlichen API.

---

## Minimales Python-Beispiel

```python
import requests

API_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
HEADERS = {
    "X-API-Key": "jobboerse-jobsuche",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

resp = requests.get(API_BASE, headers=HEADERS, params={
    "was": "Build Engineer",
    "wo": "Nürnberg",
    "umkreis": 75,
    "page": 1,
    "size": 50,
    "pav": "false",
    "angebotsart": 1,
    "veroeffentlichtseit": 7,
})

data = resp.json()
print(f"{data['maxErgebnisse']} Treffer")
for job in data["ergebnisliste"]:
    ort = job["stellenlokationen"][0]["adresse"].get("ort", "?") \
          if job.get("stellenlokationen") else "?"
    url = job.get("externeURL") or \
          f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{job['referenznummer']}"
    print(f"- {job['stellenangebotsTitel']} | {job.get('firma', '?')} | {ort}")
    print(f"  {url}")
```

---

## Offene Fragen / Unbekannt

- **`angebotsart`-Werte 2/4/16/32/34/64**: alle lieferten 0 Treffer für
  `was=Software Engineer` – ob diese Werte für andere Suchbegriffe
  (Ausbildung, Praktikum) Ergebnisse liefern, wurde nicht getestet.
- **`befristung`-Facette**: Werte `1`/`2`/`3` – genaue Bedeutung unklar
  (1=unbefristet ist Hypothese).
- **`arbeitgeber`-Parameter**: Scheint auf exakten internen Firmennamen
  angewiesen zu sein; Siemens/BMW/Bosch/Continental lieferten 0 Treffer.
  Möglicherweise ist ein Hash/ID statt Klartext erforderlich.
- **`mj`-Wert bei `arbeitszeit`**: 0 Treffer – könnte valider Wert sein,
  der für IT-Stellen schlicht nicht vorkommt.