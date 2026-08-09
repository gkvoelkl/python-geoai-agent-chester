# Chester und Geodaten — ein Konzept

> Dieses Dokument deckt die **Daten** ab, die durch Chester fließen — woher sie kommen, wo
> sie leben und wie sie altern. Siehe auch [`geodata-search.md`](./geodata-search.md) für das
> Finden *autoritativer offener Daten*, wenn OpenStreetMap eine Ebene fehlt.

## 0. These

QGIS-Processing (426 Algorithmen über `qgis_process`) ist ein gelöstes Problem —
deterministisch und auf jeder Maschine identisch. **Der unterscheidende Teil eines
GeoAI-Agenten sind Daten: Akquise und Bewusstsein** — eine unscharfe Anfrage
(„Flutausdehnung um Ahrweiler, Juli 2021") in die richtigen Inputs verwandeln, wissen, was
schon auf der Platte liegt, und Quelle und Lizenz jeder Ebene verfolgen.

Die atomare Einheit ist ein **GeoDataset** — eine Ebene (eine Vektortabelle oder ein Raster)
mit ihrem CRS, ihrer Ausdehnung und ihrem Schema. Chester erreicht GeoDatasets über **zwei
Bestände, und nichts sonst**:

- **GeoConnectors** — Live-Verbindungen zu externen Quellen. Ein Connector erreicht **ein oder
  mehrere GeoDatasets**: OSM immer eines (das Query-Ergebnis), ein GeoPackage oder ein
  PostGIS-Schema viele. Connectors sind der einzige Weg, auf dem neue Daten hereinkommen.
- der **GeoCache** — die lokalen GeoDatasets, die Chester tatsächlich verarbeitet: Daten, die
  über einen Connector *heruntergeladen* wurden, **und** Daten, die Chester selbst *erzeugt*
  hat (Clips, Indizes, Karten). Es ist ein **Cache**: jeder Eintrag ist wegwerfbar und
  neu-erzeugbar, deshalb **altert jeder Eintrag und wird irgendwann geprunt**.

Alles folgt aus dieser Aufteilung: Connectors füllen den Cache, der Cache ist das, wovon der
Agent *weiß*, und der Cache bleibt beschränkt, weil seine Einträge ablaufen. Nichts im Cache
ist kostbar — ein heruntergeladener Datensatz wird über seinen Connector neu geholt, ein
abgeleiteter neu berechnet.

## 1. Aktueller Stand

> _Diese Tabelle ist die **Pre-Phase-5-Baseline**, gegen die das Konzept geschrieben wurde.
> Diese Lücken sind inzwischen geschlossen (DEM, Multi-Katalog-STAC, OGC-WFS, LiDAR, Container-
> + statistische Connectoren und ein voller GeoCache mit Provenienz/Ablauf)._

| | Heute | Lücke |
|---|---|---|
| **GeoConnectors** | drei — geocode, OSM, STAC (§3) | kein DEM, ein einziger STAC-Katalog, keine OGC-Dienste, keine Container-Connectoren (PostGIS / SpatiaLite / GeoPackage) |
| **GeoCache** | flaches `.chester/workspace/`, kein Inventar; `vector_info`/`check_crs` inspizieren **eine** Datei | kein Bewusstsein (Agent rät seine eigenen Dateinamen über Runden hinweg), keine Provenienz, kein Ablauf (Downloads häufen sich ewig) |

---

## 2. Der GeoCache — lokaler Store, Bewusstsein, Ablauf

Ein Verzeichnis, **`.chester/workspace/geocache/`**, das jedes **gecachte** GeoDataset
(heruntergeladen oder selbst erzeugt) mit einem Sidecar neben jeder Datei und einem Inventar
an seiner Wurzel hält — das auch Datensätze aus Daten-Roots (§4.2) katalogisiert, obwohl die
außerhalb von `geocache/` liegen. Identität/Skills/Memory bleiben, wo sie sind. Über
`resolve_path` ist das nur eine Änderung des Default-Ausgabeverzeichnisses, keine Änderung der
Tool-Signatur; bestehende flache Dateien werden beim nächsten `geocache_sync()` absorbiert.

### 2.1 Bewusstsein — das Inventar

**Der Cache spiegelt ein veränderliches Dateisystem.** Ein Datensatz kann zwischen Runden
gelöscht, verschoben, reprojiziert oder **abgelaufen** sein, ein Inventar, das nur *erinnert*,
**driftet** also und weist den Agenten auf eine veraltete oder fehlende Datei — schlimmer als
keins. Der Fix: ein `geocache_sync()`, das **Platte ↔ Inventar** abgleicht (neue Datensätze
hinzufügen, fehlende entfernen, geänderte auffrischen). *Nur Erinnern ist ein Bug; Erinnern +
Abgleichen ist das Design.*

Das Inventar ist eine einzige persistente, menschenlesbare **`geocache/geocache.md`**
(Klartext, zeilen-diffbar; liegt unter dem gitignorierten `.chester/`, also inspizierbar,
nicht versioniert), eine Zeile pro **GeoDataset** — ein mehrschichtiges GeoPackage expandiert
also zu mehreren Zeilen. Von `geocache_sync()` regeneriert, trägt es zwei Spaltenarten:

- **Fakten** — CRS, bbox (nativ + WGS84), Feature-/Band-Zahl, Geometrietyp, Felder, Pixelgröße,
  nodata, Größe, mtime. Aus jedem Datensatz *gescannt* (`vector_info`/`check_crs`
  wiederverwendend); immer neu-ableitbar.
- **Semantik & Lifecycle** — source, query, licence, `created_at`, `last_used`, `expires_at`.
  Aus `geocache_note`, dem Read-Path-Touch (§2.2) und Provenienz-Sidecars (§2.4); nicht durch
  Scannen neu-ableitbar.

**Tools, die der Agent bekommt** — eine `GeoInventoryCapability`, ergänzt in
`geo_capabilities()`:

| Tool | Was es tut |
|---|---|
| `geocache_list(filter=…)` | Listet die **gecachten** Datensätze — „was habe ich lokal?", statt Dateinamen über Runden zu raten. Expandiert mehrschichtige Container. |
| `geocache_sync()` | Inventar ↔ Platte abgleichen: Workspace + Daten-Roots (§4.2) scannen, neue hinzufügen, fehlende entfernen, geänderte auffrischen und **abgelaufene** löschen (§2.2). |
| `geocache_note(path, note)` | Zweck/Absicht/Semantik an einen Datensatz heften (der wirklich *erinnerte* Teil); kann auch die TTL pinnen (§2.2). |

Um zu listen, was erreichbar, aber *noch nicht gecacht* ist, siehe die Connector-Tools (§3).

`geocache_sync()` läuft beim Gateway-Start und implizit vor jedem `geocache_list`, das Inventar
bleibt also korrekt, selbst wenn der Agent vergisst, etwas zu erfassen. Datensätze werden per
Endung erkannt (`.gpkg .geojson .shp .gml .tif .nc .laz …`), dann durch Öffnen bestätigt.

**Im Prompt:** `get_instructions()` injiziert jede Runde eine **Zeile-pro-Datensatz**-
Zusammenfassung (Name, Art, CRS, Ausdehnung, Alter) — wie `WorkspacePromptCapability` die
Identitätsdateien injiziert. Auf die N jüngsten gedeckelt; für den Rest auf `geocache_list`
zurückfallen.

**Als Befehle** (kanal-abgefangen, kein Modell-Roundtrip):
- **`/geocache`** — alle gecachten Daten listen (druckt `geocache.md`, der „aktuelle Daten
  zeigen"-Befehl).
- **`/geocache prune`** — die abgelaufenen Einträge jetzt **löschen** (Datei + Sidecar +
  Inventarzeile); `--dry-run` zeigt die Liste erst vorab (§2.2).
- **`/geocache rm <dataset>`** — einen benannten gecachten Datensatz löschen (Datei + Sidecar +
  Inventarzeile) unabhängig von der TTL. **Verweigert** bei einem referenzierten
  `source: user`-Daten-Root-Datensatz — das ist nicht Chesters zu löschen, und der nächste
  `geocache_sync` würde ihn ohnehin wieder aufnehmen; stattdessen den Root aus der Config
  entfernen (§4.2/§2.4).
- **`/geocache rm all`** — den **ganzen** Cache leeren: synct, dann jeden
  `source != user`-Datensatz löschen (Datei + Sidecar), referenzierte `source: user`-Roots
  behaltend. `--dry-run` zeigt die Liste erst vorab.

Das Cache-CLI spiegelt diese: `uv run data.py` (list), `data.py --prune`. Connector-Ansichten
haben eigene Befehle — `/geoconnector`, `/geodataset` (§3).

### 2.2 Ablauf & Pruning (der Cache ist beschränkt)

**Jeder Cache-Eintrag altert — heruntergeladen und selbst erzeugt gleichermaßen.** Jeder
Datensatz erfasst ein `created_at`, ein `last_used` und eine `ttl` → ein `expires_at` (ab
`last_used` gezählt, §2.4). **`geocache_sync()` löscht Einträge jenseits `expires_at`** (Datei
+ Sidecar) als Teil des Abgleichs, Ablauf wird also bei jedem Sync erzwungen — beim
Gateway-Start, vor jedem `geocache_list` und wann immer der Cron ihn läuft. Der Cache kann nie
unbeschränkt wachsen.

- **Pruning ist automatisch und erzwingbar.** Ablauf wird von `geocache_sync()` selbst erzwungen
  (Cron läuft es planmäßig für lang-untätige Sitzungen). Um es auf Abruf zu erzwingen:
  **`/geocache prune`** (oder `data.py --prune`) löscht die abgelaufenen Einträge jetzt —
  `--dry-run` zeigt die Liste ohne Löschen. Einen Datensatz pinnen (nächster Punkt), um ein
  Arbeitsergebnis vor dem Ablauf zu behalten.
- **TTL-Defaults, überschreibbar.** Ein globaler Default (z. B. 30 Tage) mit
  Per-Connector-Overrides — kurz für schnell wechselnde Vektoren (OSM), länger für
  nahezu-statische Raster (DEM). Ein `geocache_note` kann einen Datensatz **pinnen** (TTL
  verlängern oder einfrieren), wenn der Nutzer ein Arbeitsergebnis behalten will.
- **Touch beim Lesen.** Einen Datensatz als Tool-Input zu lesen stupst seine Uhr (LRU-artig):
  die Toolbox stempelt ein `last_used` auf den Datensatz-Record (sein Sidecar oder den
  Inline-Inventareintrag in Phase 1) **bevor** `geocache_sync()` den Ablauf auswertet, eine
  aktiv bearbeitete Ebene wird also nie mitten im Workflow geprunt. Der Touch liegt im
  Read-Path, nicht im Ermessen des Agenten.
- **Durch Pruning geht nichts verloren.** Ein geprunter Download wird über seinen Connector
  mit der Provenienz-Query neu geholt (§2.4); ein geprunter abgeleiteter Datensatz durch
  Neu-Ausführen des Schritts neu berechnet. *Deshalb* ist aggressiver Ablauf sicher.
- **Raus aus dem Cache, raus aus dem Ablauf.** Nur-lesende **Daten-Roots** (§4.2) werden im
  Inventar katalogisiert, aber an Ort und Stelle referenziert, und die Live-Quellen hinter
  **Container-Connectoren** (§3.2) werden nie komplett kopiert — beide bleiben außerhalb des
  Cache und werden **nie geprunt**; nur die lokalen Cache-Kopien, die sie erzeugen, altern.
- **osmnx-`cache/`** ist ein HTTP-Cache (Overpass/Nominatim) mit eigenem Lebenszyklus — aus
  `geocache_sync()` und aus diesem Ablauf ganz herausgehalten.

### 2.3 CRS-Bewusstsein

Das Inventar hält das CRS jedes Datensatzes, dort gehört also auch eine **CRS-Mismatch-Warnung**
über Ebenen hin, die kombiniert werden sollen — vor einem fehlerhaften Overlay ausgelöst, nicht
danach.

### 2.4 Provenienz-Sidecars

Jeder Datensatz, den der Cache hält, bekommt ein `<file>.meta.json`:

```json
{ "source": "osm/overpass", "query": {"tags": {"building": true}, "place": "Pentling"},
  "created_at": "2026-06-29T18:22:05Z", "last_used": "2026-06-30T09:14:50Z",
  "ttl_days": 14, "crs": "EPSG:4326", "licence": "ODbL-1.0", "tool": "osm_features" }
```

Es erledigt drei Aufgaben auf einmal:

1. **Lifecycle** — `ttl_days` ab `last_used` (mit Rückfall auf `created_at`) ergibt `expires_at`
   und treibt den Ablauf; der Read-Path frischt `last_used` auf (Touch-on-Read, §2.2).
2. **Neu-Erzeugung** — `source` + `query` lassen einen abgelaufenen oder gelöschten Download
   über seinen Connector **neu holen**; bei einem abgeleiteten Datensatz sagt die erfasste
   Tool-Kette, wie man ihn neu baut. Die Wegwerfbarkeit hängt daran.
3. **Lizenz-Bewusstsein** — lässt den Agenten Lizenzen zitieren (für die meisten offenen Daten
   Pflicht) und `render_map` **Attribution** für jede gezeichnete Ebene stempeln — automatisch
   aus dem Inventar zusammengesetzt.

`source` fällt in **drei Klassen, die den Lifecycle bestimmen**: `connector/*` (heruntergeladen
— altert, neu-holbar), `chester` (selbst erzeugt — altert, neu-berechenbar) und **`user`**
(deine eigenen Daten, an Ort und Stelle über einen Daten-Root referenziert — **nie geprunt**,
§2.2). Weil Daten-Roots nur-lesend sind, wird die Klasse eines `user`-Datensatzes im Inventar
selbst erfasst (aus seiner Lage ableitbar), nicht in einem in deinen Ordner geschriebenen
Sidecar. Die Nie-Prunen-Regel für `source: user` lebt in der Inventar-Capability, nicht in einem
Skill oder im Ermessen des Agenten — den Verlust der eigenen Datei des Nutzers darf nicht davon
abhängen, ob das LLM einem Rezept folgt.

Provenienz ist Teil von „Korrektheit ist eine Loop-Phase": sie macht Läufe reproduzierbar. Ein
Datensatz ohne Sidecar erscheint trotzdem (nur Fakten) und altert auf der Default-TTL; Provenienz
reichert an und macht ihn neu-erzeugbar, ist zum Gelistetwerden aber nicht erforderlich.

Sidecars sind der eine Teil, der **nicht** in `geocache.md` kollabieren kann: Fakten werden durch
Scannen neu-abgeleitet, Semantik muss *mit der Datei reisen*, wenn sie herauskopiert wird.
**Minimal-first:** Phase 1 kann `created_at`/`last_used`/`expires_at` inline im Inventar halten
und den vollen Sidecar später als das durable Upgrade ergänzen.

---

## 3. GeoConnectors — woher Daten kommen

Ein Connector ist ein dünnes Tool über einer Art Live-Quelle, das **ein oder mehrere
GeoDatasets** exponiert. Zwei Geschmacksrichtungen:

- **Query-Connectoren** erzeugen aus einer Query einen einzelnen Datensatz — geocode, OSM
  (eins), STAC (ein Raster pro Fetch). Man enumeriert sie nicht; man fragt.
- **Container-Connectoren** halten eine enumerierbare Menge existierender Datensätze, die man
  **listet, dann zieht** — PostGIS, SpatiaLite (a.k.a. GeoSQLite), GeoPackage.
- **Statistische Connectoren** holen eine *Tabelle* (ohne Geometrie), die an eine Admin-Ebene
  gejoint wird — Wikidata (Bevölkerung/Fläche je Gemeinde, AGS-Schlüssel), Eurostat (NUTS) und
  World Bank (je Land, ISO-3) (§3.6), alle credential-frei. Der Join-Schlüssel (AGS / NUTS /
  ISO-3) reitet in der Tabelle mit.

Alle teilen das Muster: `resolve_path` gibt in den Cache aus, `{"ok": false, …}` bei Fehler, ein
Sidecar (§2.4) bei Erfolg — jeder Pull altert also wie jeder andere Cache-Eintrag (§2.2).

| Connector | Exponiert | Status |
|---|---|---|
| **Geocode** (Nominatim) | ein Punkt/bbox pro Ort | ✓ |
| **OSM** (`osm_features`, Overpass) | ein Datensatz pro Query | ✓ |
| **STAC** (`stac_search`/`fetch_raster`) | ein Raster pro Fetch (ein fest verdrahteter Katalog) | ✓ |
| **DEM** (`fetch_dem`) | ein Höhenraster pro bbox | vorgeschlagen — §3.1 |
| **GeoPackage** | **viele** Ebenen in einer `.gpkg` | vorgeschlagen — §3.2 |
| **SpatiaLite** | **viele** Ebenen in einer `.sqlite` | vorgeschlagen — §3.2 |
| **PostGIS** | **viele** Tabellen in einem Schema | vorgeschlagen — §3.2 |
| **Multi-Katalog-STAC** | ein Raster pro Fetch, viele Kataloge | vorgeschlagen — §3.3 |
| **OGC-Dienste** (`wfs_features`/`wms_map`/`wcs_coverage`) | eine Ebene pro Request | vorgeschlagen — §3.4 |
| **LiDAR** (`fetch_pointcloud`) | eine Punktwolke pro bbox | vorgeschlagen — §3.5 |
| **Statistik** (`stats_table`) | eine Tabelle pro Code, an Geometrie gejoint (AGS/NUTS) | ✓ gebaut — §3.6 |

> **Status-Hinweis.** Dieser Abschnitt ist ein Design-Snapshot. Die als *vorgeschlagen*
> markierten Zeilen (§3.1–3.5) sind **inzwischen gebaut**. (Ebenfalls seither gebaut, über
> das ursprüngliche Konzept hinaus: die DACH-Connectoren — Schweiz/Österreich — und der
> GTFS-Transit-Connector.)

### Listen, was verfügbar ist

Zwei Tools beantworten „was kann ich erreichen?" — das Connector-seitige Gegenstück zu
`geocache_list`s „was habe ich?":

| Tool | Was es tut |
|---|---|
| `geoconnectors_list()` | Die konfigurierten Connectoren und ihre Art (query / container). |
| `geodatasets_list(connector=…)` | Die GeoDatasets, die ein **Container**-Connector exponiert — PostGIS-Tabellen, GeoPackage-/SpatiaLite-Ebenen — mit Geometrie/Raster, CRS/SRID, Zeilen/Ausdehnung. |

Ein Container-Connector bietet dann ein uniformes **describe → fetch**-Paar:
`geodataset_describe(connector, dataset)` (Spalten, Typen, Ausdehnung) und
`geodataset_fetch(connector, dataset, bbox, where, output)` (einen gewählten Datensatz in ein
Cache-GeoPackage subsetten, wo er wie jeder Download altert).

Beide Ansichten sind auch **Kanal-Befehle** (kein Modell-Roundtrip): **`/geoconnector`** listet
die konfigurierten GeoConnectors, **`/geodataset [<connector>]`** die aktuell über sie
erreichbaren GeoDatasets — das Connector-seitige Gegenstück zu `/geocache`.

### Vorgeschlagene Connectoren, nach Wert sortiert

1. **DEM / Höhe — größte Lücke.** `terrain-analysis` und `building-heights` *brauchen* ein DTM,
   aber nichts holt eins. `fetch_dem(bbox)` über **Copernicus DEM GLO-30** (frei, global, 30 m)
   via STAC oder OpenTopography → schließt zwei Skills end-to-end.
2. **Container-Connectoren — PostGIS, SpatiaLite, GeoPackage.** Erreichen die bestehenden
   Datensatz-Sammlungen des Nutzers: `geodatasets_list` zum Sehen, `geodataset_fetch` zum Ziehen
   eines Subsets. Datei-Container (`.gpkg`, `.sqlite`) sind zero-config; **PostGIS ist der
   Hauptwunsch** (Config + SQL-Sicherheit in §4.3).
3. **Multi-Katalog-STAC.** Eine kleine Registry über die fest verdrahtete Element84-URL hinaus
   (Earth Search, **Planetary Computer**, **Copernicus Data Space**). PC braucht Asset-URL-
   Signierung (`planetary_computer.sign`), sonst 403t `fetch_raster`. Schaltet Landsat,
   Sentinel-1, Copernicus DEM, WorldCover hinter einem Tool frei.
4. **OGC-Webdienste (WFS/WMS/WCS/OGC API Features).** Wie autoritative/nationale Daten
   veröffentlicht werden — `wfs_features` (Vektor) + `wms_map`/`wcs_coverage` (Raster).
   Standard-basiert: ein Tool deckt Tausende Dienste ab.
5. **LiDAR / Punktwolken.** `lidar-ground` konsumiert `.laz/.las`, aber nichts holt es.
   `fetch_pointcloud(bbox)` über OpenTopography oder Landesportale (z. B. OpenGeodata.NRW).
   Unterhalb des Raster-DEM (§3.1), das die meisten Bedarfe deckt.
6. **Nice-to-have:** gekachelte Basiskarten / XYZ-WMTS als `render_map`-Kontext (kosmetisch);
   STAC-Index-Discovery (`stac_catalogs(keyword)` über stacindex.org), um einen unbekannten
   Katalog zu finden, sobald die Registry (§3.3) existiert.
7. **Statistische Connectoren (§3.6, gebaut — Phase 5.8).** Amtliche Statistik holen, um sie an
   Admin-Geometrie zu joinen für thematische (Choroplethen-)Karten: `stats_sources` /
   `stats_search` / `stats_table` über Wikidata (SPARQL, DE je Gemeinde/Kreis Bevölkerung +
   Fläche, AGS-Schlüssel), Eurostat (JSON-stat, EU-weit, NUTS) und World Bank (Indicators API,
   global je Land, ISO-3) — alle credential-frei (die GENESIS-2020-Quellen, die Accounts
   brauchten, wurden entfernt). Der Connector liefert die Tabelle (CSV in den Cache, mit einem
   AGS/NUTS-Join-Schlüssel + Provenienz-Sidecar); der Join an Geometrie ist ein normaler
   QGIS-Schritt (`native:joinattributestable`), dann rendert `render_map(column=…)` die
   klassifizierte Choroplethe.

**Out of scope:** Bulk-Archive, authentifizierte kommerzielle APIs (Planet, Maxar), alles im
Terabyte-Bereich. Chester holt *Ausschnitte von Interesse*, keine Archive.

---

## 4. Deine eigenen Bestände („kann ich Chester meine Daten geben?")

Über die zwei Säulen hinweg — deine Dateien werden **an Ort und Stelle referenziert** oder in
den Cache kopiert, deine Datenbanken und Dienste werden Connectoren.

### 4.1 Eine Quelle onboarden — der `connect-data`-Skill
Wann immer du Chester auf eine Quelle zeigst — *„hier ist meine `house.gpkg`"*, ein PostGIS-DSN,
eine SpatiaLite-Datei — fährt der **`connect-data`-Skill** ein uniformes Onboarding. Er deckt
alle drei Container-Arten (GeoPackage, SpatiaLite, PostGIS) ab, weil sie das gleiche Trio teilen
(§3):

1. Die Quelle als Container **erkennen** (`resolve_path` / DSN).
2. **Fragen**, wie hereingebracht werden soll — *an Ort und Stelle referenzieren* oder *eine
   Arbeitskopie importieren*:

   | Wahl | Was passiert | Wann |
   |---|---|---|
   | **In place** (Daten-Root) | zero-copy, Original direkt gelesen, **nie geprunt**, immer aktuell | große oder autoritative Daten; nichts dupliziert |
   | **Arbeitskopie** (in den Cache importieren) | eine Kopie/Subset altert wie jeder Download; das **Original bleibt unangetastet** und neu-importierbar | ein isoliertes Arbeitsset |

3. Die Datensätze **listen** — `geodatasets_list` (ein Container hält eine *oder viele* Ebenen),
   mit CRS, Geometrie, Zahl.
4. **Katalogisieren & berichten** — `geocache_sync` schreibt eine Inventarzeile pro Ebene;
   Chester berichtet *„house.gpkg hält buildings (EPSG:25832, 412 Polygone), parcels, …"*.

So oder so bleibt das Original **`source: user` und altert nie** (§2.4); nur Cache-Kopien tun
es. Der Skill ist das *Rezept*; die Nie-Prunen-Garantie liegt in der Capability (§2.4), damit sie
nicht übersprungen werden kann.

### 4.2 Lokale Dateien & Daten-Roots (kein Code)
Ein nur-lesender **Daten-Root** ist der „in place"-Pfad für einen ganzen Ordner —
`geocache_sync` katalogisiert seine Datensätze, `resolve_path` liest sie, wo sie liegen. Ein
Daten-Root wird **nur je gelesen**: `resolve_path` confineт jede Tool-*Ausgabe* auf `geocache/`,
ein Buffer um dein Shapefile landet also im Cache, nie zurück in deinem Ordner. Schreibvorgänge
außerhalb des Cache werden auf der Pfad-Ebene blockiert, nicht dem Agenten überlassen. Deine
Originale werden daher **referenziert, nicht gecacht**: `source: user`, nie geprunt (§2.2). (Eine
Datei *in* `geocache/` abzulegen ist stattdessen der „Arbeitskopie"-Pfad — in Ordnung, aber diese
Kopie altert dann; das Master als Daten-Root behalten.)

```json
"geodata": { "roots": ["~/gis/projects", "/data/dems"] }
```

### 4.3 Container-Connectoren — PostGIS, SpatiaLite, GeoPackage
Datei-Container brauchen nur einen Pfad; PostGIS braucht einen Connection-String:

```json
"geodata": { "postgis": { "dsn": "postgresql://user@localhost:5432/gis", "schema": "public" } }
```

Alle drei exponieren das gleiche Trio (§3): `geodatasets_list` → `geodataset_describe` →
`geodataset_fetch`. Fetches subsetten in ein Cache-GeoPackage via `GeoDataFrame.from_postgis`
(oder OGR für Datei-Container), der Rest der Pipeline bleibt also unverändert und das Subset
altert wie jeder andere Download.

**Route (PostGIS):** mit GeoPandas `from_postgis` starten (passt zum datei-basierten Flow); den
GDAL/OGR-`PG:`-Treiber später ergänzen, damit `qgis_process` Tabellen direkt liest, wenn der
Export teuer wird. **Standardmäßig read-only** — Chester zieht *aus* der DB.

**Sicherheit (nicht verhandelbar) für die SQL-gestützten Connectoren (PostGIS, SpatiaLite).**
`where`/`bbox` sind LLM-generiert, daher: gebundene **Parameter** (nie SQL string-konkatenieren),
`dataset` auf `geodatasets_list()`-Namen whitelisten, ein parametrisiertes
`ST_MakeEnvelope(..., srid)` für den bbox, und mit einer **read-only-Rolle** verbinden. Das macht
die Datenbank für einen autonomen Agenten sicher.

### 4.4 Deine eigenen STAC-/WFS-Endpunkte
Interne Kataloge/Dienste sind nur weitere Einträge in der STAC-Registry (§3.3) und dem OGC-Tool
(§3.4) — Config, kein Code.

---

## 5. Passung zu Chesters Architektur

- **Capabilities, kein Fork.** `GeoInventoryCapability`, die Container-Connector-Capability und
  die neuen Akquise-Tools fügen sich in `geo_capabilities()` ein — den vorgesehenen
  Erweiterungspunkt. Keine Gateway-Änderungen.
- **Config-getrieben.** Daten-Roots, PostGIS-DSN, Container-Pfade, die STAC-Registry und
  TTL-Defaults leben unter einem einzigen `geodata`-Block in `.chester/chester.json`
  (geschrieben von `setup.py`s `DEFAULT_CONFIG`) — der Schirm über **beiden** Beständen
  (Connectoren *und* Cache), weshalb er den neutralen `geodata`-Namen behält, während die
  Cache-Artefakte `geocache.*` heißen. Quellen und Lifecycle sind Config, nicht Code, wie die
  Modellschicht.
- **Provenienz + Validierung bleiben erstklassig** — der Korrektheits-Loop erstreckt sich auf
  *Inputs*, nicht nur Outputs, und Provenienz ist das, was den Cache wegwerfbar macht.
- **Ablauf reitet auf dem Sync.** Keine neue Maschinerie — `geocache_sync()` löscht abgelaufene
  Einträge selbst; SelmaKits Cron läuft es nur planmäßig für untätige Sitzungen.

## 6. Phasing

1. **Bewusstsein + Ablauf** (bester Wert/Aufwand): `GeoInventoryCapability` über einer einzigen
   `geocache_sync()`-regenerierten `geocache.md` + das `geocache/`-Layout + `/geocache`-Befehl +
   Prompt-Kontext, mit `created_at`/`last_used`/`expires_at` inline und `geocache_sync()`, das
   abgelaufene Einträge löscht (cron-geplant). Stoppt heute das Dateinamen-Raten und das
   unbeschränkte Wachsen.
2. **DEM** (`fetch_dem`): schließt `terrain-analysis` + `building-heights`.
3. **Container-Connectoren + `connect-data`-Skill**: GeoPackage/SpatiaLite (zero-config), dann
   PostGIS (`geodatasets_list` / `describe` / `fetch` + SQL-Sicherheit); der Skill treibt das
   In-place-vs-Arbeitskopie-Onboarding (§4.1).
4. **Multi-Katalog-STAC + OGC-Dienste**: Planetary Computer, deutsche/EU-Daten.
5. **Extras**: volle Provenienz-Sidecars (durable Semantik + Re-Fetch), STAC-Index, Basiskarten,
   LiDAR (§3.5).

Jede Phase liefert unabhängig aus, getestet wie die bestehenden Discovery-Tools (offline
gemockte-I/O-Units, opt-in `--run-network` für Live-Checks).

## TL;DR
Der Unterscheider sind **Daten**, organisiert als **GeoDatasets** in zwei Beständen:
**GeoConnectors** (Live-Quellen — Query-Connectoren holen eins, Container-Connectoren wie
PostGIS/SpatiaLite/GeoPackage exponieren viele) und der **GeoCache** (lokale Datensätze,
heruntergeladen *und* selbst erzeugt). Gib dem Agenten (A) Bewusstsein über den Cache via eines
persistenten, Platte-abgeglichenen Inventars plus Provenienz; (B) **Ablauf**, weil ein Cache
beschränkt bleiben muss — jeder Eintrag altert aus und wird auf Abruf neu geholt oder neu
berechnet; und (C) Tools, um beides zu listen, was er *hat* (`geocache_list`) und was er
*erreichen* kann (`geodatasets_list`). Dann die Connectoren verbreitern (DEM, Container,
Multi-Katalog-STAC, OGC). Alles config-getriebene Capabilities, kein Fork. Mit Bewusstsein +
Ablauf starten — billigste Änderung, größte Wirkung.
