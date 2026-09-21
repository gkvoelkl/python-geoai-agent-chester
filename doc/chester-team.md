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

### Das Team hat keine Skills — beide Plätze sind durchprobiert

An einem Tag (21.09.2026) an beiden Stellen versucht, beide Male hat es einen Lauf
gekostet:

- **Beim Orchestrator.** Er las `walkability` und reichte `qgis_service_area` als
  Anweisung an ein Ressort weiter — ein Werkzeug, das er nicht hat und niemand im Team
  hat. Das Ressort lief in den Zeitdeckel.
- **Bei den Ressorts.** Das Ausgabe-Ressort lud vier Skills in **215 Sekunden**, drei
  davon Rezepte für Phasen, die es nicht bedient, und rief danach `geodatasets_list`,
  `vector_info` und `geo_python_run` — alle drei hat es nicht. Nach 600 Sekunden stand
  nichts. Der Skill hat nicht nur Vorspann gekostet, er hat **gezielt**: 28 Sekunden
  nach `connect-data` (einem Rezept der Datenphase) kam der Aufruf eines Datenwerkzeugs.

Der Grund ist strukturell, nicht handwerklich: **Ein Skill ist ein Rezept für die
ganze Kette.** 8 von 9 nennen Werkzeuge aus zwei bis vier Ressorts. Der Orchestrator
kann keines lesen, weil er keine Werkzeuge hat; ein Ressort kann keines lesen, weil
ihm drei Viertel davon nicht gehören.

**Wohin das Wissen stattdessen geht: in den Werkzeugtext.** Er steht in jedem Aufruf
des zuständigen Ressorts, kostet keinen Ladevorgang und nennt einen Namen, den es
gibt. Der Fall, der das ausgelöst hat — aus „Supermärkte im 10-Minuten-Gehbereich“
wurde ein 800-Meter-Luftlinienpuffer — steht jetzt bei `service_area`: *„A travel time
is never a buffer.“*

*Aufgehoben, nicht verworfen:* Was ein Skill hat und kein Werkzeugtext haben kann, ist
die **Reihenfolge der Schritte**. Die ist im Team konstruktionsgemäß Sache des
Orchestrators. Zeigen die Vergleichsläufe, dass dem Team fachliches *Vorgehen* fehlt
und nicht Werkzeugwahl, dann ist die Antwort eine zweistufige Skill-Form — Ziele für
den Orchestrator ohne Werkzeugnamen, Phasenstücke für die Ressorts. Das sind neun
Dateien Arbeit und eine zweite Pflegefläche; dafür will ich erst die Messung.

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
   `open_points`, `needs` (siehe unten) — und bei einem Deckel `capped: true`, bei
   einem Fehler `ok: false`
   samt `error`. **Es wirft nie**; ein gescheitertes Ressort darf den Lauf des Teams
   nicht mitreißen. Antwortet das Modell statt der Struktur in **Prosa**, gilt der Text
   als Bericht: Ein lokales Modell verlor am 20.09. dreimal hintereinander eine
   getane Arbeit an der Formvorschrift, und die Pfade stehen ohnehin in den
   Werkzeug-Rückgaben.
5. Der Orchestrator gibt die Pfade weiter, beschafft einen gemeldeten Bedarf, oder
   antwortet.
6. Das **Gate** prüft die Dateien, die der Lauf erzeugt *und* die Antwort nennt.

### Der Rückkanal: ein Ressort darf etwas anfordern

Die Kette läuft nicht nur vorwärts. `needs` nennt **Bedingungen, die ein Ressort für
seine Aufgabe braucht und selbst nicht herstellen kann** — Ebenen in einem gemeinsamen
CRS, eine Grenze, die es nicht bekommen hat, Daten, die noch nicht auf der Platte
liegen. Der Orchestrator beschafft sie und ruft das fragende Ressort erneut.

Drei Festlegungen tragen das (21.09.2026):

- **Eine Bedingung, kein Rezept.** „Beide Ebenen in einem metrischen CRS“, nicht „ruf
  `vector_reproject`“. Das ist die **Spiegelung der Regel, die der Orchestrator hat**:
  Er verteilt Ziele, weil er die Werkzeuge der Ressorts nicht kennt — und ein Ressort
  kennt die der anderen ebensowenig. Derselbe Fehler in der Gegenrichtung ist schon
  gemessen: Der Orchestrator reichte `qgis_service_area` weiter und kostete damit einen
  ganzen Lauf.
- **Anfordern ist kein Scheitern.** Die Rückgabe bleibt `ok: true`, auch wenn nichts
  entstanden ist — sonst liest sich eine richtige Antwort wie ein Absturz. Was möglich
  war, wird trotzdem getan; blockiert der Bedarf alles, kommt die Rückgabe sofort.
- **`needs` ist nicht `open_points`.** Ein Bedarf **blockiert** diese Aufgabe, ein
  offener Punkt ist ein Zweifel an getaner Arbeit. Das Modell vermischt beides, wenn
  man es nicht trennt.

Gegen ein Endlospendeln steht eine Regel im Orchestrator-Text: Derselbe Bedarf zum
zweiten Mal heißt, dass er so nicht zu beschaffen ist — dann ein anderer Weg oder eine
ehrliche Antwort, kein dritter Versuch.

**Der Rückkanal ist zugleich der Melder für Leihgaben.** `needs` steht in jeder
Zeile von `ressort-calls.jsonl`, ist also zählbar. Ein Bedarf, der immer wieder
auftaucht, sollte keine Dauerschleife über den Orchestrator sein, sondern ein
geliehenes Werkzeug (`LENT`) — genau so ist `vector_reproject` beim Ausgabe-Ressort
gelandet, nur dass ich dafür ein Protokoll von Hand lesen musste. Umgekehrt gilt: Was
selten gebraucht wird, bleibt Rückfrage und vergrößert keinen Vorspann.

### Verworfen: Unter-Ressorts

Ein Ressort, das selbst Unter-Ressorts verteilt, wurde am 21.09.2026 geprüft und
verworfen. Jede Ebene zahlt ihren eigenen Vorspann bei **jedem** Aufruf (`data`:
28.109 Zeichen ≈ 7.000 Token ≈ 35 s Prefill bei gemma4), verengt die Übergabe ein
zweites Mal — das Gate sieht schon heute nur Zusammenfassungen — und bräuchte eine
dritte Kennzahl, damit die Messung noch etwas aussagt. Der einzige Fall, für den man
es bauen wollte, wäre `data` wieder in Finden und Holen zu teilen; das sind dann
besser Geschwister als eine Unterebene.

Wo Unter-Agenten tragen, ist eine **andere Form**: Fächerung über Daten statt
Spezialisierung — dieselbe Aufgabe, verschiedene Eingaben, kein gemeinsames Urteil
(zwölf Kacheln, fünf Stadtbezirke). Dann entfällt der Übergabeverlust, die Ebenen
laufen gleichzeitig, und SelmaKits `delegate_task` gäbe es dafür schon. Offen, nicht
verworfen.

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

**Schon gemessen (21.09.2026):** Instruktionen je Ressort — Vektor 10.893, Raster
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
