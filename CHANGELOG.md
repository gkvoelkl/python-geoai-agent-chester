# Changelog

Alle nennenswerten Änderungen an Chester. Format nach
[Keep a Changelog](https://keepachangelog.com/de/1.1.0/), Versionierung nach
[Semantic Versioning](https://semver.org/lang/de/).

Chester ist ein **Forschungsvehikel**, kein Produkt — „experimentell" ist kein
Übergangszustand. Schnittstellen dürfen sich zwischen Vorabversionen ändern.

## [0.1.3] — 2026-08-19

Ein Wartungslauf mit einem roten Faden: **einem Lauf beim Arbeiten zusehen können.**
Die Bench zeigt jetzt eine getaktete Zeitleiste statt zweier Halbbilder, jeder Lauf
hinterlässt ein Protokoll, und die drei Stellen, an denen ein Lauf bisher spurlos
verschwinden konnte, sind geschlossen.

### Hinzugefügt

- **Die Laufansicht der Bench** (`benchlive.py`) — **eine** Zeitleiste statt zweier
  Halbbilder (Textprotokoll live, aber ohne Struktur · SelmaKit-Transkript
  strukturiert, aber erst nach dem Turn). Tool-Aufruf und sein Ergebnis stehen in
  *einer* Zeile, ungekürzt hinter dem Aufklapper, jede Zeile mit Uhrzeit und Abstand
  zur vorigen; eine Tool-Zeile bekommt beim Ergebnis ihre Laufzeit. Auch für
  **vergangene** Läufe. Wiederholte Instruktionsblöcke werden zu einer Zeile gefaltet,
  die nennt, welcher Abschnitt sich geändert hat — im gemessenen Lauf wurden 212
  Zeilen so zu 80.
- **Ein Protokoll je Lauf** unter `.chester/evals/runs/` — das zeitgestempelte
  Protokoll plus eine Kopie des Session-Traces, verlinkt aus der Historie. Die Kopie
  ist der Punkt: SelmaKit schreibt den Trace je Session-*Schlüssel*, der nächste Lauf
  desselben Tests überschreibt ihn. Ohne Kopie lebte die Aufzeichnung eines Laufs
  genau bis zum nächsten.
- **`GeoSkillGuideCapability`** — die Auswahlregel für Skills, die pydantic-ai 0.1.26
  fallen ließ. Dessen Hinweis „die Werkzeuge einer Capability bleiben verborgen, bis
  sie geladen ist" stimmt allgemein und ist für Chester falsch: seine Skills tragen
  *keine* Werkzeuge. Ein Modell, dem man sagt, ihm fehlten Werkzeuge, die ihm nicht
  fehlen, hat keinen Grund zu laden. Gemessen vorher: **zwei** `load_capability`-Aufrufe
  über 65 Sessions, beide namentlich verlangt — kein Skill hat sich je selbst gewählt.
- **„Welches Feature meine ich?"** — `vector_info(path, values_of="name")` listet die
  Werte einer Spalte in der Reihenfolge der Ebene (`geofacts.column_values`, ohne
  Geometrien gelesen, also auch auf 40.000 OSM-Objekten billig). Dieselbe Frage wurde
  in einem Benchmark-Lauf fünfmal als PyQGIS-Schleife geschrieben, weil kein Werkzeug
  sie beantwortete. Fehlt die Spalte, kommt die Liste der vorhandenen zurück — eine
  fehlende Spalte ist eine Frage, keine Ausnahme.
- **Gate-Stufe V1b: hält die Ebene die Fläche, die sie zu halten behauptet?**
  (`gate._area_identity_problems`) Eine Ebene mit *einem* Objekt, dessen `name` kein
  Wort mit dem Dateinamen teilt, löst einen Retry aus. Anlass: ein Lauf zählte
  Haltestellen in `innenstadt_boundary.gpkg`, das die UNESCO-Relation „Altstadt von
  Regensburg mit Stadtamhof" enthielt — strukturell makellos, inhaltlich eine andere
  Frage. Referenzfrei, verglichen werden die zwei Aussagen der Datei über sich selbst.
  Gemessen an 10 echten Ebenen: 2 Befunde, beides derselbe defekte Umriss, keine
  Fehlalarme.
- **`chester/visioncaps.py`** — die Frage, ob ein Modell überhaupt ein Bild
  entgegennehmen kann, *bevor* eines angehängt wird (siehe „Behoben").
- **`qgis_run_python` führt statt nur auszuführen**: der Namensraum enthält jetzt jede
  `Qgs*`-Klasse wie die QGIS-Python-Konsole, und ein `NameError`/`ImportError` bringt
  einen `hint` mit den passenden Werkzeugen zurück. Die belegte Gefahr ist, dass der
  Notausgang zum ersten Griff wird: in einem Lauf 15 von 24 Aufrufen, davon 5 an
  halluzinierten APIs gescheitert, wo benannte Werkzeuge je einen Aufruf gebraucht
  hätten.
- **Ein Strukturtest über die Capability-Menge** — ein `Gateway.from_config` ohne
  `capabilities=` stellt stillschweigend SelmaKits Standardsatz wieder her und gäbe
  einem Aufrufer einen *anderen* Agenten als dem Rest.

### Geändert

- **SelmaKit 0.1.27** (von 0.1.26).
- **`CronCapability` wird aus SelmaKits Standardsatz gefiltert** — über den dafür
  vorgesehenen `capabilities=`-Haken, kein Fork. Kein Geo-Lauf hat je einen Job
  geplant, und Chesters eigene Aufräumläufe liegen auf einem Daemon-Thread. Gemessen:
  **211 Token**, 0,7 % des Prompts — wer hier mehr erwartet, prüfe erst, wo die Token
  wirklich liegen (63 % sind Werkzeugschemata).
- **Die Instruktionen sagen jetzt, wo die amtlichen Grenzen aufhören.** Das BKG endet
  bei der Gemeinde; ein *Stadtbezirk* oder *Ortsteil* liegt darunter und ist schlicht
  nicht im Datensatz. Der Weg dorthin führt über `geodata_search` und das Portal der
  Stadt — und **nie** über ein ähnlich klingendes OSM-Polygon: auf „Innenstadt"
  antwortet OSM mit dem Welterbe-Umriss, auf „Zentrum" mit einem Punkt.
- **Eurostat wird über httpx geholt, nicht über `urllib`.** urllib prüft TLS gegen den
  Systemspeicher, der auf dieser Maschine keinen Aussteller für `ec.europa.eu` führt —
  jeder Eurostat-Aufruf starb an `CERTIFICATE_VERIFY_FAILED`, während Wikidata und
  Weltbank durchgingen. Die Quelle sah dadurch selektiv kaputt aus statt falsch
  konfiguriert. httpx bringt certifi mit.
- Schreibweise durchgehend **„Geo-AI"** statt „GeoAI".

### Behoben

- **Der Fallback für ein blindes Hauptmodell war unerreichbar — für genau die Modelle,
  für die er gebaut wurde.** `inspect_map` hängte seinen Schnappschuss ans Hauptmodell
  und verließ sich darauf, dass ein Textmodell das *sagt* und mit
  `via_vision_model=True` nachfragt. Ollama lehnt aber die **Anfrage** mit HTTP 400 ab,
  bevor das Modell ein Token liest; die Exception reißt den Event-Stream ab, und weil
  SelmaKit eine Session nur bei vollständigem Ergebnis schreibt, bleibt vom Lauf
  **nichts** übrig. Gemessen an `walk-isochrone-hauptbahnhof`: 634 s korrekte
  Geoverarbeitung, danach keine Spur und nichts zu bewerten. Die Entscheidung fällt
  jetzt vor dem Anhängen des Bildes. Die Erkennung ist bewusst zaghaft — „sieht nichts"
  nur bei ausdrücklicher Auskunft, sonst bleibt alles wie bisher: falsch in diese
  Richtung kostet einen unnötigen Sprung zum Fallback-Modell, falsch in die andere den
  Lauf. Hintergrund in [`doc/visual-validation.md`](./doc/visual-validation.md) §7.
- **Ein abgestürzter Lauf war nicht bewertbar.** `read_trace` nimmt jetzt das
  gestreamte Protokoll als zweite Quelle und spricht den Abbruch *als* Antwort aus —
  ein Leerstring läse sich für den Judge wie „das Modell hat nichts gesagt", genau die
  Verwechslung, gegen die die Wächter aus 0.1.2 gebaut wurden.
- `chester.__version__` stand seit 0.1.0 still, während `pyproject.toml` weiterzählte.

## [0.1.2] — 2026-08-16

### Hinzugefügt

- **Luftbild als Hintergrund** (`chester/dop.py`: `aerial_backdrop_png`). Ein
  Hintergrund will ein *Bild*, keine Daten: ein WMS-`GetMap` sind 70 KB in 0,3 s
  gegen 18–91 MB für eine einzelne Datenkachel. Genutzt von der visuellen Prüfung
  und der Bodenplatte des 3D-Stadtmodells; `fetch_dop` bleibt der Weg für alles,
  was ausgewertet werden soll.
- **Befliegungsjahr aus Kachelnamen** (`acquisition_years`) — bei Luftbildern ist
  die Aktualität die halbe Aussage, und die Quellen schreiben sie in den Dateinamen.
- **Aufwandskennzahlen im Benchmark.** Neben der Tool-Coverage (*Reichweite*) misst
  `testprompt.tool_effort` jetzt die *Kosten* eines Laufs: Aufrufe, verschiedene
  Werkzeuge, Aufrufe je geplantem Schritt und nicht eingeplante Werkzeuge. Drei
  erwartete Tools in dreißig Aufrufen waren vorher 100 %. Bewusst **ohne
  Schwellenwert** — an 36 archivierten Läufen kalibriert, die Aufrufzahl trennt
  Bestehen und Scheitern kaum (Median 14 zu 13). Details in
  [`doc/agent-test-prompts.md`](./doc/agent-test-prompts.md).
- **`./check.sh`** — ein Einstiegspunkt für „ist es grün": Lint-Zählung,
  Strukturtests, Unit-Tests. `--full` nimmt die Netz- und QGIS-Schichten dazu.
- **Strukturtests** (`tests/test_structure.py`) — Fitness-Funktionen über den
  *Quelltext* statt über das Verhalten: Import-Verträge, Capability-Verdrahtung,
  Dateigrößen- und Lint-Ratschen gegen eine eingecheckte Baseline.
- **`./mutate.sh`** — Mutationstests für einzelne Module, um Testgüte statt
  Testabdeckung zu messen.
- **[`doc/code-map.md`](./doc/code-map.md)** — ein Eintrag je Modul: was es tut,
  welche Entscheidung es festhält, wo die Fallen liegen.

### Geändert

- **SelmaKit 0.1.26** (von 0.1.25), mit pydantic-ai 2.31. Drei sichtbare Folgen:
  Skills sind **deferred capabilities** — im Prompt stehen nur Name und
  Beschreibung, den Rezepttext holt das Modell bei Bedarf; die Datei-Werkzeuge
  laufen **sandboxed** gegen das Zustandsverzeichnis; **Tracing ist opt-in** über
  einen `tracing`-Block in der Konfiguration. `start.sh` startet folglich keinen
  Tracing-Container mehr, sondern nur noch Gateway und Dashboard.
- **Der Schnappschuss der visuellen Prüfung zeichnet die Ergebnisebene deutlicher**
  (kräftigere Linien) und bekommt immer einen Kartenausschnitt, auch bei einer
  Ebene mit einem einzigen Objekt.
- **Testgüte statt Testabdeckung** in den Kernmodulen: die Plausibilitätsbänder,
  die Platzhalter- und Bereichsprüfungen der Validierung, die Ebenenbezeichnungen
  der Verwaltungsgliederung und die **Löschentscheidung** des GeoCache sind jetzt
  wertgenau festgenagelt — gefunden über Mutationstests, nicht über Abdeckung.
- Lint- und Typbefunde von 61 bzw. 65 auf **je 0**; jede begründete Ausnahme steht
  als `# noqa: <regel>  # <grund>` im Code und bleibt damit im Diff sichtbar.

### Behoben

- **Die visuelle Validierung war blind für Fehlplatzierung** — die Fehlerklasse,
  für die sie gebaut wurde. Vier stille Defekte, alle mit derselben Form: ein
  Fehlschlag, der wie ein Erfolg aussieht.
  1. Der Luftbild-WMS antwortet außerhalb seiner Abdeckung nicht mit einem Fehler,
     sondern mit HTTP 200 und einem reinweißen Bild. Das galt als Treffer und
     unterdrückte den OSM-Rückfall — genau im Fall, den ein CRS-Fehler erzeugt.
  2. OpenStreetMap lieferte `403`-Kacheln, weil der voreingestellte User-Agent
     nicht identifizierend ist; sie wurden wortlos ins Bild eingebaut.
  3. Die Basiskarte lag bei gleichem `zorder` **über** den Daten.
  4. Eine Ebene mit einem Objekt hatte keinen Kartenausschnitt.

  Wirkung, an denselben Szenarien gemessen: Das Vision-Modell urteilte vorher
  richtig, aber mit erfundener Begründung („Lücken zwischen Polygonen"), nachher
  benennt es den CRS-Fehler. Hintergrund in
  [`doc/visual-validation.md`](./doc/visual-validation.md) §7.
- **Das voreingestellte Vision-Modell war ein Textmodell.** Jede frische
  Installation bekam damit eine visuelle Prüfung, die beim ersten Einsatz mit
  `404` abbrach.
- `region_profile` fiel bei einem unbekannten Ländercode auf ein Profil zurück,
  statt die Vorgabe zu verwenden.
- Ein frischer Klon war rot: mehrere Strukturtests lasen Dateien, die absichtlich
  nicht Teil der Veröffentlichung sind. Sie überspringen jetzt, statt zu scheitern
  — die Prüfung gilt der Arbeitskopie, nicht dem Klon.

## [0.1.1] und davor

Aufbau von Chester bis zur vollständigen Werkzeugkette: Datenrecherche
(Geokodierung, OSM, STAC, DGM1, DOP, LoD2, amtliche Grenzen für DE/CH/AT,
Statistik, GTFS, LiDAR, WFS/WMS), QGIS-Werkzeugkasten, Vektor- und Rasteranalyse,
Spektralindizes, erzwingende Validierung, HTML-Karten, interaktive 3D-Stadtmodelle,
Steuerung von QGIS Desktop, Langzeitgedächtnis und neun Laufzeit-Skills. Diese
Phase ist nicht im Einzelnen protokolliert; die Funktionsübersicht steht in
[`doc/features.md`](./doc/features.md).
