# Chesters Fähigkeiten — die Feature-Tour

> Was Chester an Geodaten holen, verarbeiten und ausgeben kann, mit den konkreten
> Tool-Aufrufen. Bedienung/Betrieb (Slash-Befehle, Tests, Aufbau) steht in
> [`usage.md`](./usage.md); die Design-Hintergründe in den jeweils verlinkten Konzept-Docs.

## Statistische Daten (amtliche Statistik → Choroplethe)

Chester kann amtliche Statistik holen und sie an Verwaltungsgeometrien joinen, um
thematische (Choroplethen-)Karten zu erstellen, über drei Connectoren
(`stats_sources`, `stats_search`, `stats_table`). Alle Quellen sind
**credential-frei** — kein Konto, keine Config:

| Quelle | Abdeckung | Join-Schlüssel |
|---|---|---|
| `wikidata` | Deutschland, pro Gemeinde/Kreis: Bevölkerung (P1082) + Fläche (P2046), aus amtlicher Statistik | `ags` (Amtlicher Gemeindeschlüssel) |
| `eurostat` | EU-weit, NUTS 0–3 (gröber) | `geo` (NUTS-Code) |
| `worldbank` | Global, pro Land: ~1500 World Development Indicators (Bevölkerung, BIP, Gesundheit, …) | `iso3` (ISO-3166 alpha-3) |

Die deutschen GENESIS-2020-Quellen (Destatis Regionalstatistik / GENESIS-Online /
Zensus 2022) wurden **entfernt** — ihre REST-API verlangte ein Konto pro Rechner, und
einen Kern-Workflow hinter Credentials zu gaten erwies sich als unpraktisch. Für eine
deutsche Gemeinde-Choroplethe Wikidata in zwei Schritten nutzen:

```
stats_search("wikidata", "Landkreis Regensburg")   → Code "09375"
stats_table("wikidata", "09375", "pop.csv")        → CSV: ags, name, population, area_km2
```

dann die `ags`-Spalte an eine Gemeindegrenzen-Ebene joinen und symbolisieren. Die
passende Grenz-**Geometrie** liefert `fetch_boundaries` aus den offenen
BKG-Verwaltungsgebieten (`boundaries_levels` listet die Ebenen: `STA`/`LAN`/`KRS`/
`GEM` nach `ags`, `NUTS1..3` nach `NUTS_CODE`) — der fehlende Baustein, der die
Statistik-Tabelle direkt auf Polygone bringt. Für deutsche Daten, die Wikidata nicht
führt, auf eine Open-Data-CSV via `geodata_search` → `fetch_vector` zurückgreifen.
Niemals einen Wert fabrizieren, der nicht belegbar ist.

## Gebäudehöhen & 3D-Gebäudemodelle (LoD2 / CityJSON)

Für **echte Gebäudehöhen** nutzt Chester die offenen **LoD2**-Gebäudemodelle der
Bundesländer — pro Gebäude eine laser­gemessene Höhe (`bldg:measuredHeight`), besser
als eine `building:levels`-Schätzung oder eine DSM−DTM-Differenz und weit genauer als
das globale 30-m-GLO-30 (`fetch_dem`):

- `lod2_sources()` — welche Bundesländer verdrahtet sind (aktuell **Bayern, NRW,
  Brandenburg, Mecklenburg-Vorpommern**) vs. dokumentiert.
- `fetch_lod2(bbox, output, street?)` — Gebäude mit `measured_height` als GeoPackage
  (metrisches CRS); Bundesland automatisch erkannt.
- `fetch_dgm1(bbox, output)` — das **1-m-DGM**-Geschwister von `fetch_dem` (offen,
  metrisch) für feines Terrain / die DTM-Hälfte einer DSM−DTM-Rechnung.

Für **3D-Stadtmodelle** wandelt Chester das LoD2-CityGML selbst in **CityJSON** um
(reines Python, **kein Java** — es gibt keinen Java-freien Konverter, und QGIS liest
CityJSON nicht nativ), dann anzeigen:

```
fetch_cityjson(bbox, "buildings.city.json")            → CityJSON, auf die bbox geclippt
render_buildings_3d("buildings.city.json", "b.html")   → self-contained 3D-HTML (three.js,
                                                          echte LoD2-Dächer; style="blocks"
                                                          für leichte MapLibre-2.5D-Blöcke)
qgis_show_3d("buildings.city.json")                    → Live-QGIS-3D-Ansicht (zero-plugin,
                                                          über MultiPolygonZ)
```

`cityjson_to_geopackage` liefert dieselbe 3D-Geometrie als MultiPolygonZ-GeoPackage
für weitere QGIS-Arbeit.

Ein optionales `pointcloud=` bei `render_buildings_3d` blendet eine **LiDAR-Punktwolke**
(LAS/LAZ/COPC) in dieselbe three.js-Szene ein (dezimiert, nach LAS-Klasse eingefärbt, auf
die Gebäude-CRS reprojiziert und deckungsgleich zentriert) — oder Punkte allein.

## ÖPNV-Fahrpläne (GTFS)

Für den öffentlichen Verkehr — fahrplan-bewusst, was QGIS' Single-Mode-Netzanalyse nicht
kann — liest Chester offene **GTFS**-Feeds und macht daraus Geodaten mit Service-Qualität:

- `gtfs_feeds()` — die Feeds: 🇩🇪 gtfs.de (`de_fv`/`de_rv`/`de_nv`/`de_full`, credential-frei)
  · 🇨🇭 geOps (`ch_rail`/`ch_bus`/`ch_full`) · 🇦🇹 gated (manuell laden, lokalen Pfad übergeben).
- `fetch_gtfs_stops(feed, out, bbox?)` — Haltestellen-Punkte mit Abfahrten/Tag, Linienzahl
  und mittlerem/min/max Takt + Betriebszeitspanne (bbox bei den nationalen Feeds nötig).
- `fetch_gtfs_routes(feed, out, bbox?)` — Linien mit Service-Qualität je Linie.

## WMS-Dienste anzeigen (Bilder, keine Daten)

Amtliche Kartendienste (Hintergrundkarten, Flurkarten, Bebauungspläne …) kommen oft als
**OGC WMS** — der Dienst liefert **gerenderte Kartenbilder**, keine analysierbaren Daten.
Chester kann sie anzeigen und ausschneiden:

- `wms_capabilities(url)` — die Layer eines WMS auflisten (Titel, bbox, Formate).
- `render_map(wms_url=…, wms_layer=…)` — den Dienst live in die Web-Karte einblenden
  (über eigenen Ebenen oder allein; zentriert dann auf die bbox des Layers).
- `qgis_show_wms(url, layer)` — den Dienst als nativen WMS-Layer in QGIS Desktop
  streamen (Kacheln laden bei Pan/Zoom nach).
- `fetch_wms_map(url, layer, bbox, out.tif)` — einen bbox-Ausschnitt als
  **georeferenziertes GeoTIFF** in den Cache holen (z. B. als Kartenhintergrund).

Pixel eines WMS sind Farben — nie analysieren (keine Zonalstatistik, keine
Klassifikation); für Features `wfs_features`, für Messwerte-Raster STAC/`fetch_dem`.

## LiDAR-Punktwolken

- `pointcloud_search(bbox)` / `fetch_pointcloud(bbox, tile_index)` — offene Punktwolken
  (OpenTopography) finden/holen.
- `pointcloud_to_copc(input)` — LAS/LAZ → **COPC** (nötig für die Anzeige, da dieses QGIS
  COPC/EPT lädt, nicht rohes LAZ).
- `qgis_show_pointcloud(copc)` — 3D-Ansicht in QGIS Desktop; oder im Web via
  `render_buildings_3d(pointcloud=…)` (siehe oben).

## Skills

Wiederverwendbare, versionierte Workflows in [`skills/`](../skills) — der Agent wählt
sie automatisch:

- **building-heights** — echte Gebäudehöhen (offenes LoD2, gemessen) über einem
  Schwellenwert + betroffene Straßen; DSM−DTM nur als Fallback für eigene Raster
- **flood-mapping** — Offenwasser-Kartierung aus Sentinel-2 (STAC → NDWI)
- **terrain-analysis** — Slope / Aspect / Hillshade / Ruggedness aus einem DTM
- **walkability** — Reisezeit-**Isochronen** (Erreichbarkeit) im Straßennetz: was in
  *n* Minuten zu Fuß/Rad/Auto erreichbar ist (Netzwerk-Service-Area → konkave Hülle),
  kein Luftlinien-Puffer
- **lidar-ground** — Bodenpunkt-Klassifikation → DTM (QGIS-PDAL-Provider)
- **find-official-data** — wenn eine Ebene *nicht in OSM* ist (z. B. eine
  Stadtbezirksgrenze), sie in einem Open-Data-Katalog (CKAN) oder WFS finden, holen
  und nutzen (z. B. Features in einem Bezirk zählen) — siehe
  [`geodata-search.md`](./geodata-search.md)
- **review-result** — ein Ergebnis vor dem Abschluss visuell validieren: mit
  `inspect_map` einen Snapshot rendern und ihn *ansehen*, um CRS-/Platzierungs-/
  Abdeckungsfehler zu fangen, die Zahlen übersehen; ein reines Text-Hauptmodell leitet
  den Snapshot an das konfigurierte Fallback-Vision-Modell (`model.vision_model`) —
  siehe [`visual-validation.md`](./visual-validation.md)
- **connect-data** — deine eigenen GeoPackage / SpatiaLite / PostGIS einbinden (Ebenen
  auflisten, an Ort und Stelle referenzieren oder importieren) — verdrahtet über die
  Container-Connector-Tools (siehe [`geodata-concept.md`](./geodata-concept.md))

Eine end-to-end ausprobieren:

```bash
uv run python samples/make_building_sample.py
cp samples/building_heights/* .chester/workspace/
./start.sh   # dann im Dashboard fragen:
# "Finde alle Gebäude höher als 15 m im Workspace und liste die betroffenen Straßen."
```
