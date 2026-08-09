# `qgis_process` — CLI-Referenz (wie von Chester genutzt)

`qgis_process` ist QGIS' Kommandozeilen-Einstieg in das Processing-Framework. Es exponiert die
gesamte Processing-Toolbox — jeden nativen, GDAL-, GRASS-, SAGA-, WhiteboxTools- und
PDAL-Algorithmus — ohne die GUI zu öffnen. Chester nutzt es als seinen **einzigen
GIS-Ausführungspfad**: statt PyQGIS zu importieren,
schält jede Geooperation zu diesem Binary aus und parst das JSON, das es zurückgibt.

**Offizielle Dokumentation:**
<https://docs.qgis.org/latest/en/docs/user_manual/processing/standalone.html>
(QGIS User Manual §„Using processing from the command line").

Dieses Dokument hält das reale, verifizierte Verhalten auf der Maschine fest, auf der Chester
läuft:

> QGIS 4.2.0 „Belém do Pará", `/Applications/QGIS-final-4_2_0.app/Contents/MacOS/qgis_process`
> — 761 Algorithmen; Provider QGIS (native), GDAL und PDAL nutzbar. Der GRASS-Provider
> ist zwar registriert (307 `grass:*`-Algos werden gelistet/beschrieben), hat aber
> **kein lauffähiges Backend** — ein `grass:*`-Lauf scheitert zur Laufzeit („GRASS was
> not found"). SAGA/WhiteboxTools nicht installiert.

### GRASS-Status auf macOS (verifiziert + recherchiert)

**Kurz:** GRASS ist „gelistet, aber nicht lauffähig". Der Provider *listet* alle
`grass:*`-Algorithmen (introspektierbar via `qgis_process help grass:…`), aber jeder
`run` scheitert mit *„GRASS was not found or is not correctly installed"* — in der
`.app` liegt **kein GRASS-Binary**.

**Warum (nicht maschinenspezifisch, sondern die macOS-Paketierung):** Die
**macOS-Builds von QGIS 4.x bündeln GRASS nicht mehr.** Der Umstieg auf die neue,
**Homebrew-basierte Universal-/arm64-Paketierung** hat GRASS (eine schwere separate
C-Toolchain) nicht mitgezogen. Auf **Windows** (OSGeo4W) und **Linux** ist GRASS
weiterhin dabei — nur die macOS-4.x-Builds nicht. Getrackt als offener Feature-Request
[qgis/QGIS#62521 „Add GRASS to mac builds"](https://github.com/qgis/QGIS/issues/62521)
(gestellt vom Core-Entwickler m-kuhn) — **ohne Milestone/Zeitplan**, also keine Zusage,
in welcher Version GRASS zurückkommt (technisch machbar, da Homebrew eine `grass`-Formula
hat). Zweiter, verwandter Stolperstein selbst bei separater GRASS-Installation:
[qgis/QGIS#65363](https://github.com/qgis/QGIS/issues/65363) — beim Start aus dem Finder
findet QGIS `GISBASE` nicht (Env-Vererbung), Workaround `GRASS_PREFIX`/`GISBASE` setzen.

**Konsequenz für Chester:** Auf `grass:*` wird sich nicht verlassen. Wo GRASS-Funktionen
gebraucht würden, wird **in-process** gelöst (z. B. Dangle-Erkennung in
`geofacts.dangle_facts` / `check_topology(network=True)`). Will man GRASS doch aktivieren:
`GRASS-8.x.app` (bzw. `brew install grass`) installieren und `GISBASE`/`GRASS_PREFIX` in
der von `chester/qgis_env.py` gebauten Subprozess-Umgebung setzen — dann laufen die
`grass:*`-Algos (auf dieser Maschine ungetestet, solange GRASS nicht installiert ist).

> ⚠️ Subcommand-Namen haben sich über QGIS-Versionen geändert. Insbesondere nutzt **QGIS 4.x
> `help`, um einen Algorithmus zu beschreiben — `describe` existiert nicht** (es scheitert mit
> „Command describe not known!"). Ältere Blogposts sagen mitunter anderes.

---

## 1. Das Binary finden

- **macOS** (App-Bundle): `<App>.app/Contents/MacOS/qgis_process`
- **Linux** (Paket): meist auf `PATH` als `qgis_process`, sonst `/usr/bin/qgis_process`
- **Windows**: `qgis_process-qgis.bat` im QGIS-`bin`-Verzeichnis

In Chester wird das von `chester/qgis_env.py` (`resolve_qgis_env()`) aufgelöst, das auch die in
§2 beschriebene Umgebung baut. Discovery mit `CHESTER_QGIS_PROCESS_BIN` oder `CHESTER_QGIS_APP`
überschreiben.

---

## 2. Erforderliche Umgebung (headless + korrektes CRS)

QGIS bringt sein eigenes gebündeltes Qt, PROJ und GDAL-Data mit. Zwei Dinge müssen für
verlässliche headless-Läufe gesetzt sein:

| Variable | Warum | Beispiel (macOS-Bundle) |
|---|---|---|
| `QT_QPA_PLATFORM=offscreen` | Ohne Display-Server laufen | `offscreen` |
| `PROJ_DATA` (und `PROJ_LIB`) | `proj.db` finden — **ohne sie ist die Reprojektion still falsch** | `…/Contents/Resources/qgis/proj` |
| `GDAL_DATA` | GDAL-Koordinaten-/Format-Metadaten | `…/Contents/Resources/qgis/gdal` |

Symptom eines fehlenden `PROJ_DATA`: `proj_context_get_database_metadata: Cannot find proj.db`
auf stderr, gefolgt von falscher/leerer CRS-Behandlung. Mit gesetzten Variablen verschwindet die
Warnung und `qgis_process --version` meldet volle PROJ/GEOS/EPSG-Details.

---

## 3. Globale Nutzung und Optionen

```
qgis_process [--help] [--version] [--json] [--verbose] [--no-python]
             [--skip-loading-plugins] [command]
             [algorithm id | path to model | path to Python script] [parameters]
```

| Option | Wirkung |
|---|---|
| `--help`, `-h` | Hilfe drucken |
| `--version`, `-v` | Alle QGIS/GDAL/PROJ/GEOS/Qt-Versionen drucken |
| `--json` | Ergebnisse als JSON-Objekt auf stdout ausgeben (maschinenlesbar) |
| `--verbose` | Verbose-Logs |
| `--no-python` | Python-Support deaktivieren → schnellerer Start |
| `--skip-loading-plugins` | Aktivierte Plugins überspringen → schnellerer Start |

**Tipp:** `--no-python --skip-loading-plugins` senkt die Per-Call-Startlatenz merklich, wenn man
nur Core-/GDAL-Algorithmen braucht.

---

## 4. Befehle

| Befehl | Zweck |
|---|---|
| `list` | Alle verfügbaren Algorithmen listen (nach Provider gruppiert) |
| `help <id>` | Hilfe/Parameter für einen Algorithmus zeigen |
| `run <id> …` | Einen Algorithmus ausführen |
| `plugins` | Verfügbare und aktive Plugins listen |
| `plugins enable <name>` | Ein installiertes Plugin aktivieren |
| `plugins disable <name>` | Ein installiertes Plugin deaktivieren |

`<id>` kann auch ein Pfad zu einem Processing-Modell (`.model3`) oder einem Python-Skript sein.

---

## 5. `list` — Algorithmen entdecken

```bash
qgis_process list                 # menschenlesbar, nach Provider gruppiert
qgis_process list --json          # maschinenlesbarer Katalog
```

### JSON-Form

```jsonc
{
  "providers": {
    "native": {
      "name": "native",
      "long_name": "QGIS (native c++)",
      "is_active": true,
      "algorithms": {
        "native:buffer": {
          "name": "Buffer",
          "group": "Vector geometry",
          "short_description": "Computes a buffer area for all features …",
          "tags": ["buffer", "grow", "fixed", "variable", "distance"],
          "deprecated": false,
          "has_known_issues": false,
          "requires_matching_crs": false,
          "help_url": null
        }
        // … weitere Algorithmen
      }
    }
    // … gdal, pdal, … Provider
  },
  "qgis_version": "4.2.0-Belém do Pará",
  "gdal_version": "…", "proj_version": "…", "geos_version": "…"
}
```

Algorithmen liegen **unter jedem Provider**, per id verschlüsselt (`provider:name`). Chester
flacht das in eine `{id: metadata}`-Map ab und cacht sie (siehe `QgisProcess.algorithms()`),
dann macht es Keyword-Matching über id/name/description/tags für `qgis_search`.

---

## 6. `help` — einen Algorithmus beschreiben

```bash
qgis_process help native:buffer --json
```

> In QGIS 4.x ist das der einzige Weg, Parameter aus dem CLI zu introspizieren; `describe` ist
> **kein** gültiger Befehl.

### JSON-Form

```jsonc
{
  "algorithm_details": {
    "id": "native:buffer",
    "name": "Buffer",
    "group": "Vector geometry",
    "short_description": "…",
    "tags": ["buffer", "grow", …]
  },
  "parameters": {
    "DISTANCE": {
      "name": "DISTANCE",
      "description": "Distance",
      "optional": false,
      "default_value": 10,
      "type": { "name": "distance", "acceptable_values": [ … ] },
      "raw_definition": { … }
    },
    "INPUT":  { … },
    "OUTPUT": { … }
    // …
  },
  "outputs": {
    "OUTPUT": { "description": "Buffered", "type": { … } }
  }
}
```

`parameters` und `outputs` sind **Maps, per Parametername verschlüsselt**. Jeder Eintrag gibt
`description`, `optional`, `default_value` und einen `type` (String oder `{name,
acceptable_values}`). Chester verdichtet das zum Schema, das `qgis_describe` zurückgibt.

### Enum-Parameter

Enum-Parameter (z. B. `PREDICATE`, `STATISTICS`) werden **per Integer-Index** übergeben, und
`acceptable_values` dokumentiert nur das Format („Number of selected option, e.g. '1'" /
„Comma separated list, e.g. '1,3'"), nicht die Labels. Die Index-Reihenfolge folgt der
Algorithmus-Definition. Zwei, die Chesters Shortcuts nutzen:

`native:extractbylocation` → `PREDICATE`:

| 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|
| intersects | contains | disjoint | equals | touches | overlaps | within | crosses |

`native:zonalstatisticsfb` → `STATISTICS` (Default `[0,1,2]`):

| 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 |
|---|---|---|---|---|---|---|---|---|---|----|----|
| count | sum | mean | median | stdev | min | max | range | minority | majority | variety | variance |

---

## 7. `run` — einen Algorithmus ausführen

Zwei Wege, Parameter zu übergeben.

### 7a. Inline (`--PARAM=VALUE`)

```bash
qgis_process run native:buffer \
  --INPUT=points.gpkg --DISTANCE=100 --OUTPUT=buffer.gpkg
```

Einen Parameter wiederholen, um eine geordnete Liste zu bauen:

```bash
qgis_process run some:alg --LAYERS=a.shp --LAYERS=b.shp …
```

### 7b. JSON über STDIN (was Chester nutzt)

Ein einzelnes `-` anstelle der Parameterliste sagt `qgis_process`, ein JSON-Objekt von stdin zu
lesen. **Das impliziert `--json` für die Ausgabe.** Das Objekt muss eine `"inputs"`-Map enthalten:

```bash
echo '{"inputs": {"INPUT": "points.gpkg", "DISTANCE": 100, "OUTPUT": "buffer.gpkg"}}' \
  | qgis_process run native:buffer --json -
```

Optionale Top-Level-Schlüssel neben `"inputs"`:

- `"ellipsoid"` — Ellipsoid für Distanz-/Flächenberechnungen (CLI: `--ELLIPSOID=name`)
- `"project_path"` — ein existierendes QGIS-Projekt zum Laden (CLI: `--PROJECT_PATH=path`)

### Ergebnis-JSON-Form

```jsonc
{
  "results": { "OUTPUT": "buffer.gpkg" },   // ← Ausgabepfade / -werte
  "inputs":  { "INPUT": "points.gpkg", "DISTANCE": 100, "OUTPUT": "buffer.gpkg" },
  "algorithm_details": { "id": "native:buffer", "name": "Buffer", … },
  "log": "…",
  "qgis_version": "…", "gdal_version": "…", "proj_version": "…"
}
```

Die Map, die zählt, ist `results` — sie hält den aufgelösten Pfad oder Wert jeder Ausgabe.
Ausgabeformate werden aus der Dateiendung abgeleitet (`.gpkg`, `.geojson`, `.shp`, `.tif`, …).

---

## 8. Fehler

- **Fehlende Pflichtparameter** → Exit ungleich null mit einem Klartext-`ERROR: The following
  mandatory parameters were not specified`, der sie auflistet (kein JSON, auch unter `--json`).
- **Unbekannter Befehl** (z. B. `describe`) → `Command describe not known!` auf stderr.
- Chesters Wrapper (`QgisProcess._invoke`) behandelt „kein parsbares JSON auf stdout" als Fehler
  und wirft `QgisProcessError` mit dem stderr/stdout-Snippet; die Capability macht daraus ein
  `{"ok": false, "error": …}`-Tool-Ergebnis, sodass das LLM es lesen und anpassen kann, statt
  den Lauf abstürzen zu lassen.

---

## 9. Schnellrezepte

```bash
# Wie viele Algorithmen / welche Provider?
qgis_process list --json | jq '.providers | keys'

# Slope-bezogene Algorithmen finden
qgis_process list --json \
  | jq -r '.providers[].algorithms | keys[]' | grep -i slope

# Parameter eines GDAL-Algorithmus inspizieren
qgis_process help gdal:rastercalculator --json | jq '.parameters | keys'

# Vor dem Messen nach UTM 32N (metrisch) reprojizieren
echo '{"inputs":{"INPUT":"in.geojson","TARGET_CRS":"EPSG:25832","OUTPUT":"utm.gpkg"}}' \
  | qgis_process run native:reprojectlayer --json -

# Gebäudehöhen-Raster: DSM minus DTM
echo '{"inputs":{"INPUT_A":"dsm.tif","BAND_A":1,"INPUT_B":"dtm.tif","BAND_B":1,"FORMULA":"A-B","OUTPUT":"height.tif"}}' \
  | qgis_process run gdal:rastercalculator --json -
```

---

## 10. Wie Chester darauf abbildet

| Chester-Tool | `qgis_process`-Aufruf |
|---|---|
| `qgis_search(keyword)` | `list --json` (gecacht) + lokale Filterung |
| `qgis_describe(id)` | `help <id> --json` → verdichtetes Schema |
| `qgis_run(id, params)` | `run <id> --json -` mit `{"inputs": params}` auf stdin |
| 9 Shortcuts (`qgis_buffer`, …, `qgis_extract_by_attribute`) | `run <fixed id> --json -` mit einem typisierten Param-Dict |

Implementierung: `chester/qgis_process.py` (Runner) und `chester/capabilities/qgis.py` (die
SelmaKit-Capability / LLM-Tools).

---

*Verifiziert gegen QGIS 4.2.0 auf macOS. Das Verhalten des `list`/`help`/`run`-JSON ist innerhalb
einer Major-Version stabil; Enum-Index-Reihenfolgen sind über Versionen hinweg stabil.*

**Referenzen**
- Offizielle QGIS-Docs — Using processing from the command line:
  <https://docs.qgis.org/latest/en/docs/user_manual/processing/standalone.html>
- Bei Bedarf auf ein bestimmtes Release pinnen, z. B.
  <https://docs.qgis.org/3.44/en/docs/user_manual/processing/standalone.html>
