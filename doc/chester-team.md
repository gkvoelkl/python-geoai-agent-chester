# Chester-Team — der Multi-Agent

Chester gibt es in zwei Besetzungen über **derselben** Werkzeugschicht: als **einen**
Agenten (`chester-agent`, die Basis aller bisherigen Messungen) und als **Team**
(`chester-team`) — ein Orchestrator, der die Arbeit an fünf Ressort-Agenten verteilt.
Beide laufen auf demselben Unterbau (`chester-runtime`), mit demselben Gate, denselben
Werkzeugen und demselben Modell. Bewegt wird nur die **Architektur**.

Dieses Dokument hält fest, warum das Team so geschnitten ist, welche Entscheidungen
dabei gefallen sind — jede mit ihrem Gegenargument und dem Kriterium, das sie
umstoßen würde —, und was gemessen werden soll. Den Aufbau der Pakete beschreibt die
[Code-Map](./code-map.md), die Teststufen [`test-levels.md`](./test-levels.md), die
Messzellen [`tool-compensation.md`](./tool-compensation.md).

**Stand:** gebaut am 19./20.09.2026, funktionsfähig, **noch nicht gemessen**.

## Der Anlass

Der Einzelagent trägt 85 Werkzeuge und rund 38.000 Zeichen Instruktionen in **jeder**
Runde. Zwei Fragen stehen dahinter: Wie gut wählt ein Modell aus so vielen Werkzeugen,
und was kostet der Vorspann? Ein Team beantwortet beides anders — jedes Ressort sieht
nur seinen Ausschnitt.

## Was es *nicht* wird, und warum

- **Keine Parallelität.** Auf einer lokalen GPU serialisiert ein Fan-out ohnehin, und
  ein Entwurf, der „modellagnostisch“ sein will, darf Parallelität nicht voraussetzen.
- **Kein Peer-Swarm.** Am Ende einer Geo-Aufgabe steht *ein* Ergebnis mit klarer
  Herkunft. Das ist eine Verarbeitungskette, und Ketten parallelisieren nicht. Ein
  Swarm passt zu Anfragen, deren Zuständigkeit sich erst aus dem Inhalt ergibt.

## Die Architektur: Orchestrator über einer Pipeline

Ein Koordinator zerlegt die Aufgabe, ruft Spezialisten **als Werkzeuge** und fasst
zusammen. Die Geo-Arbeit ist von Natur aus eine Kette:

```
finden → beschaffen → rechnen (Vektor | Raster) → darstellen
```

**Das Blackboard gibt es schon.** Workspace, GeoCache, `geofacts` und die
Provenance-Sidecars sind zusammen eine vollwertige gemeinsame Ablage. Deshalb sind
Sub-Agenten hier billig: Sie reichen **Pfade** weiter, keine Daten. Die klassische
Multi-Agent-Schwäche — Kontextverlust bei der Übergabe — fällt damit weitgehend weg,
weil die echten Artefakte auf der Platte liegen und nachvollziehbar sind. Das gilt für
die wenigsten Projekte und ist der stärkste Einzelgrund, warum der Umbau hier
überhaupt lohnt.

## Der Ressort-Schnitt

Geschnitten wird entlang der **Phasen**, nicht entlang der Fachdomänen. Die Zuordnung
steht als Daten in `chester/ressortcut.py` — nur Namen, keine Importe, damit jeder
Adapter denselben Schnitt nutzen kann.

| Ressort | Werkzeuge | Inhalt |
|---|---:|---|
| `data` | 47 | Finden **und** Holen: `geodata_search`, `stac_*`, `wfs_capabilities`, `geocode`, `region_*`, `*_sources`, `geocache_*` — und die ganze `fetch_*`-Familie, `osm_features`, `wfs_features`, `stats_table`, `geodataset_fetch`, die zwei Konverter |
| `vector` | 16 | `vector_*` und `service_area` |
| `raster` | 12 | `slope`, `aspect`, `hillshade`, `zonal_stats`, `raster_calc`, `spectral_index`, `detect_water`, … |
| `output` | 2 | `render_map`, `render_buildings_3d` — dazu **geliehen** `vector_reproject` (`LENT`), weil eine Karte sonst an der Projektion scheitert |
| **Prüfung** | 4 | `check_crs`, `sanity_check_result`, `check_topology`, `cross_check` — **kein Ressort**, jedes bekommt sie |

Dazu zwei Werkzeuge außerhalb der Hüllenschicht: `geo_python_run` bekommen Vektor und
Raster (ohne QGIS geht dort manches nur über einen Schnipsel), `inspect_map` bekommen
alle — es ist eine Prüfung.

### Finden und Holen sind ein Ressort

Zwei Tage lang waren es zwei — ein `scout`, der schaut, und eine `acquisition`, die
holt. Zusammengelegt am 21.09.2026, nachdem die Grenze zweimal Arbeit gemacht hatte
statt welche zu sparen:

1. **Sie trugen denselben Text.** Von 12 bzw. 13 Hüllenmodulen waren **9 gemeinsam**;
   die Vereinigung ihrer Instruktionen kam auf 25.501 Zeichen — *weniger* als jede der
   beiden für sich (26.134 / 26.503), weil dieselbe Connector-Prosa zweimal im Umlauf
   war, einmal für „was gibt es“ und einmal für „hol es“.
2. **Die Grenze musste schon geflickt werden.** `osm_features` und `wfs_features`
   standen zuerst beim Scout, wie das Konzept sie führte — beide sind Abfragen, und der
   Scout braucht einen Blick auf die Daten, um eine Quelle zu beurteilen. Aber beide
   **laden herunter und schreiben**: Der Scout holte `supermarkets.gpkg` selbst, und der
   Orchestrator plante daraufhin „erst finden, dann holen“ — zwei Schritte für dieselbe
   Arbeit.
3. **Es ist ein Gedanke.** „Schau, was es für Regensburg gibt“ und „nimm es“ gehören
   zusammen. Getrennt musste der Scout sein Urteil über eine Quelle als Prosa
   hinüberreichen, an ein Ressort, das dasselbe Urteil noch einmal bilden musste — genau
   der Handoff, den der Phasenschnitt vermeiden soll.

Der Preis: 47 Werkzeuge plus die vier Prüfwerkzeuge, 28.109 Zeichen Vorspann. Das ist
nah genug an den 83 Werkzeugen und ~38.000 Zeichen des Einzelagenten, dass dieses
Ressort dessen Auswahlproblem erben könnte. **Das ist zugleich das Umstoßkriterium:**
Liegt die Werkzeug-Trefferquote *innerhalb* von `data` unter der des Einzelagenten auf
denselben Prompts, war der Schnitt zu grob und die Trennung kehrt zurück.

**Zwei Regeln für den Schnitt.** Ein Ressort muss eine **ganze Kette** ausführen
können: `clip → buffer → dissolve` ist *ein* Gedanke, und wer mitten darin übergibt,
zahlt eine Übergabe für nichts. Und: Phasen, keine Einzeloperationen.

## Die Entscheidungen

Jede steht so auch im Code, dort wo sie wirkt.

### Prüfung ist kein Ressort

Vier Gründe: Ein Ergebnis wird geprüft, **wo es entstanden ist** — die Prüfung braucht
die Absicht des Schritts, und die hat nur der Ausführende. Ressorts sind Phasen,
Prüfen ist Querschnitt. Die unabhängige Instanz gibt es bereits zweimal, und beide
sind unabhängiger als ein Prüf-Ressort auf demselben Modell wäre: das **Gate**
(mechanisch, kann eine Wiederholung erzwingen) und der **Judge** auf Test-Level 3
(anderes Modell, andere Herkunftslinie). Und die meisten Prüfungen sind Rechnung, kein
Denkvorgang; ein eigener Agentenlauf wäre teuer für nichts.

*Gegenpunkt:* `inspect_map` ist die eine Prüfung, die ein Modell braucht — der
plausible Kandidat, falls doch ein Prüf-Ressort kommt.
*Was es umstößt:* Rufen die Ressorts ihre Prüfwerkzeuge kaum, ist „jeder prüft sich
selbst“ in der Praxis gescheitert. Zählbar in `team-runs/ressort-calls.jsonl`.

### Ressorts sind je Aufruf zustandslos

Kein Ressort-Agent existiert, bis der Orchestrator eines ruft; jeder Aufruf baut einen
frischen. Gemessen: 7 ms beim ersten Aufbau (Modulimporte), danach unter 1 ms — gegen
Minuten für einen Modellaufruf. Es gibt also nichts zu speichern. Was ein Ressort
weiß, steht im Auftrag und in den Eingabepfaden; der Zustand liegt auf der Platte.

*Preis:* Jeder Aufruf zahlt den vollen Prefill seiner Instruktionen, und zwei Aufrufe
desselben Ressorts teilen keinen Faden.
*Alternative:* ein `message_history` je Ressort **innerhalb** eines
Orchestrator-Laufs — billiger und mit Kontext, aber ein Ressort mit Gedächtnis kann
eine überholte Annahme mitschleppen, die niemand mehr sieht.

### Das Team nimmt nichts aus Chester-MCP

`validate_result` ist das Gate **ohne Zwang**, gebaut für einen fremden Client, den
Chester nicht zur Wiederholung bewegen kann. Der Orchestrator hat das echte Gate,
genau wie der Einzelagent; ein zweiter Weg zu denselben Prüfungen lädt nur ein, den
billigeren zu nehmen. `read_artifact` gibt Dateiinhalte zurück, weil ein MCP-Client
Chesters Cache nicht lesen kann — im Prozess liest man die Datei. Beide stehen als
`MCP_ONLY` im Schnitt: benannt, damit er vollständig bleibt, aber keinem Ressort
zugeteilt.

### Skills haben die Ressorts, nicht der Orchestrator

Ein Skill ist ein **Rezept mit Werkzeugnamen**, geschrieben für einen Agenten, der sie
besitzt. Der Orchestrator besitzt keine — er verteilt Ziele —, und er nahm am 21.09.
genau daran Schaden: Er las `walkability`, reichte `qgis_service_area` als Anweisung
an ein Ressort weiter, und das lief in den Zeitdeckel. Er bekommt deshalb weder den
Katalog noch den Skill-Hinweis.

Die Ressorts bekommen ihn (entschieden am selben Tag, nach dem nächsten Lauf): Aus
„Supermärkte im 10-Minuten-Gehbereich“ wurde ein **800-Meter-Luftlinienpuffer** —
genau der Fehler, vor dem `walkability` in seiner ersten Zeile warnt („Uses real
network reach, not straight-line buffers“). Das Wissen fehlte dort, wo gerechnet wird.

Der Katalog ist **aufgeschoben**: Im Prompt stehen nur Name und eine Zeile je Skill,
der Rumpf wird bei Bedarf geladen. Gemessen am 21.09.: Vektor 12.229 Zeichen
Gesamtprompt (9.536 Text + Katalog), Scout 28.827. Gegenüber den ~38.000 des
Einzelagenten bleibt der Gewinn bestehen.

### Der Orchestrator ist ein SelmaKit-Agent, ein Ressort nicht

Der Orchestrator ist das, womit der Nutzer redet: Sitzungen, Kanäle, Dashboard,
Persona, Gate-Anschluss, Slash-Befehle. Das alles nachzubauen, um ihn in blankem
pydantic-ai zu schreiben, wäre Arbeit ohne Gegenwert. Ein Ressort dagegen braucht
nichts davon: Es bekommt einen Auftrag und liefert Pfade zurück. Es ist ein
pydantic-ai-Agent über seinem Werkzeugausschnitt — *ein Agent als Werkzeug*.

### Die Persona ist vorerst geteilt

Team und Agent lesen dieselben Workspace-Dateien (`SOUL.md`, `IDENTITY.md`). Die Rolle
des Orchestrators steht in seinen Capability-Instruktionen, nicht in einer zweiten
Identität.

## Der Weg einer Anfrage

1. Der **Orchestrator** (10 Werkzeuge: 5 Ressorts, 4 Prüfwerkzeuge, `inspect_map`)
   zerlegt die Aufgabe. Er verteilt **Ziele, keine Rezepte**: Er nennt weder Werkzeuge
   noch Schritte, denn er weiß nicht, welche Werkzeuge ein Ressort hat. Deshalb hat er
   auch **keine Skills** — ein Skill ist ein Rezept mit Werkzeugnamen, geschrieben für
   einen Agenten, der sie besitzt. Am 21.09. las er `walkability` und reichte
   `qgis_service_area` als Anweisung an ein Ressort weiter, ein Werkzeug, das ohne
   QGIS nicht existiert; das Ressort lief in den Zeitdeckel.
2. Er ruft ein **Ressort-Werkzeug** mit Auftrag und Eingabepfaden.
3. Das **Ressort** läuft als eigener Agent mit seinem Ausschnitt.
4. Es gibt zurück: `outputs` (absolute Pfade, jede Datei einmal), `report`,
   `open_points` — und bei einem Deckel `capped: true`, bei einem Fehler `ok: false`
   samt `error`. **Es wirft nie**; ein gescheitertes Ressort darf den Lauf des Teams
   nicht mitreißen. Antwortet das Modell statt der Struktur in **Prosa**, gilt der Text
   als Bericht: Ein lokales Modell verlor am 20.09. dreimal hintereinander eine
   getane Arbeit an der Formvorschrift, und die Pfade stehen ohnehin in den
   Werkzeug-Rückgaben.
5. Der Orchestrator gibt die Pfade weiter oder antwortet.
6. Das **Gate** prüft die Dateien, die der Lauf erzeugt *und* die Antwort nennt.

**Der Übergabevertrag ist die Statik des Ganzen.** Das Gate sieht vom Team nur die
Ressort-Rückgaben. Steht ein Pfad nicht in `outputs` und nennt die Antwort ihn nicht,
prüft das Gate ihn nicht — und schweigt dabei. Deshalb nennen die Ressort-Rückgaben
ihre Pfade, und deshalb verlangen die Instruktionen des Orchestrators die **exakten**
Pfade in der Antwort.

**Deckel.** 25 Anfragen und 600 s je Ressort-Aufruf (`team.ressort_request_limit`,
`team.ressort_timeout_s`). Greift einer, sagt die Rückgabe das — ein stummer Abbruch
in einem Ressort wäre schlimmer als im Einzelagenten, weil niemand zusieht.

**Das Gate spricht die Sprache des Empfängers.** Seine Wiederholungstexte nennen dem
Einzelagenten Werkzeugnamen (gemessen: das wirkt), dem Team dagegen seine Ressorts
(`AGENT_ROUTES` / `TEAM_ROUTES`). Im ersten Team-Lauf hatte der Orchestrator aus einer
Empfehlung für `geodata_search` einen Scout-Auftrag improvisiert, der ins Leere lief.

## Betrieb

```
uv run ask_team.py "…"    # CLI wie ask.py
./start_team.sh           # Gateway :8100 + Dashboard :8601
./test_team.sh            # Test-Bench :8602
```

Eigene Ports, damit Agent und Team **gleichzeitig** laufen können; Telegram bleibt aus
(zwei Bots teilen sich kein Token). Alles steht im Block `team` der
`.chester/chester.json` (`webchat_port`, `dashboard_port`, `bench_port`,
`ressort_model`, `ressort_request_limit`, `ressort_timeout_s`). Weil SelmaKit seine
Konfiguration nur als Datei liest, schreibt das Team bei jedem Start eine abgeleitete
Sicht `<name>.team.json` — Zustand, keine zweite Quelle.

Geteilt werden GeoCache, Workspace und das Sitzungsverzeichnis: ein Cache, eine
Ablage. Jeder Ressort-Aufruf hinterlässt eine Zeile in
`.chester/workspace/team-runs/ressort-calls.jsonl` (Ressort, Werkzeuge, Dauer, Grund
eines Abbruchs) — und unter `calls` **jeden einzelnen Werkzeugaufruf mit seinem
Ausgang**: `ok` und, wenn es schiefging, die ersten 200 Zeichen des Fehlers. Dort steht
also, *warum* ein Werkzeug nicht half; in der Rückgabe an den Orchestrator steht es
bewusst nicht, denn die muss klein bleiben (siehe oben).

**Live sichtbar.** Ein Ressort-Aufruf dauert Minuten, und bis er zurückkommt, sah der
Zuschauer nichts. `ask.py` veröffentlicht seinen Strom deshalb als Kanal
(`chester.runtime.live`), und jedes Ressort schreibt **jeden Aufruf und sein Ergebnis**
hinein, während es arbeitet:

```
   [vector] → vector_reproject({"input_path": "pts.gpkg", …})
   [vector] ← vector_reproject: {"ok": true, "output": "…/pts_25832.gpkg", …}
```

Dazu am Ende eine Zusammenfassung unter der Ressort-Rückgabe — mit
Wiederholungszähler, denn genau der ist bei einem Fehlgriff der Befund:

```
← ressort_data: {"ok": false, …}
   ↳ geodatasets_list, geodataset_fetch×22 · 408s · request limit of 25
```

Das gilt für die CLI, `ask_team.py` und die Test-Bench gleichermaßen, weil alle
denselben Strom benutzen.

## Messen

Die Zellen heißen **L+team** und **F+team** (siehe
[`tool-compensation.md`](./tool-compensation.md)): dasselbe Modell, dieselben
Werkzeuge wie L+ bzw. F+, bewegt wird nur die Architektur. Jede Historienzeile trägt
`agent` (`agent` oder `team`), und der Bericht warnt, wenn Etikett und Agentenart
nicht zusammenpassen.

**Zwei Kennzahlen, und erst beide zusammen sagen etwas:**

- **Werkzeug-Trefferquote** — wurde das erwartete Werkzeug gerufen? Beim Team gemessen
  an den Werkzeugen *in* den Ressorts.
- **Ressort-Trefferquote** — hat der Orchestrator an das erwartete Ressort übergeben?

Ohne die zweite Zahl ließe sich nicht unterscheiden, ob der Schnitt wirkt oder die
Entscheidung nur eine Ebene nach oben gewandert ist.

**Schon gemessen (21.09.2026):** Instruktionen je Ressort — Vektor 9.536, Raster
10.544, Ausgabe 11.990 Zeichen gegen ~38.000 beim Einzelagenten; `data` 28.109, weil es
den ganzen Beschaffungstext trägt. Das ist der nächste Ansatzpunkt, falls der Vorspann
gedrückt werden soll — und der einzige Posten, bei dem das Team dem Einzelagenten
nahekommt.

**Erste echte Läufe** (gemma4, Umprojektion): Der Orchestrator gibt die Arbeit richtig
an `ressort_vector`, das Ergebnis stimmt (EPSG:25832, 40.000 m² gegen den Sollwert).
Der erste Lauf stürzte ab — ein Fehlalarm des Gates schickte den Scout los, der seine
Übergabe nicht formgerecht hinbekam, und die Ausnahme riss alles mit. Beides ist
behoben (Fehlalarm; ein Ressort meldet Fehler, statt zu werfen).

**Erster Lauf auf Test-Level 3** (20.09., `supermarkets-within-10min-walk`,
abgebrochen): Das Suchen lief sauber. Dann bekam das **beschaffende** Ressort den
Auftrag, eine Punktebene aus zwei Koordinaten zu erzeugen — wofür es kein Werkzeug hat.
Es probierte 22× `geodataset_fetch`, bis der Anfragedeckel griff; der Orchestrator gab
ihm dieselbe Aufgabe zweimal erneut. Die Deckel hielten, nichts stürzte ab. Die Lehre
steckt jetzt in drei Texten: Das Daten-Ressort holt, was es gibt — eine Ebene
**erzeugen** ist Vektorarbeit; passt kein Werkzeug, probiert ein Ressort keine
Varianten, sondern sagt, was fehlt und wer es kann; und ein Ressort ohne die nötigen
Werkzeuge bekommt dieselbe Aufgabe nicht noch einmal.

## Offen

- **Die vergleichenden Messläufe** L+team ↔ L+ und F+team ↔ F+ (`internal/TODO.md`,
  Phase KP).
- **QGIS bleibt vorerst draußen.** Die Ressorts arbeiten mit der Hüllenschicht; die
  `qgis_*`-Familie hat noch keine Hüllenschicht.
- **Beobachten statt jetzt ändern:** ob die abgeleitete Config oder das geteilte
  Sitzungsverzeichnis stören; ob eine sehr lange Ressort-Rückgabe ausgelagert wird und
  dem Gate die Pfade verbirgt; ob ein Umweg über ein falsches Ressort in der
  Trefferquote auffällt.
- **Der Name.** Sind die Ressorts Capabilities, hießen die Pakete eher
  `chester-orchestrator` und `chester-ressorts`. Vorerst bleibt es `chester-team` mit
  den Modulen `orchestrator` und `ressorts`.
