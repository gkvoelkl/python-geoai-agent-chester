# Visuelle Validierung — Chester sein Ergebnis *ansehen* lassen

> Begleitdokument zu [`geodata-search.md`](./geodata-search.md) (ein Akquise-Feature).
> Dieses Dokument
> skizziert ein **Korrektheits**-Feature: einen Karte-im-Loop-Schritt, bei dem der
> Agent das gerenderte Ergebnis *sieht* und Geometriefehler selbst korrigiert, die
> die aktuellen reinen Textprüfungen übersehen.
>
> **Status: gebaut** — das `inspect_map`-Tool (ein statischer Snapshot, als
> Bild-Content zurückgegeben), immer registriert, plus ein **Fallback-Vision-Modell**
> (`model.vision_model`), an das der Agent routet, wenn er das Bild nicht selbst sehen
> kann, und der `review-result`-Skill. **Neu (V4): dieser visuelle Kanal ist jetzt
> auch in das erzwingende Gate verdrahtet** — auf Validierungsstufe ≥2
> (`/valid_level 2`) rendert das Gate das gemeldete Ergebnis und holt ein
> **beratendes** Vision-Urteil ein (als Notiz an die Antwort, kein harter Retry —
> siehe `validation-concept.md` §4.1). Der Snapshot bekam dafür eine **OSM-Basiskarte**
> (`contextily`, best-effort), damit Fehlplatzierung überhaupt erkennbar ist. Phase D
> (der Abnahme-Eval mit eingebautem Geometriefehler) ist als opt-in Test gebaut
> (`tests/test_validation_v4.py`, `llm`+`network`-markiert). Inspiriert von
> [`dekart-xyz/geosql`](https://github.com/dekart-xyz/geosql), einem Claude/Codex-Skill,
> dessen prägende Idee ein SQL→Karte→*ansehen*→korrigieren-Loop ist.

## 0. These

Chester behandelt Korrektheit bereits als Pflichtphase im Loop — aber die Prüfungen
sind **textuell/numerisch**: `check_crs`, `sanity_check_result` (Fläche/Plausibilität),
die Regel „CRS vor dem Messen". Zahlen fangen *manche* Fehler. Sie übersehen die, die
ein Mensch auf einen Blick erkennt:

- eine falsch reprojizierte Ebene, die im Ozean oder in der falschen Hemisphäre landet;
- ein „Bezirks"-Polygon, das in Wahrheit die ganze Metropol-Umrandung ist;
- ein Buffer/Overlay, das überlappende Features doppelt zählt;
- eine Choroplethe, deren Klassen alle eine Farbe haben (kaputter Join, konstantes Feld);
- eine Wasser-/Vegetations-Klassifikation, die über eine offensichtliche Wolke „ausblutet".

Die Lehre von GeoSQL ist unmissverständlich: einen **visuellen** Kanal in den Loop zu
ziehen — Ergebnis rendern, Bild ansehen, korrigieren — steigert den Erfolg bei
Geo-Aufgaben angeblich weit stärker als jede Prompt-Optimierung. (Das genaue „4×" ist
umstritten, aber die *Richtung* stimmt: eine zweite Modalität fängt eine Fehlerklasse,
die die erste nicht kann.)

Chester ist dafür ungewöhnlich gut aufgestellt: das Rendering existiert bereits
(`render_map` — jetzt raster-fähig — und `qgis_screenshot`), und die
Multi-Provider-Modellschicht unterstützt bereits Vision-Modelle. Das **einzige**
fehlende Stück ist ein Schritt, der das gerenderte Bild *zurück in das Reasoning des
Agenten* speist.

## 1. Das Bild sehen — annehmen, dann ein Fallback (keine Vorab-Erkennung)

Der Loop braucht ein Modell, das Bilder sehen kann. Das im Voraus zu erkennen erwies
sich als unzuverlässig: ein Name lügt (ein MLX-`qwen3.5:27b`-Build *ist* Vision,
`gemma4:12b` nicht), und selbst Ollamas `/api/show`-`capabilities` **lügt** — das
Standard-Coding-Modell wirbt mit `vision`, antwortet aber auf ein Testbild mit
*„Ich sehe kein Bild."* Das einzige vertrauenswürdige Signal ist die Antwort des
Modells selbst.

Das Design **erkennt daher nicht vorab**. Stattdessen:

- **Annehmen, dass das Hauptmodell sehen kann.** `inspect_map` ist immer verfügbar und
  liefert den Snapshot als Bild-Content.
- **Das Laufzeit-Signal ist die Wahrheit.** Kann das Hauptmodell es nicht sehen, sagt
  es das („Ich sehe kein Bild") — *so* erfährt man, dass es reines Text ist.
- **Ein konfiguriertes Fallback-Vision-Modell.** `.chester/chester.json` trägt
  `model.vision_model` (z. B. `ollama/llava:latest` oder ein gehostetes
  `anthropic/claude-*`). Kann das Hauptmodell nicht sehen, ruft der Agent
  `inspect_map(..., via_vision_model=True)`; der Snapshot wird an dieses Modell
  geroutet (über SelmaKits eigenen Provider-Dispatch), und dessen **schriftliches
  Urteil** kommt zurück — so bekommt selbst ein reines Textmodell eine brauchbare
  visuelle Prüfung.

Ein vorhandenes Capability-Flag sagt nichts über die *Qualität*: llava sieht das Bild,
liest eine Karte aber schlecht; ein stärkeres Vision-Modell gibt ein besseres Urteil.
`model.vision_model` entsprechend wählen.

## 2. Was gerendert wird — ein *statischer* Snapshot, nicht die interaktive Karte

`render_map` erzeugt **Folium-HTML**; ein Modell kann HTML nicht „sehen". Visuelle
Validierung braucht ein **Rasterbild** (PNG). Zwei Quellen, mit klarem Default:

- **Headless-Statik-Snapshot (Default):** ein `geopandas`/`matplotlib`-Plot derselben
  Ebenen → PNG, optional über einer Basiskarte (`contextily`). Self-contained, kein
  Fenster, läuft auf einem Server. Für die Validierung ausreichend — Formen, Ausdehnung,
  Abdeckung, Klassenvariation sind lesbar, auch wenn es nicht die polierte interaktive
  Karte ist.
- **`qgis_screenshot` (existiert bereits):** ein PNG des lebenden QGIS Desktop. Höhere
  Detailtreue, braucht aber ein sichtbares Fenster und ist nur lokal — wiederverwenden,
  wenn QGIS ohnehin offen ist, nicht als Default.

Der Snapshot ist ein Validierungs-Artefakt, nicht das nutzerseitige Ergebnis (das bleibt
die interaktive HTML von `render_map`).

## 3. Architektur — ein Tool, das ein für das Modell sichtbares Bild zurückgibt

pydantic-ai erlaubt einem Tool, **Bild-Content** zurückzugeben (`BinaryContent`,
`media_type="image/png"`), den ein Vision-Modell als Teil des Tool-Ergebnisses erhält.
Das ist der ganze Mechanismus.

**`inspect_map(layers, question=None, via_vision_model=False)`** — Tool auf
`MapOutputCapability`
- Rendert einen statischen PNG-Snapshot von `layers` (§2) in den Cache.
- **Default (`via_vision_model=False`):** gibt `BinaryContent(png)` **plus** eine kurze
  textuelle Zusammenfassung zurück, die das Modell gegen das Bild gegenprüft — Feature-Zahl
  je Ebene, Geometrietypen, CRS, WGS84-Ausdehnung und (bei einer Choroplethe) den
  Wertebereich. Das Hauptmodell *sieht* die Karte im Tool-Ergebnis.
- **Fallback (`via_vision_model=True`):** routet den Snapshot an das konfigurierte
  `model.vision_model` (über SelmaKits Provider-Dispatch, eine multimodale Runde) und gibt
  dessen **schriftliches Urteil** als Ergebnis zurück — für den Fall, dass das Hauptmodell
  das Bild nicht selbst sehen kann.
- **Automatisch derselbe Fallback:** kann das Hauptmodell *nachweislich* kein Bild
  entgegennehmen, schaltet das Tool von sich aus um, ohne auf das Flag zu warten
  (`chester/visioncaps.py`; siehe §7 „Blindes Hauptmodell"). Ist dann kein
  `model.vision_model` gesetzt, kommt der Faktenteil **ohne Bild** zurück, mit einer
  Notiz, dass die visuelle Prüfung nicht stattgefunden hat — inert statt fatal.
- `question` fokussiert die Prüfung optional („folgt das Wasser dem Flusslauf?",
  „kacheln die Bezirke die Stadt ohne Lücken/Überlappungen?").

Der Agent schließt dann aus dem, was er (oder das Fallback-Modell) gesehen hat:
plausibel → weiter; unplausibel → diagnostizieren (falsches CRS? kaputter Join? falsche
Ebene?) und den fehlerhaften Schritt neu machen. Das ist ein **Loop**, kein Endschritt.

## 4. Worauf die Instructions den Agenten achten lassen

Eine kurze Checkliste in `get_instructions()` der Capability (und vom `review-result`-Skill
wiederverwendet), jeder Punkt bildet ein visuelles Symptom auf eine wahrscheinliche
Ursache ab:

- **Platzierung** — liegen die Daten dort, wo der Ort wirklich ist? (vor der Küste /
  falsche Hemisphäre ⇒ CRS- oder lon/lat-Vertauschungs-Bug.)
- **Ausdehnung/Maßstab** — passt der Umriss zum erwarteten Gebiet? (ein Bezirk, der den
  ganzen Rahmen füllt ⇒ falsche Verwaltungsebene.)
- **Abdeckung/Kachelung** — decken Partitionsebenen (Voronoi, Bezirke) ohne Lücken oder
  Überlappungen ab?
- **Choroplethen-Variation** — variiert die Farbe wirklich? (uniform ⇒ kaputter Join oder
  ein konstantes/leeres Feld.)
- **Klassifikations-Ausbluten** — folgt bei NDWI/NDVI Wasser/Vegetation echten Merkmalen,
  nicht Wolke/Schatten?
- Das Bild gegen die **numerische Zusammenfassung** und die aufgabeneigene Plausibilität
  gegenprüfen (eine Flut sollte sichtbar größer sein als der normale Flusslauf).

## 5. Phasen

| Phase | Ergebnis | Status | Begründung |
|---|---|---|---|
| **A** | `inspect_map` — statischer Snapshot → `BinaryContent`, immer registriert | **gebaut** | der Mechanismus; entsperrt alles |
| **B** | Instructions + ein `review-result`-Skill (die §4-Checkliste, der render→ansehen→korrigieren-Loop) | gebaut | der Instruction-Block *und* der `review-result`-Skill sind ausgeliefert |
| **C** | annehmen + `model.vision_model`-Fallback, **plus** Vorab-Erkennung wenn das Modell sie sicher beantwortet | gebaut, **2026-08-19 revidiert** | siehe §7 „Blindes Hauptmodell": die Annahme trug nur, solange ein blindes Modell *antworten* konnte — gegen ein lokales Ollama kann es das nicht |
| **D** | Eval: ein Szenario mit *eingebautem* Geometriefehler, den die Textprüfungen bestehen, das Auge aber fängt | **gebaut** (opt-in) | `test_visual_check_catches_misplaced_layer` (lon/lat-Vertauschung → Ozean), `llm`+`network`-markiert, braucht ein konfiguriertes `model.vision_model` |
| **E** | Den visuellen Kanal ins **Gate** ziehen (Stufe ≥2): Ergebnis rendern → Vision-Urteil als beratende Notiz | **gebaut (V4)** | `chester/gate.py: _visual_problems` + `make_validation_gate(vision_model=…)`; Snapshot mit OSM-Basiskarte; beratend, kein harter Retry (§7) |

## 6. Abnahmetest

Eine Aufgabe, die ein subtil falsches Ergebnis erzeugt, das die aktuellen Textprüfungen
**akzeptieren** — z. B. eine mit lon/lat-Vertauschung oder falschem Quell-CRS geschriebene
Ebene, sodass sie im Meer rendert, während `sanity_check_result` weiterhin valide,
nicht-leere Geometrie sieht. Erfolg: mit einem Vision-Modell markiert `inspect_map` die
Fehlplatzierung und der Agent macht die Reprojektion neu; mit einem reinen Textmodell ist
das Tool inert (dokumentierte Einschränkung, kein Fehler).

## 7. Offene Fragen / Risiken

- **Vision-Modell-Abhängigkeit** — irgendwo muss ein Modell das Bild sehen. Wir nehmen an,
  das Hauptmodell kann es, und fallen auf `model.vision_model` zurück, wenn nicht; kann
  *keines* sehen, ist die Prüfung schlicht nicht verfügbar (die numerischen Prüfungen
  bleiben).
- **Blindes Hauptmodell — der Fallback war unerreichbar** (gefunden 2026-08-19, behoben).
  Die Annahme hinter Entscheidung C war: ein Modell, das das Bild nicht sieht, *sagt das*
  und ruft `inspect_map(via_vision_model=True)` nach. Gegen ein lokales Ollama stimmt das
  nicht. Es lehnt die **Anfrage** mit HTTP 400 („this model does not support image input")
  ab, bevor das Modell ein Token liest; die Exception reißt den Event-Stream ab, und weil
  SelmaKit eine Session nur bei *vollständigem* Ergebnis schreibt, bleibt vom ganzen Lauf
  **nichts** übrig. Gemessen an `walk-isochrone-hauptbahnhof`: 634 s korrekte
  Geoverarbeitung (OSM-Netz, Reprojektion, Service-Area, Karte), danach keine Spur zum
  Nachlesen und nichts zu bewerten — der Fallback war genau für die Modelle unerreichbar,
  für die er gebaut wurde. Die Routing-Entscheidung fällt jetzt **vor** dem Anhängen des
  Bildes, in `chester/visioncaps.py`. Die Erkennung ist bewusst zaghaft: `False` nur bei
  einer ausdrücklichen Fähigkeitsliste ohne `vision`, sonst `None` = unbekannt und alles
  bleibt wie bisher. Falsch in diese Richtung kostet einen unnötigen Sprung zum
  Fallback-Modell, falsch in die andere den Lauf. Das entwertet die alte Begründung
  („`/api/show` lügt") nicht — es macht sie nur unerheblich, weil Schweigen als *unbekannt*
  gewertet wird, nie als „sieht nichts".
- **Qualität des Fallback-Modells** — das konfigurierte `model.vision_model` kann schwach
  sein (llava liest eine dünne Karte schlecht). Es ist nur so gut wie das gewählte Modell —
  aber **erst nachsehen, ob es überhaupt an der Größe liegt**: am 2026-08-26 lag es an der
  Eingabe, nicht am Modell (nächster Punkt), und dasselbe 8B-Modell urteilte danach richtig.
- **Selbstbeurteilung durch dasselbe Modell** — das Modell, das den Fehler machte, beurteilt
  auch das Bild. Eine zweite *Modalität* hilft trotzdem (das ist GeoSQLs ganze Prämisse),
  ist aber keine Garantie; das Urteil **beratend** halten, kein hartes Gate, das ewig
  loopen kann. Die Zahl der Review→Fix-Iterationen deckeln.
- **Snapshot-Treue vs. Ergebnis** — das statische PNG muss nicht der interaktiven Karte
  gleichen; es muss nur lesbar genug sein, um die §4-Symptome zu erkennen. Einfach halten
  (Basiskarte + klassifizierte Farbe + Legende).
- **Der Snapshot ist die eigentliche Fehlerquelle, nicht das Modell** (gemessen
  2026-08-16, gefunden durch *Ansehen* der PNGs statt durch Lesen der Rückgabewerte).
  Vier stille Defekte machten die Prüfung genau für Fehlplatzierung blind — die
  Fehlerklasse, für die sie gebaut wurde. Alle vier hatten dieselbe Form: **ein
  Fehlschlag, der wie ein Erfolg aussieht**, deshalb stehen sie hier als Merkposten:
  1. Der Luftbild-WMS antwortet außerhalb seiner Abdeckung nicht mit einem Fehler,
     sondern mit HTTP 200 und einem **reinweißen Bild** (`std=0.00`). Der Code prüfte
     nur auf leere Bytes, hielt das für einen Treffer — und unterdrückte damit den
     OSM-Fallback. Genau der Fall, den ein CRS-Bug erzeugt: Daten im Meer, wo es keine
     Luftbilder gibt.
  2. OSM lieferte **403 „Access blocked"**-Kacheln, weil `contextily` per Vorgabe eine
     zufällige UUID als User-Agent sendet; `add_basemap` baut die Fehlerkacheln
     wortlos ins Bild ein, statt zu scheitern. Behoben durch einen identifizierenden
     User-Agent (OSM-Kachelrichtlinie).
  3. Die Basiskarte lag **über** den Daten: unsere erste Ebene zeichnet mit `zorder=i`,
     also 0 für i=0, und `contextily` lässt ein Basis-Bild ebenfalls auf 0 — bei
     Gleichstand entscheidet die Zeichenreihenfolge.
  4. Eine Ebene mit **einem** Feature hat eine entartete Bounding-Box, also gar keinen
     Kartenausschnitt. Der Abnahmetest aus Phase D ruhte darauf: er konnte nur bestehen,
     wenn ein Modell aus einem leeren Bild einen Befund *rät*.

  Wirkung, gemessen an denselben zwei Szenarien: `qwen3-vl` urteilte vorher `PROBLEM:
  Partition polygons leave big gaps` (richtige Entscheidung, erfundene Begründung),
  nachher `PROBLEM: CRS bug`. Ein Modellwechsel hätte das nicht gebracht.
- **Ein Bild ohne Legende lädt zum Raten ein** (gemessen 2026-08-26 — dieselbe Lehre ein
  zweites Mal, an einer anderen Stelle). Zwei von zwei visuellen Prüfungen dieses Tages
  beurteilten die falsche Sache: einmal hielt das Sehmodell eine blau **gefüllte**
  Stadtgrenze für die Vegetationsfläche, einmal beschrieb es auf einer Regensburger Karte
  Münchner Ortsnamen und „orange Ringe", die es nicht gab. Der Prüf-Prompt bestand bis
  dahin nur aus der Frage des Agenten — welche Ebene welche Farbe trägt, stand nirgends,
  und in einem Fall fragte der Agent nach einem **Puffer, den er gar nicht mitgezeichnet
  hatte**. Ein Modell, das unterbestimmt gefragt wird, füllt die Lücke, statt sie zu
  melden.
  *Behoben:* `_render_snapshot` führt die Farbe je Ebene mit, `_legend()` stellt der Frage
  „diese Ebenen, von unten nach oben, in diesen Farben" voran, und `_review_prompt()` hängt
  zwei Regeln an — nur Gezeichnetes beurteilen, und keinen Ortsnamen nennen, der nicht als
  **Beschriftung im Bild** steht.
  *Gemessen, gleiche Bilder, gleiche Frage:* `qwen3-vl:8b` antwortet auf das Bild ohne
  Puffer jetzt „the buffer … is not one of the layers listed" (64 s) und auf das Bild *mit*
  Puffer mit einer richtigen Beschreibung samt Farbzuordnung (151 s) — beides vorher
  fehlerhaft. Der Gegenversuch mit `qwen3-vl:30b-a3b` wurde nach **25 min ohne Antwort**
  abgebrochen: `ollama ps` meldete `45 GB · 50%/50% CPU/GPU · CONTEXT 262144` auf einer
  32-GB-Maschine — nicht die 19 GB Gewichte sprengen den Speicher, sondern der KV-Cache
  eines 256k-Fensters. **Vor der Parameterzahl das Kontextfenster prüfen**, sonst misst man
  Speicherdruck und nennt es Modellqualität.
- **Kosten/Latenz** — ein zusätzliches Rendern plus eine Vision-Runde je geprüftem Ergebnis.
  Begrenzen: das **finale** Ergebnis prüfen (oder auf ausdrückliche Anfrage), nicht jedes
  Zwischenergebnis.
- **Abhängigkeit** — eine Basiskarte unter dem Snapshot will `contextily` (eine neue, kleine
  Abhängigkeit); ohne sie auf schlichtem Hintergrund plotten.
- **Unbewiesene Größenordnung** — GeoSQLs eigene Zahlen wurden auf HN angezweifelt. Den
  Nutzen für Chester als real-aber-unquantifiziert behandeln, bis Phase D ihn misst.

## 8. Was das *nicht* ist
Kein Ersatz für die numerischen Prüfungen — es ist ein **zweiter** Kanal neben
`check_crs`/`sanity_check_result`. Und nichts hier berührt Chesters SQL-Oberfläche
(GeoSQL ist ein Spatial-SQL-Agent; Chester führt QGIS-Algorithmen aus) — nur die
render→ansehen→korrigieren-*Idee* überträgt sich.
