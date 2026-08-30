# Die vier Test-Level

Chester wird auf vier Ebenen geprüft. Sie unterscheiden sich **nicht durch die Größe
der Aufgabe, sondern dadurch, wer geprüft wird** — sonst wären es nur vier Größen
desselben Tests, und man wüsste bei einem roten Ergebnis wieder nicht, wo es klemmt.

> **Warum „Test-Level" und nicht „Level":** In diesem Repository laufen bereits zwei
> andere Leitern mit denselben Zahlen — die **Autonomiestufen 1–5** aus
> [`einordnung.md`](./einordnung.md) und die **Validierungsstufen 0–3** von
> `/valid_level` ([`validation-concept.md`](./validation-concept.md)). Das Präfix ist
> nicht Umständlichkeit, sondern spart jede Rückfrage.

| | Frage | Unter Test | Daten | Urteil | Kosten | Wann |
|---|---|---|---|---|---|---|
| **Test-Level 1** — Unit | Ist der Code richtig? | Chesters Code, **kein Modell** | Fixtures | `assert` | Sekunden | jeder Commit (`./check.sh`) |
| **Test-Level 2** — Mikro-Geo | Beherrscht der Entscheidungskern die Operation? | **Modell + ein Werkzeug** | Fixtures, **kein Netz** | exakter Vergleich am **Artefakt**, kein Judge | ~22 min für zehn Proben (gemessen) | bei jedem Modellwechsel, vor jedem Freeze |
| **Test-Level 3** — Prompts | Löst er die Aufgabe? | Modell + voller Werkzeugkasten | live | LLM-Judge + Tool-Coverage | 6–20 min je Fall | Messkampagne |
| **Test-Level 4** — Dialoge | Trägt es über mehrere Züge? | Modell + Gedächtnis + Aufräumen | live | Turn- und Verlaufskriterien | teuer | selten, gezielt |

Nach unten wächst die Realitätsnähe, nach oben die **diagnostische Schärfe**: Level 1
sagt, welche Zeile falsch ist; Level 4 sagt, dass ein Gespräch schiefging, und man
sucht danach selbst.

## Test-Level 1 — Unit  ·  *vorhanden*

`tests/`, ausgeführt von `./check.sh`. Reine Funktionen, Werkzeugverträge, Struktur-
und Baseline-Prüfungen; QGIS-abhängige Fälle überspringen, wenn kein QGIS da ist.
Kein Sprachmodell im Spiel.

Grenzfall zur nächsten Stufe: `tests/test_workflow_building_heights.py` fährt die
Kette DSM−DTM → Zonenstatistik → Filter **deterministisch ohne Modell** durch. Das ist
Level 1 — es beweist, dass die Kette trägt, nicht dass jemand sie findet. Genau das
prüft Level 2.

## Test-Level 2 — Mikro-Geo-Tasks  ·  *gebaut, zehn Proben*

Eine Aufgabe, ein Werkzeug, ein exakter Sollwert. Der Zweck ist ein **Vorfilter**: Ob
ein anderes Modell überhaupt in Frage kommt, muss man in Minuten beantworten können
und nicht mit einem 15-Minuten-Livelauf, der zusätzlich vom Netz und von einem
Sehmodell abhängt.

**Drei Regeln, die die Stufe scharf halten:**

1. **Gemessen wird am erzeugten Artefakt**, nie am Antworttext. Die Fläche der
   geschriebenen Ebene, die Objektzahl, das CRS — Werte, die ein `assert` prüft.
2. **Kein Judge, kein Netz.** Braucht ein Fall Urteilsvermögen oder eine Live-Quelle,
   ist er Test-Level 3. Ohne diese Härte wird die Stufe langsam und unzuverlässig,
   also unbenutzt.
3. **Jeder Fall trägt eine Falle.** Kein Happy Path.

### Warum die Fälle nicht aus dem Funktionsverzeichnis kommen

Die [PostGIS-Referenz](https://postgis.net/docs/reference.html) und
[OGC Simple Features (06-103r4)](https://docs.ogc.org/is/06-103r4/06-103r4.pdf) sind
die richtige **Skelettliste**: Sie sagen, welche Operationen es überhaupt gibt, und
verhindern blinde Flecken — Prädikate/DE-9IM, `ST_Union`, `ST_Transform`,
`ST_Distance`, `ST_Centroid`. Aber sie definieren **Geometrie-Semantik, nicht
Analyse-Semantik**, und dort liegt Chesters Fehlerbild nicht.

Kein einziger belegter Fehlschlag dieses Projekts war ein Geometriefehler — GEOS und
QGIS rechnen `ST_Buffer` korrekt. Es waren: Grad statt Meter, **Berührung statt
Schnitt**, der falsche Nenner, OR- statt AND-Semantik bei Tags, unbemerkt fehlende
Abdeckung. Ein Probe-Satz, der die Funktionsliste abarbeitet, prüft, was ohnehin
funktioniert.

Deshalb: **Skelett aus der Norm, Inhalt aus den Fallen.**

| SFA / PostGIS | Die Falle | Bestanden heißt |
|---|---|---|
| `ST_Buffer` | Ebene in EPSG:4326, „100 m" gefordert | erst umprojizieren; Pufferfläche im Sollbereich |
| `ST_Area` / `ST_Length` | Fläche in einem geographischen CRS messen | Grad-Quadrate erkennen und verweigern statt zu liefern |
| `ST_Intersection` vs. Prädikat | „welche Wälder liegen im 100-m-Streifen" | Schnittfläche, nicht ganze Polygone (Faktor-Fehler, belegt 2026-08-26) |
| `ST_Within` / `ST_Contains` | Objekt genau auf der Grenze | Randfall benannt, nicht stillschweigend entschieden |
| `ST_Union` | überlappende Puffer summieren | Vereinigung statt Flächenaddition |
| `ST_Transform` | Zielsystem als Zahl, Name gefragt | richtiger CRS-Name (nicht „Gauß-Krüger" für 25832) |
| Attribut-Join | `null`, float und string gemischt (§5.1.3 des Autonomous-GIS-Papiers, siehe [`einordnung.md`](./einordnung.md)) | kein stiller Zeilenverlust |
| Zonenstatistik | Raster deckt eine Zone nur teilweise | Teilabdeckung gemeldet, nicht als voller Mittelwert ausgegeben |

### Wie es im Repo aussieht

| | |
|---|---|
| Aufgaben | `agent-probe-tasks.jsonl` — je Zeile eine Probe: Operation, Falle, Prompt, Fixtures, Prüfungen |
| Fixtures | `samples/probe/`, erzeugt von `samples/make_probe_fixtures.py`; **jeder Sollwert wird dort gerechnet und ausgegeben**, statt zugesichert zu sein (`samples/probe/expected.json`). Die Dateien liegen eingecheckt bei (1,1 MB), das Skript erzeugt sie neu, wenn sich eine Aufgabe ändert |
| Auswertung | `chester/probes.py` — acht Prüfarten (`output_exists`, `no_output`, `crs_metric`, `crs_epsg`, `features`, `area_m2`, `no_nulls`, `value_seen`), rein und ohne Modell testbar (`tests/test_probes.py`) |
| Runner | `probe.py` — `uv run probe.py`, `uv run probe.py <id>`, `--list`, `--verbose` |

**Alle Proben laufen in *einem* Prozess mit *einem* Agenten**, nur die Sitzung wird je
Aufgabe geleert. Der System-Prompt bleibt damit gleich, die kalte Prefill wird genau
einmal bezahlt (gemessen 78,6 s kalt gegen 0,1 s im Cache). Ein Runner, der je Aufgabe
einen Prozess startet, macht den Vorfilter kaputt.

Zwei Nebenbedingungen, die aus der Praxis kommen: Vor jeder Probe werden ihre Fixtures
frisch kopiert **und ihre erwarteten Ausgaben gelöscht** — sonst besteht ein Lauf auf
der Datei des vorigen (der Stale-State-Fall aus den Dialogtests). Und der Runner hängt
dieselbe Validierungs-Verdrahtung an wie das Produkt, sonst prüft er einen Agenten, den
es so nicht gibt.

### Der Zeitdeckel — und warum es ihn gibt

Jede Probe hat einen Deckel (`--timeout`, Vorgabe **180 s**). Wer ihn reißt, ist
durchgefallen, unabhängig davon, was der Lauf danach noch versucht; die Prüfungen
laufen trotzdem, weil das bis dahin Geschriebene mehr sagt als ein blankes
„abgebrochen".

Der Anlass ist gemessen: Im ersten Durchlauf ohne Deckel (2026-08-29) kreiste
`join-leading-zero-ags` **elf Stunden** über 82 Werkzeugaufrufe — 56 davon
`qgis_python`, immer wieder am selben Importfehler — und lieferte am Ende eine
**leere** Ebene. Ohne Deckel bestimmt der schlechteste Fall die Laufzeit des ganzen
Vorfilters, und ein Vorfilter, der über Nacht läuft, ist keiner.

Eine Probe darf einen **eigenen** Deckel mitbringen (`timeout_s` in der Aufgabe); er
gewinnt gegen die Vorgabe. Gedacht ist er für Absage-Fälle, die erst prüfen müssen,
bevor sie „nein" sagen können — `ndvi-without-nir` hat 420 s. Der Wert steht in der
Aufgabe und nicht im Runner, damit er neben der Falle steht und begründet werden kann.

Vor der ersten Probe läuft der Agent **einmal warm** (ein trivialer Zug außerhalb der
Zeitnahme). Sonst zahlte die erste Probe die kalte Prefill (~160 s auf der
Entwicklungsmaschine) und risse den Deckel — gemessen würde der Cache, nicht das
Modell.

### Erster Messstand — `gemma4:26b-mlx`, 2026-08-30

**6/10 in 22 Minuten.** Die Zahl ist kein Qualitätsurteil über das Modell, sondern der
Beleg, dass die Stufe als Vorfilter taugt: Dieselben zehn Fragen über Test-Level 3 zu
stellen hätte zwei Stunden gedauert und hätte drei der vier Fehler nicht gezeigt.

| Probe | | Zeit | Befund |
|---|---|---|---|
| `buffer-in-degrees` | PASS | 52 s | metrisch umprojiziert |
| `area-in-degrees` | PASS | 107 s | keine Grad-Quadrate |
| `intersection-not-selection` | PASS | 142 s | 45.000 m² statt 135.000 |
| `union-not-sum` | PASS | 165 s | 100.000 m² statt 120.000 |
| `within-on-the-boundary` | PASS | 86 s | 3 statt 4 — Kantenpunkt draußen |
| `utm-choice-germany` | **FAIL** | 180 s | **EPSG:25833 statt 25832** — falsche UTM-Zone |
| `join-leading-zero-ags` | **FAIL** | 180 s | keine Ausgabe (ohne Deckel: leere Ebene nach 11 h) |
| `ndvi-without-nir` | **FAIL** | 180 s | schreibt korrekt **nichts**, sagt es aber nicht in der Zeit (auch nicht in 420 s, nachgemessen) |
| `footprint-area-sum` | PASS | 70 s | 1.576 m² exakt |
| `height-gini` | **FAIL** | 180 s | kein Wert in einer Werkzeug-Rückgabe |

Zwei Einordnungen dazu:

- **Die falsche UTM-Zone ist der Fund, für den die Stufe gebaut wurde.** In einer
  Bank-Aufgabe wäre sie unsichtbar geblieben: Die Fläche stimmt näherungsweise, die
  Karte sieht richtig aus, der Judge hätte nichts zu beanstanden gehabt.
- **`ndvi-without-nir` fällt durch, obwohl es sachlich richtig handelt** — es entsteht
  korrekt keine Datei, die Absage wird nur nicht ausgesprochen. Der Fall hat deshalb
  einen **eigenen Deckel von 420 s** bekommen (`timeout_s` in der Aufgabe, neben der
  Falle, wo er begründet werden kann): Durchfallen soll das Erfinden, nicht die
  Sorgfalt. **Das Ergebnis blieb FAIL** — auch in sieben Minuten sagt das Modell nicht,
  dass drei Banden kein NDVI ergeben. Damit ist die Frage entschieden, und zwar gegen
  die naheliegende Vermutung: Es lag nicht am knappen Deckel.
- **`height-gini` wurde nach dem Lauf nachgeschärft** (der Prompt nennt jetzt
  ausdrücklich `qgis_python` und den Rückgabekanal) und fällt weiterhin durch. Damit
  ist es kein Zuschnittfehler der Probe, sondern eine Modellgrenze.

Ein technischer Nebenbefund: **Eine abgebrochene Probe hinterlässt keine Sitzung** —
SelmaKit schreibt den Trace nur bei vollständigem Lauf. Für die Diagnose eines
Timeouts bleibt nur das Live-Protokoll.

### Die drei Fälle aus der Bank  ·  *umgezogen am 2026-08-30*

Drei Fälle waren in der Bank falsch einsortiert und stehen seither hier:

- **`dop-ndvi-no-nir-bayern`** → `ndvi-without-nir`: prüft keine GIS-Aufgabe, sondern
  **Selbstkenntnis** — eine Absage („dieses Luftbild hat kein Infrarotband") ist die
  bestandene Antwort. In einem Aufgaben-Benchmark ist das ein Fremdkörper.
- **`total-building-footprint-area`** → `footprint-area-sum` und
  **`building-height-gini`** → `height-gini`: fixture-basiert, während die Bank live
  läuft. Als Level-2-Fälle mit exaktem Sollwert sind sie richtig; in Level 3 waren sie
  kaputt — der Fixture/Live-Mismatch, der lange als Schuldposten geführt wurde.

Die Bank steht seither bei **33 Live-Aufgaben**, und Level 2 startet nicht bei null.
Die archivierten Urteile der drei bleiben in `.chester/evals/history.jsonl` stehen; ein
Report zeigt sie weiter, fahren lässt sich dort keiner mehr.

## Test-Level 3 — Prompts  ·  *vorhanden*

Die Benchmark-Bank: `agent-test-prompts.jsonl`, gefahren mit `testprompt.py` (ein Fall
im Detail) oder `evals.py` (die ganze Bank als Tabelle), benotet von einem
unabhängigen LLM-Judge plus deterministischer Tool-Coverage. Kategorien, Attribute,
Judge, Coverage und Laufprotokolle stehen in
[`agent-test-prompts.md`](./agent-test-prompts.md); die Messfrage dahinter in
[`tool-compensation.md`](./tool-compensation.md).

## Test-Level 4 — Dialoge  ·  *entworfen, zu erstellen*

Was ein Einzelprompt prinzipiell nicht erreicht: Rückfrage-erst-Wege, Korrektur und
Rücknahme, Verfeinerung auf dem vorhandenen Layer, veralteter Zustand aus einem
früheren Turn, Standhalten unter Nachdruck, Herkunft einer Zahl auf Nachfrage. Sieben
Kategorien mit `D`-Präfix, Entwurf samt Aufbauregeln in
[`agent-test-dialogs.md`](./agent-test-dialogs.md).

## Was die Leiter *nicht* umfasst

Die Prüfung des **Harnischs** — Kontextkosten, Regeldurchsetzung, Sensorstände — ist
kein Test-Level, sondern ein eigenes Werkzeug. Die vier Stufen prüfen Chester; jenes
prüft, wie Chester gebaut wird.
