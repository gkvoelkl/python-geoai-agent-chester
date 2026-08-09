# Die Chester–QGIS-Bridge

> Begleitdokument zu [`qgis-process.md`](./qgis-process.md):
> jenes deckt die **headless** QGIS-Tool-Oberfläche ab (`qgis_process` als Subprozess).
> Dieses deckt die **live** ab — wie Chester ein *interaktives* QGIS-**Desktop**-Fenster
> öffnet und steuert, damit ein Nutzer die Daten wirklich ansehen und damit arbeiten kann.

## 0. These

Chesters Standard-„Zeig mir" ist eine inline eingebettete Folium/HTML-Karte im Chat. Das
bricht jenseits von ~100–200k Features zusammen: das ganze GeoJSON wird in die Seite
eingebettet, und der Browser muss jede Geometrie parsen und rendern (die OSM-Gebäude einer
Stadt ≈ 500 MB HTML). Ein GIS war für das *Arbeiten mit* so vielen Daten immer das richtige
Werkzeug — und QGIS ist auf der Maschine bereits installiert.

Die Bridge gibt Chester also einen zweiten Ausgabekanal: **die lokale QGIS-Desktop-App**,
live gesteuert. Sie umgeht jede Payload-Grenze (QGIS rendert Millionen Features nativ),
bringt echte Interaktivität (Pan/Zoom, Identify, Attributtabelle, Styling) und — weil QGIS
das Projekt selbst schreibt — endlich ein **valides `.qgz`**, das Chester von Hand nicht
bauen konnte.

Das Designziel, das alles geprägt hat: **null zusätzliche Abhängigkeiten und nichts in QGIS
installiert.** Jedes Stück unten nutzt nur Bibliotheken, die QGIS ohnehin mitbringt
(`QtNetwork`, PyQGIS) oder die Python-Standardbibliothek.

Es ist **naturgemäß nur lokal** — es öffnet ein Fenster auf der Maschine, die das Gateway
betreibt. Für ein Remote-/Hosted-Deployment (Phase 4) bleibt die inline Web-Karte die
Antwort; die Bridge *ergänzt*, sie ersetzt sie nicht.

## 1. Form auf einen Blick

```
Chester (venv, Python 3.13)                       QGIS Desktop (bundled Python 3.11, PyQt6)
─────────────────────────────                     ──────────────────────────────────────────
GeoLiveCapability                                 qgis_startup.py   (der `--code`-Einstieg)
  qgis_show / qgis_show_3d / qgis_show_pointcloud       │ startet
  qgis_screenshot / qgis_save_project                   ▼
        │ ruft                                    LiveBridge  (QtNetwork QTcpServer, :9878)
qgis_live_client.ensure_running() ──── startet ──▶       │ Handler laufen im QGIS-Hauptthread
        │ dann                          QGIS --code       │
        └── stdlib-Socket ─────── zeilenbegr. JSON ───────┘ ──▶ PyQGIS (QgsProject, Canvas…)
```

Zwei Prozesse, ein Socket. Kein dritter Prozess, kein MCP-Server, keine QGIS-Plugin-Installation,
kein PyQGIS in Chesters venv.

## 2. Die vier Module

- **`chester/qgis_bridge.py`** — `LiveBridge`, der Server, der **innerhalb** von QGIS läuft.
  Ein `QtNetwork.QTcpServer` gebunden an `127.0.0.1:9878`. Seine `newConnection`- /
  `readyRead`-Signale feuern im QGIS-**Hauptthread**, sodass jeder Command-Handler dort läuft,
  wo PyQGIS sicher ist — **keine Worker-Threads, kein Polling, kein Marshalling.** Importiert
  nur `qgis.PyQt`, `qgis.core` und stdlib.
- **`chester/qgis_startup.py`** — der `QGIS --code`-Einstiegspunkt. Läuft beim Start innerhalb
  von QGIS, importiert `qgis_bridge` **standalone per Verzeichnis** (nicht über das
  `chester`-Paket, damit es Chesters venv-Deps nie in QGIS' Bundled-Python zieht) und startet
  die Bridge. Der Launcher übergibt das Verzeichnis dieser Datei per Env-Variable
  `CHESTER_BRIDGE_DIR`.
- **`chester/qgis_live_client.py`** — der Chester-seitige Client (stdlib `socket` + `json`).
  Enthält `_call()`, `is_running()`, `launch_qgis()`, das zentrale **`ensure_running()`**
  (reuse-or-launch, §4) und **`to_loadable()`** (§6.1).
- **`chester/capabilities/qgis_live.py`** — `GeoLiveCapability`, die Agent-Tool-Oberfläche
  (`qgis_show` / `qgis_show_3d` / `qgis_show_pointcloud` / `qgis_screenshot` /
  `qgis_save_project`) mit einer **Ask-first**-Instruktion. Registriert in
  `agent_build.geo_capabilities()`.

Der `/qgis`-Slash-Befehl (in `agent_build.register_geo_commands`) teilt sich für die zuletzt
gerenderte Karte den *gleichen* Client-Pfad — so können Befehl und Tool nicht auseinanderdriften.

## 3. Warum diese Form (die Entscheidungen)

Jede Wahl unten war eine bewusste Abkehr von einer schwereren Option.

- **QGIS Desktop, nicht QGIS Server.** QGIS Server ist eine echte Deployment-Stufe (FCGI
  hinter nginx, ein `.qgs`-Projekt pro Karte, eine separate Installation, auf macOS nicht mal
  gebündelt) und kollidiert mit Chesters „schlank & lokal"-Haltung und dem ephemeren GeoCache.
  Die Desktop-App ist schon da; wir reden nur mit ihr.
- **Kein MCP.** Die Community-QGIS-MCP-Server (jjsantos01, nkarasiak) sind alle dreiteilig: ein
  QGIS-Plugin (Socket) + ein *separater* FastMCP-Prozess + der Client. MCP lohnt sich nur, wenn
  **fremde** Clients (Claude Desktop, andere Agenten) die Capability wiederverwenden müssen.
  **Chester ist der einzige Client**, also ist MCP reiner Overhead — eine Übersetzungsschicht
  zwischen dem Agenten und einem Socket, den wir bereits besitzen. Eine **direkte**
  JSON-über-Socket-Verbindung ist schlanker. (MCP kann später Chester-seitig aufgesetzt werden,
  ohne die Bridge anzufassen — siehe §8.)
- **`QGIS --code`, kein installiertes Plugin.** Ein QGIS-Plugin startet automatisch, wenn
  aktiviert, braucht aber die Kopieren-ins-Profil- + Plugin-Manager-aktivieren-Zeremonie. Da
  Chester QGIS selbst *startet*, startet `QGIS --code start.py` dieselbe `LiveBridge` beim
  Start ohne Installation. Die Plugin-Paketierung ist daher **optional** (siehe §7 für den
  einen Fall, in dem sie noch hilft).
- **`QtNetwork.QTcpServer`, keine rohen Sockets + Thread.** Qts Server integriert sich in QGIS'
  bestehende Event-Loop und liefert Verbindungen als Signale im Hauptthread — der idiomatische,
  thread-sichere Weg, PyQGIS anzufassen. (Die Community-Plugins nutzen einen rohen `socket`,
  gepollt per `QTimer`; `QTcpServer` ist dieselbe Idee, sauberer.)
- **Fenster vs. headless ist ein Launch-Flag.** `resolve_qgis_env()` setzt
  `QT_QPA_PLATFORM=offscreen` für headless `qgis_process`. Der Bridge-Launcher **entfernt** es,
  damit ein echtes Fenster erscheint — was auch Screenshots ermöglicht (offscreen hat kein
  Paint-Device). Headless `--code` ist weiterhin nützlich für reine Datei-Jobs (z. B. ein `.qgz`
  ohne Fenster bauen).

## 4. Reuse: `ensure_running()`

Die Bridge darf nie ein zweites Fenster öffnen, wenn Chester bereits ein QGIS oben hat.
`ensure_running()` ist **ping-first**:

1. `is_running()` — ein 2-s-`ping`. Antwortet es → `"reused"` zurückgeben. Fertig, kein Launch.
2. Sonst `launch_qgis()` — ein *Fenster*-`QGIS --code qgis_startup.py` (detached, saubere
   On-Screen-Env), dann `ping` bis `LAUNCH_TIMEOUT` (90 s) pollen, bis die Bridge antwortet →
   `"launched"` zurückgeben.

Ein zweites `qgis_show` (oder `/qgis`) verwendet also die laufende Sitzung wieder und fügt nur
Ebenen hinzu. Sowohl `qgis_show` als auch `/qgis` gehen durch diesen einen Pfad.

## 5. Protokoll-Referenz

Zeilenbegrenztes JSON über TCP `127.0.0.1:9878`. Eine Request-Zeile → eine Response-Zeile.
Nur localhost, keine Auth (ein Spike-taugliches Transport, das geblieben ist).

Request: `{"type": "<cmd>", "params": {...}}\n`
Response: `{"status": "success", "result": {...}}\n`
       oder `{"status": "error", "message": "...", "trace": "..."}\n`

| Befehl | Params | Ergebnis | PyQGIS |
|---|---|---|---|
| `ping` | — | `{qgis_version, project, layer_count}` | `QgsProject.instance()` |
| `add_layers` | `paths: [str]` | `{added: [name], failed: [path]}` | fügt (einmalig) eine OSM-Basiskarte hinzu, dann jeden Pfad via `querySublayers(ResolveGeometryType)`, sodass eine Datei mit gemischter Geometrie eine Ebene pro Typ wird; Zoom + Refresh |
| `show_3d` | `extrusion_height?` | `{styled_3d: [name], view_opened}` | setzt einen Z-geklemmten `QgsVectorLayer3DRenderer` auf die Polygon-Ebenen (CityJSON→MultiPolygonZ) und öffnet eine 3D-Kartenansicht |
| `show_pointcloud` | `paths: [str]` | `{added, failed, view_opened}` | lädt COPC/EPT als `QgsPointCloudLayer` und öffnet eine 3D-Ansicht (kein `pdal`-Provider → rohes LAZ vorher zu COPC konvertieren) |
| `zoom_full` | — | `{ok: true}` | `mapCanvas().zoomToFullExtent()` |
| `save_project` | `path: str` | `{path, ok}` | `QgsProject.write(path)` → valides `.qgz` |
| `screenshot` | `path: str` | `{path, exists}` | `mapCanvas().saveAsImage(path)` — **braucht ein sichtbares Fenster** |

## 6. Tool- & Befehls-Oberfläche

- **`qgis_show(layers)`** — der agentseitige Einstieg. Löst die Pfade auf (in den GeoCache via
  `resolve_path`), `ensure_running()`, `add_layers`, `zoom_full`. Die Capability-Instructions
  verlangen, dass der Agent es **anbietet und zuerst fragt**, bevor ein Fenster geöffnet wird;
  es wird nie autonom aufgerufen.
- **`qgis_show_3d(layers)`** — öffnet eine QGIS-**3D**-Kartenansicht: eine CityJSON wird zuerst
  in ein MultiPolygonZ-GeoPackage konvertiert (`show_3d`-Befehl), Ebenen mit Z werden as-is 3D
  gerendert. Zero-Plugin. Ebenfalls ask-first.
- **`qgis_show_pointcloud(layers)`** — öffnet eine Punktwolke (COPC/EPT) in einer 3D-Ansicht
  (`show_pointcloud`-Befehl). Ask-first.
- **`qgis_screenshot(path)`** — Canvas → PNG (erfordert vorher `qgis_show` / ein Fenster). Gibt
  den Ausgabepfad zurück.
- **`qgis_save_project(path)`** — schreibt ein `.qgz` in den Cache.
- **`/qgis`** (Slash-Befehl) — derselbe Bridge-Pfad für die *zuletzt gerenderte Karte* (gelesen
  aus `geocache/last_map.json`, das `render_map` stempelt). Nutzer-aufgerufen, daher kein
  Ask-first. Verwendet ein laufendes QGIS wieder.

Das ist das **einzige Mechanismus** für die Ansicht in QGIS. Die früheren Schritte der Roadmap
(A: ein `/qgis`, das `open -a` nutzte; B: ein separates `open_in_qgis`-Tool) wurden in diesen
Bridge-Pfad gefaltet; der `open -a`-Launcher wurde gelöscht.

### 6.1 GeoJSON → GeoPackage auf dem Weg hinein (`to_loadable`)

`qgis_show` und `/qgis` schleusen jede Ebene vor `add_layers` durch
`qgis_live_client.to_loadable()`. Es konvertiert ein **GeoJSON** in ein gecachtes
**GeoPackage** (`.gpkg` neben der Quelle, wiederverwendet solange neuer als sie) und reicht
alles andere unverändert durch. Zwei Gründe, beide an einer echten 44k-Feature-OSM-Gebäudeebene
gelernt (ein **488 MB** GeoJSON):

- **Geschwindigkeit.** Der OGR-GeoJSON-Treiber liest die ganze Datei bei *jedem* Öffnen neu, und
  `add_layers` öffnet sie mehrfach — ~**60 s**, blockiert den QGIS-Hauptthread. Das GPKG (hier
  35 MB, indiziert) lädt in ~**0,1 s**; die einmalige ~30-s-Konvertierung läuft Chester-seitig
  (venv, abseits des QGIS-Threads) und wird dann gecacht.
- **Korrektheit.** OSM-Gebäude sind *gemischte Geometrie* (manche Nodes → Punkte, die meisten
  Ways → Polygone). Ein gemischtes GeoJSON lädt in QGIS als *ein* Geometrietyp, sodass die
  Polygone still verschwanden und nur Punkte erschienen. Die Konvertierung promotet single→multi
  und splittet nach Geometrietyp in separate GPKG-Ebenen, sodass **jede** Geometrie rendert. Sie
  wirft auch case-doppelte Spalten weg (OSM `FIXME`/`fixme`), die die case-insensitiven
  GeoPackage-Spaltennamen sonst ablehnen würden.

`add_layers` fügt zusätzlich einmalig eine **OSM-XYZ-Basiskarte** (unterste Ebene) für Kontext
hinzu. Jeder Konvertierungsfehler fällt auf das Laden der Originaldatei zurück.

## 7. Randbedingungen & Vorbehalte

- **Nur lokal.** Öffnet ein Fenster auf der Gateway-Maschine; nutzlos auf einem headless Server.
  Remote-/Hosted-Deployments nutzen stattdessen die inline Web-Karte.
- **Screenshot braucht ein sichtbares Fenster.** `canvas.saveAsImage()` hat offscreen kein
  Paint-Device (`QPainter engine == 0`). Ein headless Screenshot bräuchte einen expliziten
  `QgsMapRendererParallelJob` → `QImage.save()`.
- **Kann sich nicht an ein nutzer-geöffnetes QGIS anhängen.** `--code` läuft nur beim *Start*,
  hat ein Nutzer QGIS selbst geöffnet (keine Bridge), scheitert `ping` und Chester würde eine
  **zweite** Instanz starten. Das Anhängen an ein bereits laufendes QGIS ist der eine Fall, in
  dem ein installiertes, immer-lauschendes Plugin (dieselbe `LiveBridge`) hilft.
- **Kein Transport-Hardening.** Einzelner fester Port, localhost, keine Auth, Newline-Framing.
  Für einen lokalen Einzelnutzer-Agenten in Ordnung; vor breiterer Exposition überdenken.

## 8. Testen

- **Client ohne QGIS:** der Client spricht zeilenbegrenztes JSON über einen localhost-Socket,
  und `ensure_running` prüft per Ping zuerst — der Reuse-/Launch-Zweig verhält sich also sauber,
  wenn keine Bridge läuft. `tests/test_qgis_live.py` deckt das Tool-Wiring ab.
- **Echtes QGIS, headless:** `QGIS --code` mit `QT_QPA_PLATFORM=offscreen` starten, um die echte
  `LiveBridge` und die PyQGIS-Handler ohne Fenster zu fahren — so wurden `add_layers` /
  `save_project` (valides `.qgz`) verifiziert.
- **Echtes QGIS, Fenster:** derselbe Start ohne offscreen belegt den Screenshot- und den
  sichtbaren Ansichtspfad.

## 9. Erweiterungspunkte

- **Styling beim Anzeigen.** `add_layers` wendet derzeit die QGIS-Standard-Symbologie an. Eine
  `save_project`/`add_layers`-Variante könnte einen abgestuften Renderer passend zur `column`
  der letzten Karte anwenden (die Choroplethe, die die Web-Karte bereits berechnet).
- **Headless Screenshots** via `QgsMapRendererParallelJob` (siehe §7).
- **MCP, falls je nötig.** Soll ein fremder MCP-Client die QGIS-Steuerung wiederverwenden, den
  *gleichen* Socket-Client Chester-seitig in einen FastMCP-Server wickeln (oder einen
  MCP-Endpunkt exponieren) — keine Änderung an der Bridge. Nur für Interop lohnend.
- **An ein laufendes QGIS anhängen** über eine optionale, installierte Plugin-Form von
  `LiveBridge` (immer lauschend), für den §7-Randfall — derzeit nicht ausgeliefert.
