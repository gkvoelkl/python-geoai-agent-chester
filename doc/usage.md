# Bedienung & Betrieb

> Wie man Chester betreibt und steuert — Slash-Befehle, Daten-Cache, Tracing, Tests,
> Benchmarks — sowie der Repo-Aufbau und ein Absatz zur Funktionsweise. Die Installation
> steht im [README](../README.md), was Chester an Geodaten *kann*, in
> [`features.md`](./features.md).

## Sub-Agents, Verbose & Slash-Befehle

Im Dashboard gibt es weitere Slash-Befehle (laufen vor dem LLM): `/qgis` (letzte Karte
in QGIS zeigen), `/testprompt <id>` (einen Benchmark-Prompt in-chat ausführen), `/eval`
(Eval-Historie), `/verbose on` (Tool-Aufrufe/Timing/Reasoning ins Dashboard streamen)
sowie SelmaKits `/model`, `/think` u. a. Sind **Sub-Agents** in der Config aktiviert
(`subagents`-Block), delegiert der Hauptagent in sich abgeschlossene Web-Recherche über
ein `delegate_task`-Tool an isolierte Worker (mitgeliefert: `data-scout` = offene
Geodatenquellen finden, `researcher` = allgemeine Recherche) — das hält den
Hauptkontext schlank. (Sub-Agents bekommen nur Web-/Dateisystem-Werkzeuge, nicht die
Geo-Tools.)

## Den Daten-Cache inspizieren

Chester katalogisiert jeden Datensatz, den er herunterlädt oder erzeugt, in einem
selbst-ablaufenden **GeoCache**. Vom Terminal inspizieren:

```bash
uv run data.py                 # das Inventar (Name, Art, CRS, Ausdehnung, Alter)
uv run data.py --filter ahr    # nach Name/CRS/Art einschränken
uv run data.py --prune         # abgelaufene Datensätze löschen und berichten
```

Im Dashboard ist dasselbe als **Slash-Befehle** verfügbar (laufen vor dem LLM, kein
Agent-Turn):

- `/geocache [filter]` — das Inventar; `/geocache prune [--dry-run]`;
  `/geocache rm <dataset>` oder `/geocache rm all [--dry-run]`, um den ganzen Cache
  zu leeren (beide verweigern deine eigenen `source: user`-Daten).
- `/geoconnector` — konfigurierte Connectoren; `/geodataset <connector>` — die
  Ebenen/Tabellen, die ein GeoPackage-/SpatiaLite-/PostGIS-Container bereitstellt.

### Wie lange Daten liegen bleiben

Jeder Datensatz altert ab seiner **letzten Nutzung** aus (nicht ab dem Download), und
`sync` löscht Abgelaufenes. Steuerbar im `geodata`-Block:

```jsonc
"geodata": {
  "ttl_days": 30,                       // Standard-Lebensdauer
  "ttl_by_source": {                    // Ausnahmen je Herkunft
    "connector/*":   7,                 //   alle Downloads: kurz
    "connector/osm": 2,                 //   OSM noch kürzer (exakt schlägt Familie)
    "chester":      90                  //   selbst erzeugte Ergebnisse: lang
  },
  "sync_interval_hours": 6              // 0 = aus
}
```

Die Reihenfolge ist: ein **gepinnter** Wert (`geocache_note` mit `ttl_days`, im Inventar
als `7*` markiert) → der Herkunfts-Sidecar → `ttl_by_source` → `ttl_days`. Nur der Pin
wird gemerkt; alles darunter wird bei jedem Sync neu aus der Konfiguration abgeleitet,
eine Änderung wirkt also sofort auch auf bereits katalogisierte Datensätze. Deine
eigenen Daten unter `geodata.roots` (`source: user`) laufen nie ab.

`sync_interval_hours` betrifft nur den **Gateway**: der Sync läuft ohnehin bei jedem
Start und vor jedem `geocache_list`, was für CLI-Nutzung genügt. Bleibt der Gateway
aber tagelang offen, löst sonst nichts mehr das Ablaufen aus — dann hält ein Intervall
den Cache in Grenzen. Läuft auf einem Daemon-Thread, ohne LLM.

Richte Chester über den `geodata`-Block in `.chester/chester.json` auf deine eigenen
Daten (`geodata.roots` für nur-lesbare, an Ort und Stelle katalogisierte Ordner,
`geodata.postgis` für eine Datenbank) — siehe den `connect-data`-Skill.

## Einen Lauf nachverfolgen (kein OpenTelemetry nötig)

OpenTelemetry ist standardmäßig aus: seit SelmaKit 0.1.26 schaltet es erst ein
`tracing`-Block in `.chester/chester.json` ein (`enabled`, `endpoint`, `project_name`,
`capture_http`), gegen einen beliebigen OTLP/HTTP-Collector. Das übliche Tracing ist
deshalb einfach der Pro-Session-Datensatz, den SelmaKit unter
`.chester/sessions/<key>.json` persistiert. `trace.py` zeigt genau, was der Agent
getan hat — den Prompt, sein Reasoning, jeden Tool-Aufruf mit Argumenten, jedes
Tool-Ergebnis und die finale Antwort:

```bash
uv run trace.py                 # Sessions auflisten (neueste zuerst)
uv run trace.py cli             # vollständiger Trace der Session "cli"
uv run trace.py last            # vollständiger Trace der jüngsten Session
uv run trace.py cli --full      # Tool-I/O nicht kürzen
```

(Das Gateway loggt während des Laufs auch nach stdout; leite es um, wenn du eine
Datei willst.)

## Tests

Chester wird auf vier **Test-Leveln** geprüft — die Systematik steht in
[`test-levels.md`](./test-levels.md). Hier die Bedienung der beiden unteren:

```bash
uv run pytest                  # Test-Level 1: Unit + QGIS-Integration + lokale Geo-Tests
uv run pytest --run-network    # zusätzlich osmnx-/STAC-Netzwerk-Tests
uv run pytest --run-llm        # zusätzlich Ollama-Agenten-Tests (braucht ein laufendes Modell)

uv run probe.py                # Test-Level 2: alle Mikro-Geo-Proben, am Ende k/n
uv run probe.py <id>           # eine Probe, mit Werkzeug-Protokoll
uv run probe.py --list         # Proben mit Operation und Falle auflisten
```

Test-Level 2 braucht **kein Netz und keinen Judge**: Jede Probe stellt eine Operation
gegen einen exakten Sollwert und prüft am erzeugten Artefakt. Das ist der Vorfilter vor
einem Modellwechsel — Minuten statt eines Bank-Laufs. Die Fixtures liegen in
`samples/probe/` (eingecheckt); `samples/make_probe_fixtures.py` erzeugt sie neu und
rechnet dabei jeden Sollwert vor, statt ihn zuzusichern.

QGIS-Tests werden automatisch übersprungen, wenn `qgis_process` nicht gefunden wird.
Sie prüfen geometrische Korrektheit (Pufferfläche ≈ πr², Slope = 45° auf einer
Einheits-Steigung, NDWI-Wasserfläche, die building-heights-Kette), nicht nur, dass
Code läuft. Netzwerk- und LLM-Tests sind bewusst opt-in, damit ein normaler Lauf
ohne Internet und ohne Sprachmodell durchläuft.

## Benchmarks: Wie gut löst ein Modell echte Geo-Aufgaben?

Pytest prüft den Code. Ob ein *Sprachmodell* eine Aufgabe tatsächlich löst, prüft eine
zweite Ebene: eine Sammlung realistischer Szenarien in `agent-test-prompts.jsonl` (die
Rubriken — erwartetes Verhalten, Erfolgskriterien, erwartete Tools — sind in
[`agent-test-prompts.md`](./agent-test-prompts.md) beschrieben).

```bash
uv run testprompt.py                 # alle Test-Prompts auflisten
uv run testprompt.py <id>            # einen Prompt laufen lassen (Rubrik + Live-Antwort)
uv run testprompt.py <id> --judge    # zusätzlich bewerten und archivieren
```

`--judge` bewertet den fertigen Lauf zweifach: ein **LLM-Judge** benotet die Antwort
gegen die Rubrik, und ein deterministischer Check misst aus dem Session-Trace, wie viel
der erwarteten Tools wirklich aufgerufen wurde — und was der Lauf gekostet hat
(Aufrufe, verschiedene Werkzeuge, Umwegfaktor, nicht eingeplante Werkzeuge; bewusst
ohne Schwellenwert, siehe [`agent-test-prompts.md`](./agent-test-prompts.md)). Das Judge-Modell steht in der Config
unter `evals.judge_model` (oder `--judge-model <anbieter/modell>`) — halte es **unabhängig**
vom getesteten Modell; benotet sich ein Modell selbst, warnt der Runner.

Für den ganzen Satz auf einmal:

```bash
uv run evals.py                      # gesamte Sammlung laufen lassen, bewerten, archivieren
uv run evals.py --filter building    # nur passende Tests (hier: die Gebäude-Szenarien)
uv run evals.py --gate               # exit 1 bei jedem FAIL (für CI)
uv run evals.py --report             # nur auswerten, ohne Agent/Judge/Netz
```

Jeder bewertete Lauf hängt eine Zeile an `.chester/evals/history.jsonl` — Regressionsreihe
und Modellvergleich in einem, inklusive Laufzeit (Agent und Judge getrennt gemessen, damit
ein langsamer Judge den Vergleich nicht verzerrt). `--report` aggregiert daraus Bestehensquote,
Tool-Abdeckung und mittlere Aufrufzahl pro Modell; dieselbe Auswertung zeigt der Slash-Befehl
`/eval` im Chat. Ein `-` in einer Spalte heißt „vor Einführung dieser Messung archiviert",
nie „null".

Test-Level 2 hat eine eigene, schlichtere Historie: `.chester/probes/history.jsonl`, eine
Zeile je Probe und Lauf (Zeitpunkt, Modell, bestanden, Dauer, ob der Zeitdeckel gerissen
wurde, und jede einzelne Prüfung). Kein Judge, keine Coverage — die Fragen dieser Stufe
sind mit `assert` zu beantworten. Der Tab **🔬 Test-Level 2** der Bench zeigt sie.

Wer lieber klickt, bekommt dieselbe Maschinerie als Weboberfläche:

```bash
uv run streamlit run test_app.py     # Test-Bench: Ausführen · Bearbeiten/Neu · Historie
```

Die Bench streamt den Tool-Austausch live mit, zeigt die erzeugte Karte eingebettet und
lässt Tests im Browser anlegen und ändern. Sie belegt Port `:8501` — also nicht parallel
zum Dashboard starten.

## Aufbau

```
setup.py                Gerüst — .chester/-Config + Workspace-Identität + Skills
agent_build.py          Capability-Factory (default_capabilities + geo) + Konstanten
gateway.py              Backend — selmakit.Gateway + Geo-Capabilities, SSE auf :8000
dashboard.py            Streamlit-Web-UI (selmakit.dashboard.run, spricht mit Gateway)
ask.py                  schlanker CLI-Chat (einmalig / interaktiv)
trace.py                Session-Trace-Viewer (Prompt → Tools → Antwort)
data.py                 GeoCache-Inventar ansehen/aufräumen (ohne LLM)
testprompt.py           einen Benchmark-Prompt ausführen + benoten (Test-Level 3)
evals.py                die ganze Prompt-Sammlung ausführen + Auswertung
probe.py                Mikro-Geo-Proben fahren (Test-Level 2, ohne Judge/Netz)
test_app.py             Test-Bench als Weboberfläche (Ausführen/Bearbeiten/Historie/Proben)
install.sh              geführte Erstinstallation (uv, Pakete, Config, QGIS, LLM)
start.sh                Gateway + Dashboard zusammen starten (lokal)
chester/
  qgis_env.py           qgis_process finden + Headless-Env (PROJ/GDAL)
  qgis_process.py       QgisProcess: list/help/run-Wrapper + Algorithmus-Cache
  workspace.py          resolve_path: unsaubere Pfade auf den Workspace kollabieren
  gate.py               erzwingende Validierungs-Schranke (Struktur/Visuell/Redundanz)
  geofacts.py           gemeinsame Fakten-Leser über Vektor/Raster (ohne Subprozess)
  probes.py             Prüfarten + Historie der Test-Level-2-Proben (ohne Modell)
  osmclip.py            OSM-Download auf die angefragte Grenze schneiden + berichten
  plausibility.py       Plausibilitätsbänder je Größenordnung (Höhe, Fläche, Neigung …)
  geocache.py           GeoCache: Inventar, Ablauf, Disk-Abgleich, Hintergrund-Sync
  geoconfig.py          Leser für den geodata-Konfigblock (Agent + CLI teilen ihn)
  provenance.py         Herkunfts-Sidecars (<datei>.meta.json: Quelle, Lizenz, TTL)
  evalhistory.py        Auswertung der Benchmark-Historie (für --report und /eval)
  qgis_bridge.py        LiveBridge: In-QGIS-Socket-Server (QtNetwork) für Live-Steuerung
  qgis_startup.py       `QGIS --code`-Einstieg, der die Bridge startet
  qgis_live_client.py   Chester-seitiger Socket-Client + Reuse/Launch (ensure_running)
  lod2.py · dgm1.py · boundaries.py · citymodel.py   offene DE-Datenquellen-Connectoren
  swisstopo.py · austria.py                          CH-/AT-Connectoren (DACH-Erweiterung)
  gtfs.py                                            ÖPNV-GTFS-Connector (DACH)
  regions.py                                         Land-/CRS-bewusste Quellenwahl (region_profile)
  capabilities/         die Geo-Domänen-Tools (SelmaKit-Capabilities):
    qgis · discovery · perception · vector · validation · mapoutput ·
    inventory · connectors · lod2 · boundaries · citymodel · statistics ·
    transit · qgis_live · qgis_python
skills/                 SKILL.md-Workflow-Rezepte
samples/                reproduzierbare Beispieldaten-Generatoren
doc/                    Docs: features, usage, qgis-process, qgis-bridge, geodata-concept, geodata-search, validation-concept, visual-validation, test-levels, agent-test-prompts
.chester/              Laufzeitzustand: Config, Sessions, Workspace, Memory (gitignored)
```

## Wie es funktioniert (in einem Absatz)

Eine natürlichsprachige Anfrage tritt in die SelmaKit-Agent-Schleife ein (wahrnehmen →
planen → handeln → **validieren** → ausgeben). Das LLM ruft Tools: Es kann einen Ort
`geocode`n, mit `stac_search` nach Szenen suchen, mit `fetch_raster` ein Band holen,
mit `fetch_dem`/`fetch_dgm1` Terrain (30 m bzw. 1 m), mit `fetch_lod2`/`fetch_cityjson`
gemessene Gebäudehöhen bzw. 3D-Modelle, mit `stats_table`/`fetch_boundaries` amtliche
Statistik + passende Grenz-Geometrie, mit `geodata_search` einen Open-Data-Katalog nach
einer Ebene, die OSM fehlt, jeden QGIS-Algorithmus suchen/beschreiben/ausführen, mit
`detect_water` via NDWI, Vektoren filtern, `check_crs` / `sanity_check_result`,
`render_map` (inklusive einer wertklassifizierten Choroplethe), `render_buildings_3d`
(3D-HTML) und `qgis_show` / `qgis_show_3d`, um das Ergebnis live in einem
QGIS-Desktop-Fenster (2D oder 3D) zu öffnen. QGIS läuft headless als Subprozess für die
Verarbeitung (nie PyQGIS
in-process), sodass die Umgebung des Agenten sauber bleibt. Korrektheit — CRS, Fläche,
Plausibilität — ist ein Pflichtschritt, weil Geodaten-Ergebnisse objektiv richtig oder
falsch sind.
