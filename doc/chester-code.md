# Chester-Code — der Coder

Chester gibt es jetzt in drei Besetzungen über **derselben** Werkzeugschicht:

| | Wie die Arbeit geschieht |
|---|---|
| `chester-agent` | ein Agent, ein Werkzeugaufruf pro Schritt |
| `chester-team` ([`chester-team.md`](./chester-team.md)) | ein Orchestrator verteilt die Arbeit an Ressort-Agenten |
| `chester-code` | ein Agent schreibt **ein Python-Programm**, das die ganze Aufgabe erledigt |

Alle drei laufen auf `chester-runtime`, mit demselben Gate und denselben Werkzeugen.
Bewegt wird nur die Architektur. Den Aufbau der Pakete beschreibt die
[Code-Map](./code-map.md).

**Stand (24.10.2026):** gebaut, Test-Level 1 grün, Pilot mit vier Tests (unten). Trägt, wo
Quelle und Kette feststehen; scheitert, wo erst Daten gesucht werden müssen. Der nächste
Ausbau — Anforderung, Quelle, Programm — steht unten.

## Der Anlass

Chesters Aufgaben sind Ketten: Daten holen, auf das Gebiet zuschneiden, umprojizieren,
rechnen, Karte zeichnen. Der Einzelagent geht sie Zug um Zug, 12 bis 35 Modellzüge pro
Lauf, jeder mit 30–45 s Ausgabe auf dem lokalen Modell. Die Idee von CodeMode: Das
Modell schreibt die Kette einmal als Programm, die Werkzeuge laufen darin, die
Zwischenergebnisse gehen nicht durch den Gesprächsverlauf.

Als zuschaltbarer Modus in chester-agent hat das nicht getragen
([`codemode-concept.md`](./codemode-concept.md) §8): Die Modelle riefen pro Programm
nur ein Werkzeug auf, weil der Einzelagent an fünf Stellen auf Einzelschritte angelegt
ist — Planung, Instruktionen, Werkzeugkatalog, Skills, die REPL-Beschreibung. Ein
eigener Agent ohne diese Gegenkräfte ist sauberer als fünf Ausnahmen im bestehenden.

## Die Architektur

```
Nutzer → Coder (SelmaKit-Agent) ── run_code ──► Programm (Monty-Sandbox)
                                                  ├─ await osm_features(...)
                                                  ├─ await vector_clip(...)
                                                  ├─ await geo_python_run(code=...)   ← geopandas, wo kein Werkzeug passt
                                                  └─ await render_map(...)
```

- **Ein einziges Werkzeug, strikt.** Das Modell sieht nur `run_code` — sonst nichts,
  auch keine eingebauten Werkzeuge des Anbieters. Darin stehen als `async`-Funktionen mit
  Signatur und Docstring: alle 83 Werkzeuge der Hüllenschicht, `geo_python_run`,
  `inspect_map` **und SelmaKits eigene** (`memory_search`, `memory_write`,
  `duckduckgo_search`, `web_fetch`). CodeMode läuft mit `tools="all"`. Die Web-Werkzeuge
  brauchten einen Umweg: In SelmaKits Capabilities `WebSearch`/`WebFetch` sind sie der
  lokale Ersatz eines anbietereigenen Werkzeugs, und CodeMode lässt solche Werkzeuge
  draußen — mit dem Profil des echten Modells standen `duckduckgo_search` und
  `web_fetch` neben `run_code` (24.10.2026). Jetzt fallen die beiden Capabilities weg, und
  dieselben Funktionen kommen als gewöhnliche Werkzeuge ins Toolset des Coders. Ein
  Test baut das ganze Gateway und prüft, dass das Modell nur `run_code` bekommt.
- **Kurze Instruktionen.** Rund 4.100 Zeichen statt rund 40.000 beim Einzelagenten. Sie
  beginnen mit dem Rahmen „eine Aufgabe, ein Programm" und zeigen eine ganze Aufgabe als
  Beispielprogramm. Ein Test prüft jeden Aufruf darin gegen die echten Signaturen.
- **Kein Plan.** `Planning` und `PlanGuardCapability` fallen weg — das Programm ist der
  Plan.
- **Keine Skills.** Ein Skill ist ein Rezept aus Einzelaufrufen, das Gegenteil eines
  Programms.
- **Kein Auslagern großer Rückgaben.** `ToolOutputLimits` fällt weg: Sein Hook greift
  auch bei inneren Aufrufen und würde dem Programm eine Textvorschau statt eines Dicts
  liefern. Die Instruktion verlangt stattdessen, nur Pfade und Zahlen zurückzugeben;
  `ResultSizeGuardCapability` fängt ab, was trotzdem zu groß ist (über 12.000 Zeichen):
  Pfade und Zahlen bleiben, ganze Layer werden zu einem Platzhalter mit Hinweis.
- **Typisierte Rückgaben.** Die Werkzeuge der Hüllenschicht geben `dict` zurück; für die 15
  meistgenutzten hängt `code_tools()` einen `TypedDict` aus `chester/toolreturns.py` an. Der
  `run_code`-Stub zeigt dem Modell damit z. B. `features: int` mit der Zeile „a count, not a
  list“ statt `-> dict` — gemma4 hatte `len(r["features"])` geschrieben. Kosten: rund
  1.600 Token Werkzeugbeschreibung.
- **Statische Prüfung vor dem Lauf.** `ProgramCheckCapability` liest das Programm, bevor es
  läuft: ein Puffer auf Grad-Koordinaten, nach dem Aufteilen nur die Punkte behalten, OSM
  per bbox eines benannten Orts ohne Zuschnitt, ein Puffer, wo die Aufgabe eine Reisezeit
  verlangt — dann läuft es nicht, und das Modell bekommt Zeile und Abhilfe. Ein zweites Mal derselbe
  Befund lässt es laufen (ein Fehlalarm kostet eine Runde).
- **Ein Guard für „ein Programm".** `OneProgramGuardCapability` hängt an das erste
  `run_code`, das nur ein Werkzeug rief, eine Korrektur an — dort, wo das Modell sie
  nicht überlesen kann. Einmal pro Lauf, weil ein letztes Programm nur mit `render_map`
  berechtigt ist.
- **Das Gate gilt unverändert.** `selmakit.tool_returns` packt die inneren Aufrufe aus;
  das Gate sieht dieselben Pfade und Warnungen wie beim Einzelagenten. Trace und Bench
  lesen über `load_flat_session` (`chester.runtime.codemode`).

## Bedienen

```sh
uv run ask_code.py "Lege eine 500-Meter-Einzugszone um alle Schulen in Regensburg an."
CHESTER_AGENT=code uv run testprompt.py buffer-schools-500m --fresh --judge
```

`CHESTER_AGENT=code` gilt für die ganze Bench; jeder Eintrag in
`.chester/evals/history.jsonl` trägt die Architektur (`agent: code`), damit sich die drei
nie vermischen.

```sh
./start_code.sh          # Gateway (:8200) + Dashboard (:8701)
./test_code.sh           # Test-Bench (:8702) — oder ./test.sh --code
```

Die Ports stehen im Block `code` von `.chester/chester.json` (`webchat_port`,
`dashboard_port`, `bench_port`); so laufen Agent (8000/8501), Team (8100/8601/8602) und
Coder nebeneinander. Telegram startet der Coder nie.

## Was vorher geprüft wurde (24.10.2026, ohne echtes Modell)

| Frage | Ergebnis |
|---|---|
| Laufen Hooks wie Werkzeugprotokoll und Guards für innere Aufrufe? | ja, und ihre Änderungen erreichen das Programm |
| Zählt die Laufzeit eines Werkzeugs gegen die 30-s-Grenze der Sandbox? | nein |
| Sieht das Gate die inneren Ergebnisse? | ja, einzeln und vor dem `run_code`-Eintrag |
| Was macht ein `ModelRetry` aus einem inneren Werkzeug? | einen Retry für das ganze Programm, mit Traceback |

## Erste Läufe (24.10.2026, llama.cpp, `buffer-schools-500m`, `--fresh`; Läufe 1–5 gemma4, 6–7 qwen3.6-coder)

| Lauf | was geschah | Laufzeit | Urteil (glm-4.7-flash) |
|---|---|---|---|
| 1 | **ein ganzes Programm** (`osm_features` → `vector_reproject` → `vector_buffer` → `render_map`, mit `assert`) — aber als Markdown **in die Antwort** geschrieben, nie ausgeführt; das Gate meldete die vier genannten Dateien als fehlend | 123 s | FAIL |
| 2 | nach geschärfter Anweisung und der Prüfung auf nicht ausgeführte Programme: **ein `run_code` mit der ganzen Kette** (5 Werkzeuge, alle Werkzeugzeit zusammen 2,7 s), dann ein kleines Prüfprogramm; 84 Puffer in EPSG:25832, Karte angesehen | 230 s | FAIL bei „auf das Stadtgebiet zugeschnitten" |
| 3 | strikt, nur `run_code`: ein ganzes Programm, eine korrigierte Wiederholung (`len()` auf eine Zahl) | 268 s | FAIL bei „zugeschnitten" |
| 4 | Zuschnitt auch des Ergebnisses verlangt; das Programm rief `validate_result` (eine Hülle von chester-mcp) mit dem Karten-PNG auf, bekam „unreadable, must_fix" und probierte danach herum — abgebrochen | — | — |
| 5 | MCP-Hüllen draußen, `inspect_result` überspringt Ansichten; Judge nur nach Ergebnis (`CHESTER_JUDGE=result`): fünf Anläufe desselben Programms (fehlendes `output_path`, zweimal Zählfehler), Ergebnis zugeschnitten | 707 s | FAIL bei „500 m" — aus den Layer-Fakten nicht ablesbar |
| 6 | wie 5, **qwen3.6-coder**: erstes Programm fehlerfrei, die ganze Kette wie im Beispiel (split → merge → reproject → buffer → `fetch_boundaries` → clip → Karte), dann ein Prüfprogramm | 394 s | **PASS** (alle vier Kriterien; nachbewertet, nicht archiviert — die erste Bewertung stürzte an einem Bench-Fehler ab, siehe unten) |
| 7 | wie 6, dazu der Hinweis „`features` ist eine Anzahl" und der Ersatzradius in den Layer-Fakten: der erste Download der amtlichen Grenzen brach ab (`IncompleteRead`), das `assert` fing es, das zweite Programm lief komplett | 250 s | **PASS** (archiviert; der Judge las „≈ 500 m") |

Das Urteil von Lauf 2 ist ein Judge-Fehler: Das Transkript zeigte ihm
`osm_features(place="Regensburg", …)`, und `place=` ist ausdrücklich eine der beiden
erlaubten Formen des Kriteriums. Zum Vergleich: chester-agent brauchte für denselben
Fall mit gemma4 236–898 s (vier frühere Läufe).

Daraus kam eine zweite Sicherung: **`make_unrun_program_check`**, ein
`output_validator` vor dem Gate. Enthält die Antwort ein Programm mit `await` auf
Chester-Werkzeuge, ohne dass im Lauf `run_code` aufgerufen wurde, gibt es einen Retry
mit dem Auftrag, es auszuführen.

**Bench-Fehler aus Lauf 6:** CodeMode legt die `print()`-Ausgabe eines Programms unter
`output` ab; `testprompt._produced_paths` hielt sie für einen Dateipfad, und die Bewertung
stürzte mit `File name too long` ab. Seither zählen `run_code`-Rückgaben nicht als Pfade —
die Dateien des Programms kommen über die ausgepackten inneren Aufrufe.

**Wiederkehrend bei gemma4:** `len(r["features"])` — bei Chester ist `features` eine
Anzahl, keine Liste (Läufe 3 und 5).

## Pilot mit vier Tests (24.10.2026, qwen3.6-coder, Judge nur nach Ergebnis)

| Test | Fall | Architektur | Zeit | Urteil | Aufrufe |
|---|---|---|---|---|---|
| 1 | buffer-schools-500m | chester-code | 250 s (davor 394 s) | PASS | ein Hauptprogramm |
| 2 | buffer-schools-500m | chester-agent | 652 s | PASS | Einzelschritte |
| 3 | mean-elevation-per-district | chester-code | 644 s | PASS | 8 Programme, 23 Werkzeugaufrufe |
| 4 | mean-elevation-per-district | chester-agent | 552 s | PASS | 18 Einzelaufrufe |

Chester Code gewinnt, wo die Kette feststeht (Puffer), und verliert, wo erst gesucht werden
muss (Stadtbezirke: welcher Dienst? welches Höhenmodell?). Test 3 zerfiel in drei
Suchprogramme, die eigentliche Arbeit und drei überflüssige `inspect_map`.

**Wiederholungen von Test 3**, jeweils mit einer Änderung:

| Anlauf | Änderung | Verlauf |
|---|---|---|
| 2 | „erst ein Erkundungsprogramm, dann ein Arbeitsprogramm" | sechs Erkundungsprogramme, kein Arbeitsprogramm — die Regel wurde zur Lizenz zum Herumprobieren; abgebrochen, Regel wieder heraus |
| 3 | `inspect_map` nur einmal; Docstring von `zonal_stats` sagt, dass es die Zonen selbst ins Raster-CRS projiziert (vorher versuchte das Modell `os.system("gdalwarp …")`) | `from tools import *`, ein Tippfehler in der WFS-URL — nach drei gescheiterten Programmen brach CodeMode den Lauf ab (`max_retries=3`) |
| 4 | `run_code` darf 8-mal scheitern; „die Funktionen sind schon definiert" | 18 Programme ohne Ergebnis: wechselnde OSM-Abfragen nach Stadtbezirken, Python-Fehler (`NameError`, `SyntaxError` im f-String), zuletzt Tests, ob `geo_python_run` überhaupt läuft; abgebrochen |

**Befund:** Das Programm ist nicht das Problem, sondern was vor ihm fehlt. Wo Quelle und
Kette klar sind, schreibt das Modell ein gutes Programm in einem Zug. Wo die Anforderung
offenlässt, *woher* die Daten kommen, sucht es im Programm — und verliert dabei das Ziel.
Mit jeder Anweisung, die das Suchen regeln wollte, wurde es nicht besser.

## Verwandte Arbeiten

Zwei Arbeiten lösen genau diese Lücke, beide im Volltext gelesen:

**LLM-Find** (Ning, Li u. a., Penn State, [arXiv 2407.21024](https://arxiv.org/abs/2407.21024)) —
nur Datenbeschaffung, zweistufig:
1. **Quellenwahl** aus einem kurzen *Datenquellen-Index* (je Quelle ein, zwei Sätze); Antwort
   als JSON mit Begründung; keine erfundenen Quellen, sonst „Unknown".
2. **Code** mit dem **Handbuch** der gewählten Quelle im Prompt: Endpunkte, Formate,
   Fallstricke, wann welches Werkzeug — und ein **geprüftes Vorlagenprogramm**. Festes
   Gerüst (`download_data()`), Fehler werden geworfen, Selbstkorrektur bis zu 10-mal.

GPT-4o; in über 70 Anfragen 80–90 % richtige Daten. Die **Vorlagenprogramme heben die
Erfolgsquote deutlich** (OSM-Relationen zu Multipolygonen schaffte das Modell ohne kaum).
Offen bei den Autoren: Quellen *entdecken*, lange Handbücher, Datensätze *bewerten*
(Maßstab, Auflösung).

**GeoColab** (Wu, Gui u. a., Wuhan, [IJDE 2025](https://www.tandfonline.com/doi/full/10.1080/17538947.2025.2569405)) —
Code für ganze Aufgaben, strikt linear über drei Rollen und feste Dokumente:
1. **Produktmanager → Anforderungsdokument**, neun Dimensionen: Plattform, Sprache, Ziel,
   räumliche und zeitliche Ausdehnung, Datenquelle/Format, Methode, Ausgabe,
   **CRS-Transformation**. Erst einordnen (explizit / unvollständig / offen), dann Fehlendes
   ergänzen; eigene Module für die Ausdehnung (Ortsname → amtliche Grenze) und das CRS
   (braucht die Operation Meter? → passende Projektion).
2. **Algorithmus-Ingenieur → Entwurf:** Module mit Eingabe, Ausgabe, Funktionen, Daten,
   APIs; „einfachstes Design", Vorhandenes wiederverwenden.
3. **Programmierer → Code**, mit genau den Funktionssignaturen aus dem Entwurf (RAG).

Auf dem eigenen Benchmark (50 Aufgaben) +7,6 bis +26,1 % bei Ausführbarkeit, Genauigkeit,
Lesbarkeit über sieben Modelle, auch lokal (Qwen2.5 über Ollama). Schon das Verfahren ohne
Wissensbasis bringt +1,8 bis +3,2 %; den größten Anteil hat das **gezielte Nachschlagen der
Funktionssyntax**. GeoColab führt den Code nicht aus — die Verfügbarkeit von Daten prüft
niemand, und daran scheiterte ein eigener Fall.

## Ausbau: Anforderung, Quelle, Programm

Die beiden Arbeiten ergänzen sich, und zusammen beantworten sie, was Chester Code fehlt.
Drei Schritte statt einem, in einem Lauf und mit einem Modell:

**1. Anforderung** (nach GeoColab) — **ohne Werkzeug**, als strukturierte Ausgabe:

| Feld | Beispiel `mean-elevation-per-district` |
|---|---|
| Ziel | mittlere Geländehöhe je Stadtbezirk |
| Gebiet und Grenze | Regensburg, Stadtbezirke (amtlich) |
| Daten je Ebene | Zonen: Stadtbezirke · Raster: Höhenmodell |
| CRS | EPSG:25832 (Meter, Bayern) |
| Methode | Zonalstatistik, Mittelwert, Nodata maskiert |
| Ausgabe | Layer mit Höhe je Bezirk, Karte |

Das zwingt heraus, was der Prompt offenlässt, bevor Code entsteht — und es ist für einen
Menschen und für das Gate lesbar.

**2. Quelle** (nach LLM-Find) — für jede Ebene aus dem Anforderungsdokument eine Quelle
aus einem **kurzen Index** von Chesters Quellen wählen, dann deren **Handbuch** laden:

- Chester hat die Quellen schon (Connectors, `fetch_*`, `geodata_search`); es fehlen die
  Handbücher. Je Quellenfamilie eins: OSM, amtliche Grenzen (BKG/CH/AT), Höhenmodelle
  (DGM1/DGM25/Copernicus), Luftbilder (DOP), städtische WFS, STAC, Statistik, GTFS.
- Ein Handbuch hat: wofür die Quelle taugt und wofür nicht; welche Chester-Funktion sie
  holt, mit den entscheidenden Parametern; die bekannten Fallen (z. B. „DGM1: höchstens
  200 Kacheln — für eine ganze Stadt DGM25"); und **ein geprüftes Vorlagenprogramm**.
- Die Vorlagenprogramme sind Test-Level-1-Tests wert: Jedes muss gegen die echten
  Signaturen stimmen, wie heute das Beispiel in den Anweisungen.

**3. Programm** — wie heute: ein `run_code`-Programm für die ganze Aufgabe, aber mit
Anforderungsdokument und Handbüchern im Kontext, und **nur den Docstrings der Funktionen,
die das Anforderungsdokument und die Handbücher nennen** statt aller 85 (nach GeoColabs
Befund zur Funktionssyntax). Die Prüfungen bleiben: `assert` je Schritt, 8 Korrekturen,
der Guard „ein Programm", das Gate.

**Was dabei nicht kommt:** keine weiteren Agenten (GeoColab trennt Rollen, weil seine Modelle
keinen Kontext teilen; bei uns ist es ein Lauf), keine Wissensdatenbank mit Vektorsuche
(Chesters Werkzeugzahl ist klein genug für eine feste Auswahl), keine Regel „erst erkunden"
(gemessen: lenkt ab).

**Messung:** dieselben vier Tests; Erfolg ist, wenn Test 3 mit **einem** Arbeitsprogramm
nach Anforderung und Quellenwahl auskommt und der Puffer-Fall nicht langsamer wird. Zum
Vergleich ein Lauf mit einem Frontier-Modell, um Modellgrenze und Architektur zu trennen.

## Messung des Ausbaus (24.10.2026, qwen3.6-coder, Judge nur nach Ergebnis, `--fresh`)

Gebaut: `chester/code/planner.py` (Anforderungsschritt), `chester/code/handbooks.py`
(neun Quellen mit Index, Fallen und Vorlagenprogramm), Docstrings in `run_code` nur noch
für die gewählten Quellen plus den Kern. Das Anforderungsdokument steht in den
Instruktionen der Sitzung (sichtbar mit `trace.py`).

| Test | vorher | mit Anforderung und Quelle |
|---|---|---|
| 3 `mean-elevation-per-district` | 644 s, 8 Programme (PASS); zuletzt 18 Programme ohne Ergebnis | **421 s, 4 Programme, PASS** |
| 1 `buffer-schools-500m` | 250 s / 394 s (PASS) | 371 s, dann **210 s mit einem Programm, PASS** |

**Test 3:** Das Anforderungsdokument ordnete die Ebenen richtig zu (Stadtbezirke →
`city_districts`, Höhe → `terrain`). Das erste Programm folgte dem Handbuch:
Katalogsuche, städtischer WFS (`kleingliederung`, 18 amtliche Stadtbezirke),
Copernicus-DEM, Zonalstatistik. Gesucht wurde nicht mehr — die drei weiteren Programme
korrigierten Spaltennamen und Rückgabe.

**Test 1, erster Lauf:** drei Programme kamen sofort als `{}` zurück — qwen schrieb die
Kette in `async def main()` und rief sie nie auf. Seither ein Satz in den Anweisungen
(„Schritte auf oberster Ebene") und ein Guard (`chester/code/guards.py`): Ein Programm,
das nur Funktionen definiert, bekommt einen Hinweis an sein Ergebnis gehängt. Im zweiten
Lauf rief das Modell `main()` auf, ein Programm, fertig.

**Nebenbei gefunden — ein Fehler in `zonal_stats`:** `coverage` teilte die gültigen
Zellen durch das Rechteck um die Zone statt durch die Zellen in ihr. Die 18 Bezirke
meldeten auf einem lückenlosen DEM 0,15–0,71; jetzt 1,0. Ein Test hält das
(`tests/test_zonal_crs.py`).

**Kosten:** der Anforderungsschritt ist ein Modellaufruf mehr; bis zum ersten Programm
vergehen damit rund 3 min statt rund 2 min.

**Test-Level 4** (24.10.2026, `dialog.py --model local/qwen3.6-coder`, abgebrochen):
`map-then-geotiff` riss in Schritt 1 den Deckel (900 s, drei Programme; zwei scheiterten
schon an der Typprüfung der Sandbox, weil der `geo_python_run`-Schnipsel als f-String im
Programm stand), `street-buildings-then-refine` schaffte Schritt 1 in 837 s; dann
abgebrochen. Für die Adressen-Aufgabe gibt es kein Handbuch.

## Offen

1. **Gegenprobe mit einem Frontier-Modell** — trennt Modellgrenze und Architektur; steht
   aus (lokal kein Frontier-Modell konfiguriert).
2. **Folgezüge.** Der Anforderungsschritt sieht nur die neue Nachricht; ein „jetzt in Rot“
   bekommt einen eigenen Plan ohne den vorigen. Ungemessen.
3. ~~**Große Rückgaben.**~~ Gebaut 25.10.2026: `ResultSizeGuardCapability`
   (`chester/code/guards.py`). Ungemessen, weil noch kein echter Lauf zu viel zurückgab.
4. ~~**Wissen aus den Hüllenmodulen.**~~ Gebaut 25.10.2026: Die Fallen stehen in den
   Handbüchern, das Quellenunabhängige in `ANALYSIS_NOTES`. Ungemessen.
5. **Modell.** qwen3.6-coder schreibt die besseren Programme (Puffer: zweimal fehlerfrei im
   ersten Anlauf), gemma4 stolpert öfter über Rückgabeschlüssel. Ein Lauf mit einem
   Frontier-Modell steht aus.
