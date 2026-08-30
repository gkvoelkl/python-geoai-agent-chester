# Chester — Agent-Test-Prompts (Benchmarks)

> **Das ist Test-Level 3.** Die Leiter — was auf welcher Stufe geprüft wird und
> warum — steht in [`test-levels.md`](./test-levels.md).
>
> **Umzug am 2026-08-30:** Drei Fälle sind von hier nach **Test-Level 2** gewandert und
> stehen nicht mehr in `agent-test-prompts.jsonl` — `dop-ndvi-no-nir-bayern`
> (→ `ndvi-without-nir`: prüft Selbstkenntnis, keine GIS-Aufgabe; eine Absage ist die
> bestandene Antwort), `total-building-footprint-area` (→ `footprint-area-sum`) und
> `building-height-gini` (→ `height-gini`) — die beiden letzten waren fixture-basiert,
> während die Bank live läuft, und damit in dieser Stufe kaputt (der lange geführte
> Fixture/Live-Mismatch). Die Bank steht seither bei **33 Live-Aufgaben**. Ihre
> Einträge in `.chester/evals/history.jsonl` bleiben erhalten: Ein Report zeigt sie
> weiter, sie sind nur nicht mehr fahrbar.

## 0. Wozu die Bank da ist — die Kompensationsfrage

Die Bank ist keine Regressionssuite mit Extra-Schritten. Sie existiert, um **eine**
Frage zu beantworten:

> **Nicht „Modell oder Werkzeuge?", sondern: wie viel kann ein Werkzeugkasten für
> ein kleines lokales Modell ausgleichen?**

Ein Geo-Agent hat zwei Stellschrauben — das Sprachmodell und das, was es umgibt
(Konnektoren, Werkzeugschnitt, Instruktionen, erzwungene Prüfung). Die verbreitete
Lesart stellt sie gegeneinander („nimm ein größeres Modell"). Chesters These ist,
dass das die falsche Achse ist: Interessant ist nicht, welche der beiden gewinnt,
sondern **wie weit die zweite die erste trägt** — weil die zweite lokal verfügbar,
prüfbar und gestaltbar ist, die erste auf einem Einzelrechner aber nicht.

Der bislang stärkste Beleg ist ein Nebenbefund: Der Agent ließ sich durch
Instruktionen **nicht** davon abbringen, statt auf die Gemeindegrenze auf die
Bounding-Box zu arbeiten — erst ein `warning` im **Rückgabewert** des Werkzeugs
drehte es (Regensburger Schulen 101 → 84 Objekte, GTFS-Haltestellen 1544 → 1225).
Gleiches Modell, gleiche Aufgabe, besserer Werkzeugkasten.

**Wie sich das messen lässt.** Die Bank hält die Aufgaben konstant; variiert werden
zwei Faktoren:

| Zelle | Modell | Werkzeugkasten |
|---|---|---|
| **L+** | ein kleines lokales Modell auf dem Laptop | vollständig — Konnektoren, geprüfte Abkürzungen, Instruktionen, Validierungs-Gate |
| **F−** | ein gehostetes Frontier-Modell | nackt: QGIS-Meta-Werkzeuge und rohe Beschaffung, sonst nichts |

F− behält die Beschaffungswerkzeuge absichtlich — ohne Daten wäre keine Aufgabe der
Bank lösbar, und gemessen würde „ohne Daten geht nichts" statt „wie viel trägt der
Werkzeugkasten". Weggenommen wird nur die **Führung**, nicht die Möglichkeit.

Verglichen werden zwei Bestehensquoten, berichtet in Brüchen (`3/3` gegen `1/3`),
nicht in Prozent: Drei Wiederholungen tragen keinen Prozentpunkt-Vergleich. Der
bbox-Befund sagt voraus, dass die F−-Fehler überwiegend im **Zuschnitt** liegen
(bbox statt Grenze, Grad statt Meter) und nicht in der Beschaffung — eine
Vorhersage, die die Bank falsifizieren kann.

Der Vorversuch ist gelaufen (2026-08-22) und hat vor allem eines gezeigt: Die
Streuung eines lokalen 26B-Modells ist größer als der gesuchte Effekt, **ein Lauf je
Zelle misst nichts**. Daher drei Wiederholungen. Die Zelle F− ist **noch nicht
gebaut**; heute läuft jeder Lauf mit vollem Werkzeugkasten. Was unten steht —
Kategorien, Attribute, Judge, Coverage, Historie — ist der Apparat, auf dem diese
Messung aufsetzt. Der ausführliche Versuchsplan samt Beispiel, Vorversuch und
Abbruchbedingungen steht in [`tool-compensation.md`](./tool-compensation.md).

## Esri-Kategoriendefinitionen

Die Testkategorien (unten) bauen auf Esris Taxonomie der Spatial Analysis auf (Esri, 2013):

1. **Understanding where** — Aufgaben zum Identifizieren, Klassifizieren und Charakterisieren geografischer Orte und ihrer Attribute.
2. **Measuring size, shape, and distribution** — Aufgaben, die Größe, Form, Orientierung und räumliche Verteilung geografischer Phänomene messen und bewerten.
3. **Determining how places are related** — Aufgaben, die Wechselwirkungen, Abhängigkeiten und räumliche Korrelationen zwischen Orten oder Geo-Merkmalen untersuchen.
4. **Finding the best locations and paths** — Aufgaben rund um räumliche Entscheidungen: Standortwahl, Routenoptimierung, Erreichbarkeitsanalyse.
5. **Detecting and quantifying patterns** — Aufgaben zum Erkennen räumlicher Cluster, Trends, Anomalien und Heterogenität in Datensätzen.
6. **Spatial interpolation and predictive modeling** — Aufgaben, die unbekannte Werte an nicht beprobten Orten schätzen sowie raumzeitliche Trends aus historischen Mustern prognostizieren.

Quelle: [The Language of Spatial Analysis](https://www.esri.com/content/dam/esrisites/sitecore-archive/Files/Pdfs/library/books/the-language-of-spatial-analysis.pdf) (Esri, 2013).

## Testkategorien

Die analytische Achse der Bank sind zehn Kategorien. Die Kategorien 3–8 sind die sechs
Esri-Analysekategorien von oben; ergänzt werden sie um vier praktische GIS-Workflow-Stufen
davor und danach — **Data Acquisition** / **Data Preparation / CRS** (Daten holen und
aufbereiten) sowie **Raster / Remote Sensing** / **Output / Cartography** (Wahrnehmung und
Ausgabe):

1. **Data Acquisition**
2. **Data Preparation / CRS**
3. **Understanding Where**
4. **Measuring**
5. **Determining Relationships**
6. **Finding Best Locations / Paths**
7. **Detecting Patterns**
8. **Making Predictions**
9. **Raster / Remote Sensing**
10. **Output / Cartography**

## Prompt-Test-Attribute

Jeder konkrete Test-Prompt wird durch einen festen Satz Attribute beschrieben, sodass Läufe
referenziert, bewertet und (per Runner) ausgeführt werden können, ohne zu driften.

Die konkreten Prompts liegen in [`agent-test-prompts.jsonl`](../agent-test-prompts.jsonl)
— ein JSON-Objekt pro Zeile (JSONL), je Objekt ein Test, per `id` verschlüsselt. Die Bank ist
über die reine Kategorienliste hinaus gewachsen (u. a. mehrere Prompts je Kategorie sowie
DACH-/GTFS-/3D-Szenarien für Schweiz, Österreich und Punktwolken).

### Kernattribute (jeder Prompt braucht sie)

| Attribut | Zweck / Inhalt |
|---|---|
| `id` | Stabiler, aufgabenbeschreibender Slug, z. B. `show-regensburg-buildings` oder `dem-contours-10m`. Zum Referenzieren in Traces/Reports. |
| `category` | 1–10 aus der Kategorienliste (Nummer + Name). Die analytische Achse. |
| `prompt_de` | Der eigentliche Nutzer-Prompt auf Deutsch — genau so, wie ein Nutzer ihn Chester stellen würde. Kein Meta-Text, keine Schritt-für-Schritt-Anweisungen. |
| `expected_behavior` | In Prosa: was der korrekte Agent tut (welche Tool-Klasse, welcher Lösungsweg). Grundlage für den LLM-Judge und für dich beim Lesen des Trace. |
| `success_criteria` | Prüfbare Punkte — der Kern der Bewertung. Am besten als Liste, teils deterministisch (CRS==25832, Ausgabe existiert, Fläche im Bereich), teils qualitativ. |

### Datenabhängigkeit (nötig, sobald ein Prompt Input braucht)

| Attribut | Zweck |
|---|---|
| `required_data` | Welchen Input der Prompt voraussetzt: ein Fixture (`samples/…`), eine Live-Quelle (OSM/STAC/DEM) oder „none". Bestimmt die Reproduzierbarkeit. |
| `data_mode` | `fixture` \| `live` \| `none` — explizit, damit deterministische Läufe von netzabhängigen getrennt werden können. |
| `study_area` | Falls relevant, das feste Gebiet (z. B. „Regensburg"), damit Geocoding/Cache konsistent bleiben. |

### Optionale Metadaten (nützlich fürs Reporting)

- `tools_expected` — grobe Erwartung, welche Chester-Tools feuern sollten (`qgis_run`,
  `fetch_dem`, `detect_water`…). Hilft zu messen, „hat es das richtige Tool gewählt" (wie GIS
  Copilot). Gibt es **mehrere gleichwertige Wege**, werden sie in *einem* Eintrag mit `|`
  aufgezählt — `"qgis_show_3d|render_buildings_3d"` gilt als erfüllt, sobald einer der
  beiden Aufrufe vorkommt. Ohne das zählt jeder Weg als fehlendes Tool des jeweils
  anderen, und ein korrekter Lauf könnte nie 100 % erreichen.
- `notes` — freie Anmerkungen (bekannte Schwächen des lokalen Modells etc.).

## Benchmarks ausführen

Zwei Runner treiben die Bank gegen den Live-Agenten, mit gegensätzlicher Absicht:
`testprompt.py` ist das **Mikroskop** (einen Fall in voller Tiefe inspizieren), `evals.py`
ist das **Teleskop** (die ganze Bank in eine Score-Tabelle benoten). Sie sind keine Dubletten
— `evals.py` importiert und schleift über `testprompt.py`s exakte Funktionen (`build_judge` /
`read_trace` / `judge_run` / `archive_run`), sodass Einzel- und Batch-Urteil nicht driften
können; sie *sind* dieselbe Logik.

| | `testprompt.py` | `evals.py` |
|---|---|---|
| Umfang | ein Test | die ganze Bank (oder eine `--filter`-Teilmenge) |
| Zweck | einen Lauf *lesen* (Debugging) | die Bank *messen* (Regression, Modellvergleich) |
| Ausgabe | voller Live-Trace: jeder Tool-Aufruf + Args + Ergebnis, dann die Antwort | leise; eine Zeile pro Test `[3/34] <id> PASS cov=100%`, dann `k/n PASS` |
| Judge | opt-in (`--judge`) | immer (das ist der Sinn) |
| Standard-Lautstärke | laut (man will den Trace) | leise (`--verbose` streamt jeden Lauf) |
| CI-Gate | — | `--gate` → `exit 1` bei jedem FAIL |
| Report | — | `--report` aggregiert die Historie (kein Lauf) |

Repo-Analogie: `testprompt.py` ↔ `trace.py` (ein Lauf im Detail), `evals.py` ↔ `data.py`
(eine aggregierte Übersicht).

### `testprompt.py` — Einzelfall-Runner

```bash
uv run testprompt.py                      # jeden Test auflisten
uv run testprompt.py <id>                 # einen ausführen
uv run testprompt.py <id> --fresh --show  # GeoCache erst leeren; Karte danach öffnen
uv run testprompt.py <id> --judge         # den fertigen Lauf benoten + das Urteil archivieren
```

Ohne id listet es die Bank; mit id druckt es die Rubrik des Tests (`expected_behavior` +
`success_criteria`) und streamt dann den Agent↔LLM-Tool-Austausch, sodass man ein Szenario
end-to-end beäugen kann. Es borgt sich die exakte Gateway-Verdrahtung
(`Gateway.from_config(...).agent`) und `ask.ask`, ein Testlauf verhält sich also wie eine echte
Gesprächsrunde. Der `/testprompt <id>`-Slash-Befehl fährt dasselbe Szenario in-chat.

**Dazu gehört das Validierungs-Gate**, und zwar als eigene Zeile: `Gateway.from_config(...)`
liefert einen Agenten *ohne* Output-Validator, erst `register_validation_gate` hängt ihn an.
Am **2026-08-22** fiel auf, dass genau dieser Aufruf in `testprompt.py`, `evals.py` und
`test_app.py` fehlte — die Bank maß also einen Agenten **eine Harness-Stufe unter dem
Produkt** (H2 statt H3), obwohl der Text hier „exakte Verdrahtung" sagte. Behoben an allen
drei Stellen, festgenagelt in `tests/test_structure.py`. **Konsequenz für die Historie:
Urteile vor diesem Datum sind ohne Gate zustande gekommen** und mit späteren nur
eingeschränkt vergleichbar.

### `evals.py` — Batch-Runner + Aggregat-Report

```bash
uv run evals.py                 # die ganze Bank laufen + benoten + archivieren, Summary drucken
uv run evals.py --filter crs    # nur Tests, deren id "crs" enthält
uv run evals.py --gate          # exit 1, falls ein Test FAILt (CI-Gate)
uv run evals.py --verbose       # auch jeden Agenten-Lauf streamen
uv run evals.py --report        # kein Lauf; .chester/evals/history.jsonl aggregieren
```

Der Batch ist standardmäßig leise — Dutzende voller Traces würden die Konsole fluten — und
druckt eine Ergebniszeile pro Test plus ein finales `k/n PASS`. `--report` überspringt das
Laufen komplett und aggregiert nur die angesammelte Historie (kein Agent, kein Judge, kein
Netz) über `chester/evalhistory.py`, denselben Formatter, den der `/eval`-Slash-Befehl im Chat
nutzt. Beide zeigen Pass-Rate, mittlere Tool-Coverage und mittlere Aufrufzahl je Modell sowie
das jüngste Urteil je Test; der Filter matcht Test-id **oder** Modell (`--report --filter qwen`).

### Wie ein Lauf benotet wird

Zwei unabhängige Schichten, damit das deterministische Signal nicht dem LLM ausgeliefert ist:

1. **LLM-Judge** — ein strenges, unabhängiges Modell benotet die finale Antwort gegen
   `expected_behavior` / `success_criteria` des Tests und gibt ein strukturiertes `Verdict`
   zurück (Pass/Fail je Kriterium + gesamt + Begründung). Das Judge-Modell kommt aus `evals.judge_model` in
   `.chester/chester.json`, überschreibbar mit `--judge-model <provider/model>`. Es
   **unabhängig** vom getesteten Modell halten — ein sich selbst benotendes Modell ist
   selbstreferenziell, wovor die Runner warnen. Der Judge wird vorab verifiziert, ein
   fehlendes Config scheitert also vor dem (teuren) Agenten-Lauf.
2. **Deterministische Tool-Coverage** — der Anteil von `tools_expected`, der tatsächlich
   feuerte, gelesen aus dem persistierten Session-Trace (`.chester/sessions/<key>.json`), ohne
   LLM. So lässt sich etwa prüfen, ob der Agent `check_crs` *vor* dem Reprojizieren gerufen hat.
   Berechnet von `testprompt.tool_coverage` (Tests: `tests/test_tool_coverage.py`); ein Eintrag
   mit `|` zählt als erfüllt, sobald **eine** seiner Alternativen gerufen wurde — und auch dann
   nur einmal, zwei gerufene Alternativen heben die Coverage also nicht über 100 %.

Jeder benotete Lauf hängt eine Zeile an `.chester/evals/history.jsonl`, die sowohl das getestete
als auch das Judge-Modell trägt — der Log dient so als Regressionsreihe (dasselbe Modell über
die Zeit) und als Modellvergleich. Deshalb sind die obigen Attribute operativ wichtig:
`expected_behavior`/`success_criteria` speisen den Judge, `tools_expected` die deterministische
Coverage-Prüfung.

Jede Zeile trägt außerdem die **Laufzeit**: `duration_s` ist die Wanduhr-Zeit des reinen
Agenten-Zugs, `judge_duration_s` die der Benotung — getrennt, damit ein langsamer Judge den
Modellvergleich nicht verfälscht. Der Report zeigt daraus „avg time" je Modell und die Zeit
je Test; ein `-` heißt „vor Einführung der Messung archiviert", und die Zahl in Klammern
hinter dem Mittelwert (`7.5min (3)`) nennt, auf wie vielen gemessenen Läufen er beruht.
So wird aus der Historie neben „was ging kaputt" auch „was kostet es" — Testdauer je
Modell, je Test und über die Zeit.

### Das Protokoll je Lauf

Jeder Lauf — CLI, Batch oder Bench — hinterlässt zwei Dateien unter
`.chester/evals/runs/`: `<UTC-Zeit>__<test-id>.log` mit dem **zeitgestempelten Protokoll**
(je Zeile Uhrzeit und Abstand zur vorigen Zeile, damit sichtbar wird, *wo* die Laufzeit
hinging) und `<…>.trace.json`, eine Kopie des Session-Traces nach diesem Lauf. Die
Historie verweist über das Feld `log` auf die Protokolldatei — das Urteil sagt *was*
entschieden wurde, das Protokoll ist der einzige Weg zu sehen, *warum*.

Die Kopie ist nötig, weil der Session-Trace **kein** Archiv ist: SelmaKit schreibt ihn je
Session-Schlüssel, der nächste Lauf desselben Tests überschreibt ihn, und `--fresh` löscht
ihn. Ohne Kopie lebte die Aufzeichnung eines Laufs genau bis zum nächsten. Preis dafür sind
einige hundert KB je Lauf; `.chester/` ist nicht im Git.

In der Bench (`./test.sh`) sind daraus drei Ansichten geworden: das Live-Protokoll während
des Laufs, ein **Transcript** (SelmaKits Dashboard-Renderer über die Session-Datei: der
zusammengesetzte System-Prompt, der injizierte Kontext, die Modellausgabe, jeder
Tool-Aufruf mit seinem Ergebnis) und eine Timing-Tabelle mit dem Abstand je Nachricht.

### Aufwand: was der Lauf gekostet hat

Coverage misst die **Reichweite** eines Laufs, nicht seinen **Aufwand** — drei erwartete
Werkzeuge, getroffen in dreißig Aufrufen, sind weiterhin 100 %. Die zweite Hälfte liefert
`testprompt.tool_effort` aus demselben Session-Trace, ebenfalls ohne LLM: `tool_calls` (alle
Aufrufe), `tools_distinct` (wie viele verschiedene), `calls_per_step` (Aufrufe je geplantem
Werkzeug — ein Umwegfaktor) und `tools_offplan` (gerufene Werkzeuge, die kein
`tools_expected`-Eintrag abdeckt). Der Report zeigt „avg calls" je Modell, nach derselben
Regel wie die Zeit: `-` für Läufe von vor der Messung, `(n)` für die Zahl der gezählten Läufe.

**Bewusst ohne Schwellenwert.** An den 36 archivierten Läufen gemessen trennt die Aufrufzahl
die Ausgänge kaum (Median 14 bei PASS, 13 bei FAIL) — ein Budget-Limit würde also korrekte
Läufe treffen. Der Wert liegt im Trend und im Ausreißer (Maximum war 33), nicht in der Note.

`tools_offplan` ist aus demselben Grund eine Liste und keine Präzisions-Kennzahl: dieselbe
Messung zeigt, dass sie von Werkzeugen dominiert wird, die Chesters eigene Regeln *verlangen*
— `geocode` (26 Läufe), `vector_info` (20), `check_crs` (8), `sanity_check_result` (7) —, die
die Bank aber meist nicht listet. Eine Präzisions-Zahl darüber (Median 0.42; 0.47 bei PASS
gegen 0.34 bei FAIL) würde Regeltreue als Unschärfe melden. Lies die Liste andersherum: Ein
Werkzeug, das dort ständig auftaucht, sagt, dass `tools_expected` unvollständig ist — nicht,
dass der Agent umherirrt.

## Verwandte Benchmarks

| Benchmark | Fokus | Bewertung | Jahr | Referenz |
|---|---|---|---|---|
| **GeoBenchX** | mehrschrittige Geo-Aufgaben mit Function Calling, Halluzinations-Vermeidung | LLM-as-Judge, Referenzlösungen | 2025 | [arxiv.org/abs/2503.18129](https://arxiv.org/abs/2503.18129) · [GitHub](https://github.com/Solirinai/GeoBenchX) |
| **GeoAnalystBench** | 50 Python-Aufgaben aus realen Geo-Problemen, Workflow- und Code-Generierung | Workflow-Validität, strukturelle Ausrichtung, CodeBLEU | 2025 | [arxiv.org/abs/2509.05881](https://arxiv.org/abs/2509.05881) · [GitHub](https://github.com/GeoDS/GeoAnalystBench) |
| **GIS Copilot / SpatialAnalysisAgent** | QGIS-integrierter Agent, 100+ Aufgaben über drei Komplexitätsstufen | Erfolgsrate für Tool-Wahl und Code | 2024/25 | [arxiv.org/abs/2411.03205](https://arxiv.org/abs/2411.03205) · [GitHub](https://github.com/Teakinboyewa/SpatialAnalysisAgent) |
