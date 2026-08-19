# Urban-Data-Konzept — Chester als Urban Data Agent

**Status: teils gebaut, teils bewusst gestrichen.** Diese Notiz fragte ursprünglich, was
es bräuchte, damit Chester ein allgemeiner **Urban Data Agent** wird — fähig, die volle
Breite städtischer Daten inkl. der **Echtzeit-/Sensor**- und **3D**-Dimensionen zu
entdecken, holen, integrieren, analysieren und zu visualisieren. Umgesetzt wurden die
**3D-** (Phase D) und **Transit-** (Phase C) Datentypen sowie die **Punktwolken**-Anzeige;
die **Echtzeit-/Sensor**-Achse (Phase A), **Auth** (Phase E) und die **Umwelt-Skills**
(Phase B) wurden **aus dem Scope entfernt** (Entscheidung 2026-07-18) — Chester bleibt ein
statisch-räumlicher Geo-AI-Agent. Die Landschafts-Analyse unten bleibt als Referenz stehen,
der aktuelle Stand ist in §5/§6 markiert.

Der Leitgedanke durchgehend: **den Standard integrieren, nicht die Plattform.** Städte
liefern ihre Daten zunehmend aus zentralen *Urban Data Platforms (UDPs)*, aber eine
konforme UDP exponiert **offene Standards** (OGC, FIWARE, DCAT), keine proprietäre API. So
erreicht derselbe Connector jede Plattform, indem er auf ihre Basis-URL zeigt — genau wie
Chester WFS, STAC und CKAN bereits behandelt. In Deutschland standardisiert BSI TR-03187
(„Sicherheitsanforderungen an Urban Data Plattformen"), wie eine solche Plattform aussieht;
an den Standards auszurichten bedeutet also breite Kompatibilität statt einer
Einmal-Integration.

## 1. Die Urban-Data-Landschaft

| Domäne | Typischer Typ / Standard | In QGIS? | Extra-Library | Chester heute |
|---|---|---|---|---|
| Verwaltung / Flurstücke / Gebäude / POIs | Vektor: GeoPackage, WFS, OGC API Features | ✅ nativ | — | ✅ osm / wfs / fetch_vector |
| Landbedeckung / Bilder / NDVI / **Hitze (LST)** | Raster: GeoTIFF/COG, STAC | ✅ nativ | — | ✅ STAC / perception (LST via Thermalband = Skill) |
| Terrain / Höhe | DEM-Raster | ✅ nativ | — | ✅ fetch_dem / fetch_dgm1 / terrain |
| LiDAR | Punktwolke: LAS/LAZ, COPC/EPT | ✅ (PDAL) | — | ✅ pointcloud-Tools + COPC-Anzeige (QGIS-3D & Web) |
| Bevölkerung / sozio-ökonomisch | Tabellarisch + Regionsschlüssel | ✅ (Join) | — | ✅ stats (Wikidata/Eurostat/World Bank) |
| Erreichbarkeit / Netzwerke | Liniennetz + Routing | ✅ (Netzwerkanalyse) | — | ✅ qgis_service_area (Isochronen) |
| **3D-Stadtmodelle** | CityGML / CityJSON (LoD1/2) | ◑ schwach | **cjio** + mapbox_earcut/trimesh | ✅ **gebaut** (fetch_cityjson / render_buildings_3d / qgis_show_3d) |
| **Public Transit** | **GTFS** (statisch), GTFS-RT | ❌ | **gtfs-kit** | ✅ **gebaut** (gtfs_feeds / fetch_gtfs_stops / fetch_gtfs_routes) |
| **Echtzeit / Sensoren (IoT)** | OGC SensorThings, FIWARE NGSI-LD, MQTT | ❌ | stdlib HTTP | ✗ **out of scope** (entfernt) |
| Multimodales Routing | Transit-Reisezeit | ◑ nur single-mode | r5py / OpenTripPlanner (Java) | ✗ **descoped** (kein Java) |
| Luftqualität / Lärm / Solar | Sensorpunkte / 3D-abgeleitet | teils QGIS | — | ✗ **out of scope** (mit Phase A/B entfernt) |

Lesart der Tabelle: **der statische, räumliche Teil ist erledigt**, und **3D + Transit**
sind hinzugekommen. Bewusst **nicht** umgesetzt: die **Echtzeit-/Sensor-Achse** und die
darauf aufbauenden Umwelt-Skills — die Live-/Zeit-Dimension ist für Chester out of scope.

## 2. Datentypen im Detail (Analyse — historisch)

### 2.1 Echtzeit-/Sensor-Zeitreihen — **entfernt (Phase A)**
Die „lebende Stadt" (Verkehr, Luftqualität, Lärm, Parken, Wetter …), geliefert von UDPs über
**OGC SensorThings API** und **FIWARE/ETSI NGSI-LD**, manchmal **MQTT**. Kein neuer
Library-Bedarf (REST/JSON, stdlib). Die Machbarkeit war bestätigt (öffentliche
SensorThings-Endpunkte liefern Tausende Things credential-frei) — **aber der Datentyp wurde
aus dem Scope genommen** (kein durchgesetzter Sensor-Standard; Live-Achse nicht Chesters Ziel).

### 2.2 3D-Stadtmodelle — **gebaut (Phase D)**
CityGML / CityJSON bei LoD1–2, von den Bundesländern breit veröffentlicht. Umgesetzt: Chester
schreibt CityJSON selbst aus dem LoD2-CityGML (reines Python, **kein Java**), rendert 2.5D
(MapLibre) und echte Dächer (three.js) und zeigt in QGIS-3D. Details:
[`features.md`](./features.md) („Gebäudehöhen & 3D-Gebäudemodelle"). Roof→Solar-Processing
wurde **descoped**.

### 2.3 Public Transit (GTFS) — **gebaut (Phase C)**
Fahrpläne, Haltestellen, Linien, Takte. Umgesetzt über **gtfs-kit**: ein GTFS-Connector
(`fetch_gtfs_stops` / `fetch_gtfs_routes` mit Service-Qualität) über die offenen DACH-Feeds
(gtfs.de / geOps; AT gated). Die **multimodalen Isochronen** (r5py/OTP) wurden **descoped**
(brauchen Java, gegen Chesters No-Java-Prinzip).

### 2.4 Umwelt-Exposition (Luft / Lärm / Hitze) — **entfernt (Phase B)**
Wären Analyse-Skills über bereits abgedeckte Datentypen gewesen, hingen aber am Sensor-Connector
(2.1). Mit Phase A entfernt. Ein `urban-heat`-Skill (LST via STAC-Thermalband) bliebe mit
heutigen Tools ohne neuen Connector möglich, ist aber nicht geplant.

## 3. Querschnitts-Fähigkeiten (Achsen, historisch)

Diese waren für die (nun entfernten) Live-Datentypen gedacht:

1. **Zeitreihe als erstklassiger Datentyp** — (Ort × Zeit × Wert). Mit Phase A entfallen.
2. **Standard-Web-API-Clients** (SensorThings, NGSI-LD, OGC API EDR) — mit Phase A entfallen.
3. **Authentifizierung (OAuth2/OIDC)** — **entfernt (Phase E)**: Chester bleibt credential-frei;
   ein Token-Pfad wäre nur für geschützte Tenants nötig gewesen.
4. **Interpolation & Dichteflächen** — überwiegend in QGIS vorhanden; ohne Sensor-Input moot.
5. **Temporale / proportionale Visualisierung** — `render_map` deckt Choroplethen/
   Proportionalsymbole bereits ab.

## 4. QGIS vs. neue Library — die Zusammenfassung

- **Schon in QGIS (`qgis_process` wiederverwenden):** Vektor, Raster, DEM/Terrain, Punktwolke
  (PDAL), Netzwerkanalyse/Isochronen, Interpolation, Zonalstatistik, Choroplethe.
- **Neue Python-Library nötig (umgesetzt):** CityJSON (**cjio** + mapbox_earcut/trimesh),
  GTFS (**gtfs-kit**).
- **Nicht umgesetzt (out of scope):** multimodales Routing (r5py/OTP — Java), MQTT-Streams
  (paho-mqtt), OAuth (authlib), die SensorThings/NGSI-Clients.

## 5. Phasen — tatsächlicher Stand

Die Phasen behielten stabile Buchstaben als Labels; die ursprüngliche Reihenfolge war
**D → C → E → A → B**. Umgesetzt/gestrichen:

1. **Phase D — 3D (CityJSON)** ✅ **gebaut.** Per cjio/eigenem Writer; siehe
   [`features.md`](./features.md). Roof→Solar descoped.
2. **Phase C — Transit (GTFS)** ✅ **gebaut** (Stops + Linien für DE/CH). Multimodale
   Isochronen descoped (Java).
3. **Phase E — Auth (OAuth2/OIDC)** ✗ **entfernt** (credential-frei bleibt der Default).
4. **Phase A — Sensor/Echtzeit** ✗ **entfernt** (kein durchgesetzter Standard; Live-Achse
   out of scope). Zur Datenlage-Recherche: es *gäbe* credential-freie Quellen (OGC-SensorThings:
   Fraunhofer-Luftqualität, Urban Data Platform Hamburg; plus Open-Meteo, Bright Sky,
   Sensor.Community) — bei Bedarf reaktivierbar.
5. **Phase B — Interpolation + Umwelt-Skills** ✗ **entfernt** (hing an Phase A).

Der `platforms`-Config-Block (Endpunkte), inert wenn abwesend — wie `geodata.postgis` — bleibt
als optionaler Ansatzpunkt für ein späteres Wieder-Aufgreifen dokumentiert.

## 6. Fazit

Chester deckt den **statisch-räumlichen** Teil vollständig über QGIS + die bestehenden Wrapper
ab und hat mit **3D-Stadtmodellen** und **Transit (GTFS)** zwei echte neue Datentypen dazubekommen
— beide Java-frei, standard-basiert, DACH-weit. Die **Echtzeit-/Sensor-Achse** samt Auth und
Umwelt-Skills wurde bewusst **aus dem Scope genommen**: kein Sensor-/Zeitreihen-Standard hat sich
durchgesetzt, und die Live-Dimension ist nicht Chesters Ziel. Der strategische Punkt bleibt für
die umgesetzten Teile gültig: **Chester spricht offene (Geo-)Standards** — eine gegebene
Stadt-Plattform ist dann nur ein weiterer Endpunkt, den es lesen kann.
