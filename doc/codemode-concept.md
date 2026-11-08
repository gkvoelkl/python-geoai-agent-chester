# CodeMode — ein Konzept

> Soll Chester Werkzeugketten als kurzes Programm schreiben statt als Folge einzelner
> Werkzeugaufrufe? Dieses Dokument sammelt, was CodeMode ist, was Chester davon schon
> gelernt hat, wo es in den Bau eingreift und wie ein Versuch gemessen wird.
>
> **Status (2026-10-24): abgelöst.** CodeMode als zuschaltbarer Modus in chester-agent
> wurde gebaut, in Pilotläufen erprobt und wieder herausgenommen. Die Modelle schrieben
> pro `run_code` nur einen Werkzeugaufruf (§8). Die Idee lebt als eigene Architektur
> weiter: [`chester-code.md`](./chester-code.md). Dieses Dokument bleibt als
> Vorgeschichte; die Befunde in §5.1 und §8 gelten dort weiter.

## 1. Was CodeMode ist

Normalerweise ruft das Modell pro Zug ein Werkzeug auf, liest das Ergebnis und
entscheidet den nächsten Schritt. Bei CodeMode bekommt es stattdessen **ein**
Werkzeug, `run_code`. Darin schreibt es Python, das die Werkzeuge als Funktionen
aufruft — mit Variablen, Schleifen, Bedingungen und `asyncio.gather`. Das Programm
läuft in einer abgeschotteten Umgebung; Zwischenergebnisse bleiben dort und gehen
nicht durch den Gesprächsverlauf.

Die Idee stammt von Cloudflare. Für Chester zählt die Umsetzung in
[pydantic-ai-harness](https://pydantic.dev/docs/ai/harness/code-mode/), der
Capability-Bibliothek, auf der SelmaKit aufbaut:

| | |
|---|---|
| Einschalten | `CodeMode()` als Capability am Agenten, Paket-Extra `pydantic-ai-harness[codemode]` |
| Auswahl | `tools=`: `'all'`, eine Namensliste, ein Prädikat oder ein Metadaten-Filter; nicht gewählte Werkzeuge bleiben normale Aufrufe |
| Sandbox | Monty, eine Python-Teilmenge: nur Standardbibliothek (u. a. `json`, `math`, `re`, `asyncio`), **keine Fremdimporte**, kein Dateisystem, keine Uhr ohne Freigabe |
| Grenzen | `resource_limits` (Vorgabe 30 s und 256 MiB je Codestück), `max_tool_calls` (100 je `run_code`), `max_retries` (3, auch bei Syntaxfehlern) |
| Signaturen | aus dem JSON-Schema jedes Werkzeugs; ohne Rückgabeschema steht `-> Any` da (`CodeModeReturnSchemaWarning`) |
| Protokoll | die inneren Aufrufe stehen in den **Metadaten** der `run_code`-Rückgabe (`tool_calls`, `tool_returns`) |

Die API ist ausdrücklich nicht stabil (0.x).

## 2. Warum es zu Chester passen könnte

**Chesters Aufgaben sind Ketten.** Das Straßennetz-Beispiel ist typisch:
`geocode` → `osm_features` → `vector_split_by_geometry` → `vector_merge` →
`vector_filter` → `render_map`. Jeder Schritt hängt am vorigen, aber zwischen den
Schritten ist selten ein Urteil nötig — die Werkzeuge melden selbst, was schiefging.

**Modellzüge sind die teure Einheit.** Gemessen am 2026-10-19 (gemma4 auf llama.cpp,
MacBook Air M4): Ein Lauf braucht 12–35 Modellzüge. Ein Folgezug kostet nur wenige
Sekunden Einlesen (der Prompt-Cache trägt), aber 30–45 s Ausgabe bei ~18–24 Token/s.
Fasst das Modell eine Kette in zwei, drei `run_code`-Aufrufe, entfallen die meisten
dieser Züge. Das ist ein Argument, keine Messung.

**Große Rückgaben bleiben draußen.** `vector_info` lieferte 11.586 Zeichen, die als
Überlauf ausgelagert wurden. In CodeMode liest das Programm daraus nur, was es braucht.

## 3. Was Chester schon gelernt hat

**Das Gate war schon einmal blind.** Im September 2026 (damals im Rahmen der
gestrichenen Ablationsphase) fiel auf: CodeMode legt die inneren Aufrufe in Metadaten
ab, SelmaKits `tool_returns` las nur den Inhalt — das Validierungs-Gate hätte nach dem
Einschalten nichts mehr gesehen und sich nicht gemeldet. **Seit SelmaKit 0.1.36
behoben:** `tool_returns()` packt die inneren Ergebnisse standardmäßig aus
([SelmaKit #1](https://github.com/gkvoelkl/python-selmakit/issues/1)). Was bleibt:
Wer die Nachrichten selbst durchläuft (`run_messages()`), sieht weiterhin nur
`run_code`.

**Was im Katalog steht, wird benutzt.** Mit `geo_python_run` hat Chester schon eine
eigene Code-Sandbox (Subprozess, echter geopandas-Namensraum). Am 2026-09-07 nutzte
gemma4 die dort bereitgestellten geprüften Funktionen kein einziges Mal — sie standen
nur in der Beschreibung, nicht im Werkzeugkatalog („Since I can't call 'reproject'
inside here"). Daraus wurden die zwanzig geprüften Werkzeuge. CodeMode verhält sich
hier anders: Die Werkzeuge erscheinen im Code **als Funktionen mit Signatur**. Ob das
Modell sie so annimmt, ist die erste offene Frage.

**Die beiden Sandboxen sind verschieden und ergänzen sich.**

| | `geo_python_run` | CodeMode `run_code` |
|---|---|---|
| Zweck | Rechnen, wofür es kein Werkzeug gibt (z. B. Gini) | Werkzeuge verketten |
| Umgebung | Subprozess, geopandas/shapely/rasterio | Monty, nur Standardbibliothek |
| Daten | liest und schreibt Dateien | sieht nur Werkzeugrückgaben |
| Gate-Sicht | `calls` im Inhalt des Verdikts | innere Aufrufe in Metadaten, von `tool_returns` ausgepackt |

## 4. Wo CodeMode in den Bau eingreift

**Verdrahtung.** CodeMode ist eine pydantic-ai-Capability. Sie kommt über
`geo_capabilities()` in den Agenten — kein eigener Agentenbau, kein Fork von SelmaKit.
Ein Schalter in der Konfiguration (`agent.code_mode`, Vorgabe aus) und eine
Umgebungsvariable für einzelne Läufe (`CHESTER_CODE_MODE=1/0`, nach dem Vorbild von
`CHESTER_NO_QGIS`) erlauben den Vergleich beider Zweige auf derselben Maschine.

**Welche Werkzeuge in den Code dürfen.** Nur deterministische Werkzeuge mit Pfad rein,
Pfad raus:

- **ja:** `geocode`, `osm_features`, `fetch_boundaries`, die `vector_*`-Operationen,
  `check_crs`, `rasterize`, `zonal_stats`, `slope`, `service_area`, die
  Datenabrufe (`fetch_dem`, `fetch_dop`, …)
- **nein, bleiben normale Aufrufe:**
  - `write_plan` — der PlanGuard reagiert auf jeden einzelnen Aufruf
  - `render_map`, `inspect_map` — das Ergebnis muss angesehen und verlinkt werden
  - `geo_python_run` — keine Sandbox in der Sandbox
  - `qgis_*` und die Desktop-Brücke
  - Gedächtnis, Skills (`load_capability`) und alles, was eine Freigabe braucht

**Rückgabeschemata.** Chesters Werkzeuge geben Dicts zurück. Befürchtet war `-> Any`
ohne Schlüssel. Nachgesehen am 2026-10-24: Die Stubs zeigen `-> dict[str, Any]` **mit
dem vollständigen Docstring** jedes Werkzeugs, und dort stehen die Rückgabeschlüssel;
eine `CodeModeReturnSchemaWarning` kommt nicht. Typisierte Rückgaben (`TypedDict`)
werden deshalb erst gebaut, wenn die Messung zeigt, dass das Modell Schlüssel rät.

**Prompt-Größe.** Gezählt am 2026-10-24: ohne CodeMode 85 Werkzeuge mit rund 16.700
Token Werkzeugbeschreibung, mit CodeMode 28 Werkzeuge plus `run_code` mit rund 15.700.
Die Beschreibung von `run_code` allein ist rund 38.000 Zeichen lang — sie trägt die
57 Werkzeuge, die vorher einzeln im Katalog standen.

**Der Werkzeugvertrag bleibt.** `resolve_path`, der zurückgegebene Ausgabepfad und die
Provenienz gelten unverändert, weil die Werkzeuge dieselben bleiben — nur der Aufrufer
ändert sich.

**Anweisungen.** `get_instructions()` muss sagen: keine Fremdimporte, keine
Dateizugriffe im Code, Ergebnis der letzten Zeile ist die Rückgabe, bei `ok: false`
abbrechen und melden statt weiterrechnen.

## 5. Offene Fragen

1. **Schreibt gemma4 brauchbares Monty-Python?** Schon bei JSON-Argumenten macht es
   Fehler (`"true"` statt `true`, `description` statt `content`). Im Code werden daraus
   Wiederholungen von `run_code`. Die Doku der Harness sagt zu kleinen lokalen Modellen
   nichts.
2. ~~Laufen die Hooks der Guards für innere Aufrufe?~~ **Ja** (geprüft 2026-10-24,
   siehe §5.1).
3. ~~Zählt die Laufzeit der Werkzeuge gegen die 30 s?~~ **Nein** (geprüft 2026-10-24,
   siehe §5.1).
4. **Was passiert bei einem Gate-Retry?** Das Gate prüft die fertige Antwort. Fordert
   es eine Korrektur (z. B. Zuschnitt auf die Gemeindegrenze statt Bounding Box), muss
   das Modell ein neues Programm schreiben statt einen Schritt zu wiederholen.
5. **Die eigene Auswertung.** `testprompt.py`, `trace.py` und die Ressorts von
   chester-team gehen die Nachrichten selbst durch und sähen nur `run_code` — die
   Werkzeug-Abdeckung fiele auf null. Sie brauchen dieselbe Auspackung wie
   `tool_returns`.

### 5.1 Geprüft am 2026-10-24 (ohne echtes Modell)

Ein Spielagent mit pydantic-ais `FunctionModel` (die „Modellantworten" sind fest
vorgegebene `run_code`-Programme), CodeMode aus pydantic-ai-harness 0.57.0, Monty 1.3.0,
SelmaKit 0.1.40, pydantic-ai 2.57.0:

| Frage | Ergebnis |
|---|---|
| `after_tool_execute` für innere Aufrufe? | **Ja.** Der Hook lief für beide inneren Werkzeuge, dann für `run_code` selbst, dann für ein normales Werkzeug. PlanGuard und Werkzeugprotokoll sehen also jeden Schritt. |
| Ändert ein Hook das Ergebnis, sieht das Programm die Änderung? | **Ja.** Ein Hook, der das Ergebnis ersetzte, kam im Programm so an, wie ein Guard es braucht. |
| Zählt Werkzeugzeit gegen `max_duration_secs`? | **Nein.** Ein Werkzeug schlief 4 s bei einer Grenze von 2 s, der Aufruf lief durch. Der Quellcode sagt es ausdrücklich: „time awaiting tools does not count". |
| Was liefert `tool_returns()` im Output-Validator? | Die inneren Ergebnisse **einzeln und vor** dem `run_code`-Eintrag, mit Werkzeugnamen. Das Gate sieht also dieselben Pfade und Warnungen wie ohne CodeMode. |
| Ein inneres Werkzeug wirft `ModelRetry` | Im Programm wird daraus eine Ausnahme. Fängt das Programm sie nicht, bekommt das Modell einen Retry für **ganz `run_code`**, mit Traceback und der Nachricht des Werkzeugs. Das kostet einen der drei `max_retries`, und das Programm läuft neu. |

Damit sind die technischen Hindernisse aus dem Weg. Offen bleiben die Fragen 1, 4 und 5
— sie hängen am Modell und an Chesters eigener Auswertung, nicht an der Harness.

## 6. Messplan

**Arme:** derselbe Rechner, dasselbe Modell, `CHESTER_CODE_MODE=0` gegen `=1`, alle
Läufe `--fresh`.

**Fälle:** Test-Level 3 mit Ketten unterschiedlicher Länge — `buffer-schools-500m`
(kurz), `mean-elevation-per-district` (Raster plus Zonen), `road-impact-greenspace-100m`
(mehrere Ebenen); je Arm drei Läufe.

**Kennzahlen:**

| | Quelle |
|---|---|
| Modellzüge und Laufzeit | Server-Log, `duration_s` im Laufprotokoll |
| Werkzeugaufrufe gesamt, davon in `run_code` | Trace nach Schritt 5 |
| Wiederholungen von `run_code` (Syntax, Grenzen) | Trace |
| Urteil und Kriterien | Judge-Panel |
| Gate-Eingriffe | `[gate]`-Zeilen im Protokoll |

**Entscheidung:**

- **übernehmen**, wenn die Laufzeit deutlich sinkt (Richtwert: um mindestens ein
  Drittel) und kein Fall schlechter beurteilt wird;
- **verwerfen**, wenn das Modell mehr als jeden dritten `run_code`-Aufruf wiederholen
  muss oder ein Urteil kippt, das ohne CodeMode hielt.

## 7. Abgrenzung

- Kein Ersatz für `geo_python_run`: Rechnen ohne passendes Werkzeug bleibt dort.
- Kein Umbau der Werkzeuge: Sie bekommen Rückgabeschemata, sonst nichts.
- Keine Anpassung der Bank auf CodeMode hin: Prompts und Kriterien bleiben, wie sie sind.

## 8. Was die Pilotläufe gezeigt haben (2026-10-24)

Mit `buffer-schools-500m`, `--fresh`, CodeMode eingeschaltet:

| Lauf | Modell | was geschah |
|---|---|---|
| 1 | gemma4 | schrieb ein **ganzes Programm** (`geocode` → `osm_features`, mit `ok`-Prüfungen) — und schickte es an `geo_python_run`, wo keine Werkzeuge sind; danach 40 Websuchen, bis der Kontext (64k) voll war |
| 2 | gemma4 | nach der Umleitung in `geo_python_run`: `run_code`, aber **ein Werkzeug pro Programm** |
| 3–5 | qwen3.6-coder | einmal `run_code` mit je einem Werkzeug, zweimal gar nicht — stattdessen Suche nach Werkzeugen über die verbliebenen (`geodatasets_list`, `memory_search`, `load_capability`) |

Ohne CodeMode löste qwen3.6-coder denselben Fall (PASS, 728 s).

**Warum CodeMode in chester-agent nicht trug:** Fünf Stellen ziehen gegen „ein Programm".
Die Beschreibung von `run_code` sagt „REPL-style"; die Planungsanweisung treibt
Schritt für Schritt („execute them", zwischen zwei Plänen echte Arbeit); Chesters
Instruktionen sagen „one call per step" und nennen 85-mal Werkzeuge, als wären sie
direkt aufrufbar; im Katalog bleiben ausgerechnet die ablenkenden Werkzeuge stehen;
und Schritte, deren Ergebnis den nächsten bestimmt (gemischte Geometrie), zeigt kein
Beispiel. Daraus folgte keine weitere Stellschraube, sondern eine eigene Architektur
ohne diese Gegenkräfte: [`chester-code.md`](./chester-code.md).
