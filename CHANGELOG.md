# Changelog

Alle nennenswerten Änderungen an Chester. Format nach
[Keep a Changelog](https://keepachangelog.com/de/1.1.0/), Versionierung nach
[Semantic Versioning](https://semver.org/lang/de/).

Chester ist ein **Forschungsvehikel**, kein Produkt — „experimentell" ist kein
Übergangszustand. Schnittstellen dürfen sich zwischen Vorabversionen ändern.

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
