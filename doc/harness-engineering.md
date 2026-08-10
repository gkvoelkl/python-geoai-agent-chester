# Harness Engineering in Chester

Wie dieses Projekt gebaut wird — nicht, wie Chester funktioniert.

## 1. Was ist Harness Engineering?

Der Begriff stammt aus dem OpenAI-Artikel
[*Harness engineering: leveraging Codex in an agent-first world*](https://openai.com/index/harness-engineering/)
(Ryan Lopopolo, 11.02.2026). Ein Team baute darin über fünf Monate ein internes
Produkt mit **null manuell geschriebenen Codezeilen** — rund eine Million Zeilen,
~1.500 PRs, drei bis sieben Engineers.

Der Begriff stammt also aus einem Erfahrungsbericht. Die **systematische** Fassung
lieferte kurz darauf Birgitta Böckeler (Thoughtworks) mit
[*Harness engineering for coding agent users*](https://martinfowler.com/articles/harness-engineering.html)
(02.04.2026) — eine Taxonomie, die in §2 als Ordnungsraster dient. Beide zusammen:
OpenAI liefert den Beleg, Böckeler den Rahmen.

Die These: Wenn Agenten den Code schreiben, verschiebt sich die Ingenieursarbeit vom
Code auf das **Umfeld** — Werkzeuge, Abstraktionen, Feedback-Schleifen. Die knappe
Ressource ist menschliche Aufmerksamkeit, nicht Tippgeschwindigkeit. Der Mensch
steuert (Absicht, Architektur, Bewertung), die Agenten führen aus.

Verwandt ist der [Ralph-Loop](https://ghuntley.com/loop/) von Geoffrey Huntley: ein
Agent, ein Repo, **eine Aufgabe pro Durchlauf**, bei jeder Iteration frischer Kontext.
Der Zustand liegt als Datei im Repository (Pläne, Fortschrittsprotokolle), nicht im
Kontextfenster — das umgeht Context Rot.

Der Leitsatz, an dem sich alles Weitere ausrichtet:

> Was der Agent nicht mechanisch merkt, hält er nicht ein.
> Regeln in `CLAUDE.md` sind Bitten. Hooks und Tests sind Gesetze.

### Ein Begriff, zwei Ebenen

**Harness ist die Infrastruktur eines Agenten — das Drumherum.** Ein Sprachmodell
allein tut nichts; erst Werkzeuge, Regeln, Kontext und Prüfungen machen daraus einen
Agenten, der arbeiten kann. Genau diese Hülle ist das Harness. Böckeler formuliert
dieselbe Definition als Gleichung: *„The term harness has emerged as a shorthand to
mean everything in an AI agent except the model itself — Agent = Model + Harness."*

Sie fügt eine Präzisierung hinzu, die hier zählt: Bei einem Coding-Agenten ist ein
Teil des Harness **schon eingebaut** (Systemprompt, Code-Retrieval, Orchestrierung);
was der Nutzer beisteuert, ist ein **äußeres** Harness für den eigenen Anwendungsfall.
Phase H baut also nicht Claude, sondern die äußere Schale um Claude herum.

Der Begriff gilt in diesem Projekt auf zwei Ebenen — nicht zwei verschiedene Dinge,
sondern derselbe Gedanke, einmal als Produkt und einmal als Werkzeug:

| | **Chesters Harness** | **Claudes Harness** |
|---|---|---|
| Agent | Chester (das Sprachmodell in der Agent-Schleife) | Claude Code (der Coding-Agent) |
| Das Drumherum besteht aus | QGIS-Werkzeuge, Daten-Connectoren, Skills, `chester/gate.py`, Plausibilitätsbänder | `CLAUDE.md`, `doc/`, `tests/`, Lint-Hooks, Review-Subagenten, CI |
| Rolle im Projekt | **das Produkt** — es wird hier gebaut | **das Werkzeug** — damit wird gebaut |
| Wer legt es fest? | der Entwickler, über Code | der Entwickler, über Regeln und Feedback-Schleifen |

**„Harness Engineering" ist die Vorgehensweise auf der rechten Seite:** Der Entwickler
programmiert nicht direkt, sondern legt das Drumherum des Software-Agenten Claude fest
— und erzeugt das Programm dadurch. Dieses Dokument beschreibt die rechte Spalte.
Chesters eigenes Harness ist in [`features.md`](./features.md),
[`geodata-concept.md`](./geodata-concept.md) und
[`validation-concept.md`](./validation-concept.md) beschrieben.

Die Symmetrie ist kein Zufall, sondern nutzbar: Was sich beim Bau von Chesters Harness
bewährt hat, taugt oft auch für Claudes Harness. `chester/gate.py` etwa löst das
Problem „Prüfung erzwingen, ohne in eine Endlosschleife zu geraten" — dasselbe Problem
stellt sich bei einem hart verdrahteten Review (§5).

`tests/`, `agent-test-prompts.jsonl` und `evals.py` gehören in **beide** Spalten: Sie
sichern Chesters Qualität und sind zugleich die Rückmeldung, die Claude beim Bauen
braucht.

> **Gleichnamig, aber unbeteiligt:** `pydantic-ai-harness` ist ein Paket (das
> `selmakit[subagents]`-Extra), und `chester/resources/qgis_python_harness.py` ist der
> Ausführungsrahmen für PyQGIS-Schnipsel. Beide haben mit diesem Dokument nichts zu tun.

## 2. Welche Aufgaben gehören dazu?

### Ordnungsraster

Birgitta Böckeler (Thoughtworks) hat den Gegenstand
[systematisch geordnet](https://martinfowler.com/articles/harness-engineering.html)
— eine Taxonomie statt eines Erfahrungsberichts. Drei Unterscheidungen, die beim
Sortieren helfen und weiter unten benutzt werden:

**Guides vs. Sensoren.** *Guides* wirken **bevor** der Agent handelt (Feedforward):
Doku, Regeln, Skills, Vorlagen — sie erhöhen die Trefferquote im ersten Anlauf.
*Sensoren* wirken **danach** (Feedback): Sie beobachten das Ergebnis und lösen
Selbstkorrektur aus. Für Sensoren gilt: Das Signal muss für LLM-Konsum taugen —
dieselbe Einsicht wie „die Fehlermeldung ist ein Prompt".

**Computational vs. inferentiell.** *Computational* heißt deterministisch, CPU,
Millisekunden — Linter, Typprüfer, Strukturtests; das Ergebnis ist verlässlich.
*Inferentiell* heißt semantisch, per Modell, teuer und nicht-deterministisch — Review-
Agent, LLM-as-Judge. Daraus folgt die Platzierung: computational oft und früh,
inferentiell selten und spät.

**Drei Regelungsdimensionen** — das brauchbarste Diagnoseraster:

| Dimension | Was geregelt wird | Einschätzung im Original |
|---|---|---|
| **Maintainability** | Duplikate, Komplexität, Stil, Coverage, Struktur-Drift | *„at the moment the easiest type of harness, as we have a lot of pre-existing tooling"* |
| **Architecture Fitness** | Architektureigenschaften — *„Basically: Fitness Functions"*; Performance-Anforderungen, Logging-Standards | keine Reifeaussage getroffen |
| **Behaviour** | funktionale Korrektheit | *„This is the elephant in the room"* |

Zur letzten Zeile beschreibt sie den üblichen Stand — Feedforward: „A functional
specification"; Feedback: „Check if the AI-generated test suite is green" — und
urteilt: *„This approach puts a lot of faith into the AI-generated tests, that's not
good enough yet."* Ihr Fazit: *„we still have a lot to do to figure out good harnesses
for functional behaviour"*.

Das ist für Chester mehr als eine Randnotiz: Die Eval-Bank arbeitet mit **menschlich
geschriebenen Rubriken** pro Testfall statt mit KI-generierten Tests, und
`chester/gate.py` ist ein Behaviour-Sensor für eine Domäne, in der „richtig oder
falsch" objektiv entscheidbar ist. Chesters eigenes Harness liegt damit ausgerechnet
in dem Feld, das die Taxonomie als offen bezeichnet.

Dass zwei unabhängige Quellen — ein Erfahrungsbericht mit n=1 und eine Taxonomie —
bei denselben Punkten landen (Signale für LLM-Konsum optimieren, Struktur maschinell
prüfen, Doku als Guide, Agent-Review als teure Ausnahme), erhöht das Vertrauen in
genau diese Punkte.

### Was Sensoren nicht leisten

Böckeler prüft ihre eigenen Beispiele gegen einen Katalog typischer Agenten-Fehler und
kommt zu einem ernüchternden Befund. Computational zuverlässig erkannt wird das
Strukturelle: *„duplicate code, cyclomatic complexity, missing test coverage,
architectural drift, style violations"*. Inferentiell teilweise erkannt wird das
Semantische: *„semantically duplicate code, redundant tests, brute-force fixes,
over-engineered solutions"* — aber teuer und probabilistisch, also nicht bei jedem
Commit. **Keines von beidem** erfasst verlässlich die folgenschwersten Fälle:
*„Misdiagnosis of issues, overengineering and unnecessary features, misunderstood
instructions."* Und der Satz, der die Grenze markiert:

> *„Correctness is outside any sensor's remit if the human didn't clearly specify
> what they wanted in the first place."*

Ein Harness ersetzt also keine klare Aufgabenstellung — es verhindert das Abrutschen
darunter. Wer das Ziel falsch formuliert, bekommt ein sauber formatiertes,
schichtentreues, getestetes Programm, das die falsche Sache tut.

### Was sich in der Praxis bewährt hat

Böckeler hat ihre eigene Taxonomie zwei Monate später an einer echten Anwendung
erprobt und
[berichtet](https://martinfowler.com/articles/sensors-for-coding-agents.html), was
trägt. Vier Ergebnisse, die den Plan in §4 unmittelbar geformt haben:

**Vier KI-Fehlermuster sind billig abfangbar** — Argumentanzahl, Dateilänge,
Funktionslänge, zyklomatische Komplexität. Wichtiger Zusatz: *„these weren't even
active in ESLint's default preset, I had to configure maximums for them first."* Ein
Standard-Linter schweigt also genau zu den Mustern, die Agenten produzieren.

**Ein Ventil macht Schwellwerte erst brauchbar.** Der historische Grund, warum
statische Analyse liegenblieb, ist Verwaltungsaufwand: Warnungen, die man *nicht immer*
beheben will, verkommen zu Lärm. Ihre Lösung nutzt aus, dass ein Agent abwägen kann —
er darf eine Warnung unterdrücken, **muss aber den Grund in den Code schreiben**
(`-- (give reason why)`), und er darf einen Schwellwert **leicht anheben**, wenn ein
Refactoring unnötig oder unmöglich ist. Der Wert bleibt im Diff sichtbar, und die Regel
feuert erneut, sobald es schlimmer wird. Das ist besser als eine starre Ratsche:
Ausnahmen werden nicht verhindert, sondern sichtbar und überprüfbar gemacht.

**Kopplungsdaten allein nützen dem Agenten nichts.** Rohe Aufruf- und Importgraphen
waren zu verrauscht — *„I don't have the impression that this type of coupling data is
useful to AI on its own"*; was „angemessen" ist, hängt am Kontext. Nützlich sind sie
eher zur **Risiko-Triage im Review** (eine Datei mit 10+ Aufrufern verdient mehr
Aufmerksamkeit). Also keine Kopplungsmetriken bauen.

**Der KI-Modularitätsreview findet echte Schulden.** Sie ließ ihn erst spät laufen und
fand *„some quite concerning and very valid findings"*; ihr Fazit: ohne menschliches
Review **und** ohne diesen KI-Review häufte der Agent unbemerkt technische Schulden an.
Strukturregeln über Importe und Ordner (dependency-cruiser & Co.) wirken, *„but they
can only go so far"* — sie greifen nur, was sich über Importe, Dateinamen und
Ordnerstruktur ausdrücken lässt. Für Modularität braucht es die semantische Deutung
eines Modells.

Zwei Warnungen aus demselben Bericht:

- **Sensorkonflikte.** `max-lines` gegen `max-lines-per-function` verlagerte Komplexität
  nur, statt sie zu senken: *„More trade-offs like that are probably lurking."*
- **Scheinsicherheit.** *„I can't help but wonder if this can also lead to a false sense
  of security and an illusion of quality."* Jeder neu aktivierte Regelsatz brachte
  „a mix of irrelevant things and things that actually matter" zutage.

### Der Steuerkreis

Die eigentliche menschliche Tätigkeit beschreibt Böckeler als Iteration am Harness
selbst: Tritt ein Problem **mehrfach** auf, gehört nicht das Problem behoben, sondern
der Guide oder Sensor verbessert, der es künftig unwahrscheinlich macht. Das ist
dieselbe Praxis, die aus dem OpenAI-Bericht als „jeder Stolperstein wird zu einer
Zeile Doku oder einer Regel" hervorgeht — hier als Regelkreis formuliert.

Wichtig für den Aufwand: **Das Harness darf man vom Agenten bauen lassen.** Coding-
Agenten verbilligen genau diese Arbeit — Strukturtests schreiben, Regeln aus
beobachteten Mustern ableiten, Linter gerüstartig anlegen, How-to-Guides aus dem
Code rekonstruieren.

### Die sechs Aufgabenfelder

Unabhängig vom konkreten Projekt; in Klammern die Einordnung ins Raster:

**a) Regeln mechanisch erzwingen.** *(Sensor, computational — Maintainability.)*
Formatierung, Basisregeln, Abhängigkeitsrichtung. Technisch ein `PostToolUse`-Hook auf
`Edit|Write`. Zwei Details entscheiden über Wirkung oder Wirkungslosigkeit:

1. **`exit 2`, nicht `exit 1`.** Exit-Code 1 gilt als nicht-blockierender Fehler und
   wird ignoriert. Nur Exit-Code 2 blockiert *und* speist stderr in den Agentenkontext.
2. **Die Fehlermeldung ist ein Prompt.** Sie landet im Kontext, also gehört die
   Reparaturanweisung hinein — nicht nur „Verstoß in Zeile 42".

**b) Struktur prüfen, nicht nur Verhalten.** *(Sensor, computational — Architecture
Fitness.)* Tests, die Aussagen über den *Quelltext* treffen: Dateigrößen,
Namenskonventionen, erlaubte Importkanten, „jede Capability hat `get_instructions`".
Bei OpenAI sinngemäß als *tests asserting code structure* und eine Obergrenze von 350
Zeilen je Datei beschrieben — nur über Sekundärquellen belegt, siehe §6. Der
etablierte Name dafür ist **Fitness-Funktion**
(Evolutionary Architecture); im Java-Umfeld ist **ArchUnit** das bekannte Werkzeug —
in Python genügen wenige Zeilen über den AST. Die Kategorie, die klassische Linter
nicht abdecken und die am billigsten zu haben ist.

**c) Agent-zu-Agent-Review.** *(Sensor, inferentiell — alle drei Dimensionen.)* Ein
Subagent mit **eigenem Kontextfenster** sieht das Ergebnis, nicht die Begründungskette
des Autors — und erbt dessen blinde Flecken nicht. Mehrere schmale Reviewer
(Architektur, Konventionen, Security) schlagen einen breiten. Weil inferentiell und
damit teuer: selten und spät, nicht bei jedem Edit.

**d) Doku und Skills als Guide.** *(Feedforward — der einzige Hebel, der Fehler
verhindert statt sie zu melden.)* Was der Agent nicht im Repo lesen kann, existiert für
ihn nicht. Eine Chat-Diskussion, die ein Muster festgelegt hat, ist für ihn so unsichtbar
wie für einen Kollegen, der drei Monate später anfängt. Bewährt hat sich eine **kurze
Landkarte** (~100 Zeilen) plus ein strukturierter `doc/`-Baum als eigentliche Quelle;
eine große Allzweckdatei ist gescheitert (Kontext ist knapp — wenn alles wichtig ist,
ist nichts wichtig). Dazu **Skills** für wiederkehrende Arbeitsgänge: Nur Name und
Beschreibung liegen im Kontext, der Rumpf wird bei Bedarf geladen.

**e) Verifikationsschleife.** *(Sensor — Behaviour, die unreifste Dimension.)* Der
Agent muss seine Änderung *prüfen* können, statt sie zu behaupten: schnelle Tests,
schnelle CI, erreichbare Logs.

**f) Pflege und Drift-Überwachung.** *(Dritte Zeitstufe, neben „schnell vor dem
Commit" und „teuer danach".)* Das Harness wird nicht entworfen, es lagert sich ab. Drei
Praktiken: jeder Stolperstein wird zu einer Zeile Doku oder einer Regel; periodische
Aufräum-Läufe („garbage collection day"), die das Repo auf Inkonsistenzen absuchen; und
laufende Drift-Beobachtung — toter Code, Qualität der Testabdeckung, Abhängigkeiten.

## 3. Was in Chester bereits vorhanden ist

Chester wird seit Beginn agentisch entwickelt; Teile des Harness sind dabei beiläufig
entstanden, ohne so genannt zu werden.

| Aufgabenfeld | Vorhanden | Bewertung |
|---|---|---|
| **Doku als System of Record** | `CLAUDE.md` (Landkarte, Architekturregeln, Code-Map jedes Moduls) und `TODO.md` (Phasenplan) — beide **bewusst nicht veröffentlicht**, siehe unten; dazu der `doc/`-Baum (Design-Entscheidungen, Konzepte) | inhaltlich stark, aber `CLAUDE.md` ist mit **844 Zeilen** rund achtmal so lang wie die empfohlene Landkarte |
| **Verhaltenstests** | 38 Dateien in [`tests/`](../tests/), `pytest`; Netz- und LLM-Tests opt-in, QGIS-Tests skippen automatisch | gut, läuft aber nirgends automatisch |
| **End-to-End-Verifikation** | `agent-test-prompts.jsonl` + `evals.py` (LLM-bewerteter Benchmark, `--gate` für CI), `chester/evalhistory.py`, `test_app.py` | entspricht dem „Agent-SDK im Testcode" aus dem Artikel — hier ist Chester **weiter** als die Vorlage |
| **Konventionen schriftlich** | Sprachregelung (Docs Deutsch / Code Englisch), „`chester/*.py` ohne SelmaKit-Abhängigkeit", „alle Pfadargumente durch `resolve_path`", „schreibende Tools stempeln einen Provenance-Sidecar" | die Regeln existieren — aber nur als Prosa, niemand prüft sie |
| **Regeln mechanisch erzwingen** | — | fehlt: kein `ruff`, kein Formatter, kein Hook (`.claude/` enthält nur `settings.local.json` mit Berechtigungen) |
| **Strukturtests** | — | fehlt vollständig |
| **Agent-Review** | — | fehlt; auch mangels Grundlage: das Repo hat **0 Commits**, es gäbe keinen Diff zu prüfen |
| **CI** | — | fehlt vollständig |
| **Pflege** | — | keine geplanten Aufräum-Läufe, kein Tech-Debt-Verzeichnis |

**Kurzfassung:** Die *Wissens*-Hälfte des Harness ist überdurchschnittlich gut
ausgebaut, die *Durchsetzungs*-Hälfte fehlt ganz.

### Harnessability: wie gut lässt sich Chester überhaupt einspannen?

Böckeler führt dafür einen eigenen Begriff ein — nicht jede Codebasis ist gleich gut
harnessbar:

> *„A codebase written in a strongly typed language naturally has type-checking as a
> sensor; clearly definable module boundaries afford architectural constraint rules …
> Without those properties, those controls aren't available to build."*

Ihr Kollege Ned Letcher nennt das **ambient affordances** — *„structural properties of
the environment itself that make it legible, navigable, and tractable to agents"*. Und
sie benennt die unangenehme Asymmetrie: Auf der grünen Wiese lässt sich Harnessbarkeit
von Tag eins einbacken, im Bestand nicht — *„the harness is most needed where it is
hardest to build."*

Für Chester gemessen statt geschätzt:

| Eigenschaft | Stand | Bewertung |
|---|---|---|
| **Typannotationen** | 83 % der 427 Funktionen in `chester/` vollständig annotiert (`capabilities/` sogar 93 %) | hoch — pydantic-ai *verlangt* Annotationen für Tool-Funktionen, das Framework hat den Preis längst bezahlt |
| **Modulgrenzen** | scharf und schriftlich: reiner Kern (`chester/*.py`) → Capabilities → Runtime; LLM-freie CLIs daneben | hoch — Importkontrakte sind ableitbar, nicht zu erfinden |
| **Dateigrößen** | 4 Module über 700 Zeilen, Spitze 1.730 | niedrig — erschwert gezielte Änderungen |
| **Fachliche Prüfbarkeit** | Geometrie, CRS, Fläche sind objektiv nachrechenbar | sehr hoch — seltener Vorteil, den die meisten Codebasen nicht haben |

Chester ist also **besser harnessbar als angenommen**. Das hat unmittelbar eine frühere
Entscheidung gekippt — siehe die Typprüfung in Schritt 1.

### Probelauf der vier Regeln

Böckelers vier KI-Fehlermuster (siehe §2) einmal an Chester gemessen — in ruff heißen
sie `PLR0913` (Argumentanzahl), `PLR0915` (Funktionslänge), `C901` (Komplexität), bei
Schwellen 6 / 60 / 10:

| | Treffer |
|---|---|
| gesamt | **60** |
| davon in `get_toolset` | **11 — Artefakt, kein Befund** |
| echte | **49** |

Der Ausreißer ist die eigentliche Lehre. `get_toolset` erreicht in
`capabilities/discovery.py` **Komplexität 115 und 428 Statements** — nicht weil die
Funktion verworren wäre, sondern weil sie der Container ist, in dem alle Tools einer
Capability als verschachtelte Defs liegen. Das ist Chesters Architektur, kein Mangel.
**`get_toolset` braucht also eine Ausnahme, sonst ist die Regel Lärm** — genau der
„mix of irrelevant things and things that actually matter", vor dem der Bericht warnt.
Ohne diesen Probelauf wäre das erst im Betrieb aufgefallen, und zwar als Fehlerwand am
ersten Tag.

Die 49 echten verteilen sich unauffällig (`citymodel.py`, `capabilities/validation.py`,
`capabilities/mapoutput.py` je 6). Spitzenreiter ist `register_geo_commands` in
`agent_build.py` (Komplexität 36, 108 Statements).

Im Raster aus §2 gelesen fällt eine zweite Asymmetrie auf: Chester hat **neun Skills
für seinen Laufzeit-Agenten, aber keinen einzigen für den Coding-Agenten**
(`.claude/` enthält nur Berechtigungen). Auf der Guide-Seite steht damit allein
`CLAUDE.md` — und Schritt 3 macht die sogar kleiner. Feedforward ist der einzige
Hebel, der Fehler *verhindert*, statt sie zu melden; deshalb hat Schritt 1 einen
Guide-Teil bekommen (H1b).

### Interne und veröffentlichte Unterlagen

Ein Teil des Harness ist **bewusst nicht Teil des veröffentlichten Artefakts** — es
sind Arbeitsunterlagen für den Coding-Agenten auf dieser Maschine, nicht Ergebnis:

| Ort | Inhalt | Status |
|---|---|---|
| `internal/` | `TODO.md` (Phasenplan), `zielgruppe-zukunft-tutorial.md` (Positionierung) | ignoriert, als **Verzeichnis** |
| `CLAUDE.md` (Wurzel) | Landkarte + Konventionen | ignoriert, einzeln |
| `doc/` | 15 Konzept- und Designdokumente | veröffentlicht |

Der Ausschluss greift auf Verzeichnisebene, nicht pro Datei — sonst ist jede neue
Notiz so lange veröffentlicht, bis jemand an eine `.gitignore`-Zeile denkt. Genau so
lag `zielgruppe-zukunft-tutorial.md` zunächst in `doc/`: intern gemeint, öffentlich
konfiguriert. `CLAUDE.md` ist die unvermeidliche Ausnahme — Claude Code lädt sie nur
aus dem Projektwurzelverzeichnis.

Zwei Folgerungen:

- **Kein öffentliches Dokument darf nach `internal/` oder auf `CLAUDE.md` verlinken**
  (dieses hier nennt beide deshalb ohne Link). Der Doc-Linter aus Schritt 3 prüft nicht
  nur, ob ein Ziel existiert, sondern auch, ob ein öffentliches Dokument über die
  Grenze zeigt — ein toter Link für jeden, der das Repository klont.
- Was dauerhaft und zitierbar sein soll — insbesondere die Code-Map aus `CLAUDE.md` —
  gehört nach `doc/`. Schritt 3 erledigt das ohnehin.

## 4. Was geplant ist

Die Reihenfolge folgt einer Logik: Jeder Schritt macht den nächsten erst möglich. Ohne
Historie gibt es keinen Diff, den ein Reviewer lesen könnte. Und die Prosa in
`CLAUDE.md` darf erst weichen, wenn Hook und Tests die darin beschriebenen Regeln
mechanisch tragen — sonst löscht man die Regel mitsamt ihrer Durchsetzung.

### Schritt 0 — Git-Historie + minimale CI  · ~1 h

Ein Baseline-Commit des jetzigen Stands (rückwirkend thematisch aufzuteilen lohnt bei
13.357 Zeilen nicht). Dazu `.github/workflows/ci.yml`: `uv sync` + `uv run pytest`.
Die Netz- und LLM-Tests bleiben opt-in, QGIS-Tests skippen ohne QGIS von selbst — die
CI läuft also auch auf einem nackten Runner grün.

*Voraussetzung für alles Weitere:* ohne Commit kein `git diff`, ohne Historie kein
Rollback, wenn ein Agent etwas kaputt macht. Der fehlende Commit ist intern schon
länger als Veröffentlichungs-Blocker vermerkt — hier wird er zusätzlich zur
technischen Voraussetzung.

#### Entscheidung: lokal prüfen, auf GitHub nur reproduzieren  *(2026-08-09)*

Der naheliegende Einwand lautet: *Wenn die aussagekräftigen Schichten auf einem
GitHub-Runner ohnehin nicht laufen — warum dann nicht ganz lokal prüfen?* Er ist zur
Hälfte berechtigt, und die Auflösung liegt darin, dass „CI" **zwei** Dinge bündelt:

- **Sensor** (`ruff`, `mypy`, `pytest`, Doc-Linter) — findet lokal exakt dasselbe,
  nur schneller und ohne Push. Hier hat der Einwand recht.
- **Unabhängige Umgebung** — sauberer Checkout, fremder Rechner, ohne Zutun des
  Entwicklers. Das ist lokal **strukturell unmöglich**.

Für Chester wiegt das Erste schwer: QGIS, Ollama und die Netz-Connectoren existieren
auf einem Runner nicht, also laufen dort weder die QGIS-Integration noch die
Connectoren noch die Eval-Bank. Ein grüner Haken bestätigt die *am wenigsten*
interessante Schicht — „grüne CI, kaputtes Projekt" ist hier ein realistisches
Ergebnis, kein Schreckgespenst.

Was nur der Runner beantwortet, ist dafür genau die Frage, die für ein
**zitierfähiges Artefakt** zählt: *Funktioniert das auf einem Rechner, der nicht
deiner ist?* `uv sync --frozen` auf nackter Infrastruktur fängt die Fehlerklasse, die
lokal per Definition unsichtbar bleibt — eine Abhängigkeit, die nur im eigenen venv
liegt, aber nicht in `pyproject.toml` steht; eine nie committete Datei; ein Test, der
heimlich an `.chester/`-Zustand oder an einem absoluten Benutzerpfad hängt; eine
Darwin-Annahme; ein `uv.lock`, der sich nicht mehr auflösen lässt. Genau darauf zielt
auch Böckelers Formulierung: *„The CI pipeline confirms the result on **clean
infrastructure**"* — nicht „findet mehr Fehler".

**Daraus die Arbeitsteilung:**

| | läuft | Aufgabe |
|---|---|---|
| Hook + ein `check`-Einstiegspunkt | lokal | `ruff`, Strukturtests, QGIS-Tests, Connectoren, Eval-Bank — die Fachprüfung, dort wo die Umgebung existiert |
| **ein einziger** GitHub-Job | Runner | `uv sync --frozen` + `ruff check` + die QGIS-freien Unit-Tests — ausschließlich der Reproduzierbarkeitsnachweis |

Zwei Auflagen, ohne die der Nutzen wieder verpufft:

1. **Ein Einstiegspunkt für „die Prüfungen laufen"** — ein Skript, das Hook, Mensch
   und Agent gleichermaßen aufrufen. Sonst entstehen drei Definitionen von „grün".
2. **Der Zweck gehört in den Kopf der Workflow-Datei** — *„prüft nicht die
   Fachlogik, sondern dass ein fremder Rechner das Repo aus dem Lock bauen kann."*
   Sonst wird der grüne Haken später als Qualitätsaussage missverstanden, vom Autor
   in sechs Monaten wie von einem Gutachter.

Fiele die Zitierbarkeit als Ziel weg, wäre die GitHub-CI hier reines Ritual und
gehörte gestrichen. Sie fällt nicht weg (Zenodo-DOI, Konferenzweg) — deshalb:
behalten, aber auf diese eine Aufgabe zusammengeschrumpft, nicht als
„Qualitätssicherung" geführt.

### Schritt 1 — Lint-Hook + Strukturtests  · ein Nachmittag

`.claude/settings.json` mit einem `PostToolUse`-Hook auf `Edit|Write` →
`.claude/hooks/lint.sh`: liest den Dateipfad aus dem stdin-JSON, ruft `ruff format` und
`ruff check --fix` **nur auf dieser Datei** und meldet Restverstöße auf stderr mit
`exit 2` und Reparaturanweisung im Text.

Zum Regelsatz gehören neben `E`/`F`/`I` die **vier KI-Fehlermuster** aus §2 —
`PLR0913` (Argumentanzahl), `PLR0915` (Funktionslänge), `C901` (Komplexität), dazu die
Dateilänge über Regel 5 unten. Zwei Dinge dabei, beide aus dem Probelauf in §3:

- **`get_toolset` wird ausgenommen** (`per-file-ignores` oder eine `noqa`-Zeile je
  Capability). Dort liegen alle Tools als verschachtelte Defs, die Metrik misst also
  die Capability, nicht eine Funktion — 11 der 60 Treffer sind reines Artefakt.
- **Das Ventil:** Der Agent darf eine Warnung unterdrücken, **muss aber den Grund
  danebenschreiben**, und einen Schwellwert leicht anheben, wenn ein Refactoring
  unnötig oder unmöglich ist. Beides bleibt im Diff sichtbar und feuert erneut, wenn es
  schlimmer wird. Das ist der Unterschied zwischen einer Regel, die gepflegt wird, und
  einer, die man nach zwei Wochen abschaltet.

Dazu Strukturtests als ganz normale `pytest`-Fälle in `tests/` — bewusst **statt**
`import-linter`: je ~20 Zeilen über den AST, keine neue Abhängigkeit, keine zweite
Konfigurationsdatei, und sie laufen in der CI aus Schritt 0 automatisch mit.

Kandidaten, alle bereits als Prosa in `CLAUDE.md` vorhanden:

1. `chester/*.py` (Kernmodule) importiert kein `selmakit`/`pydantic_ai` — dokumentierte
   Ausnahme: `gate.py` (braucht `ModelRetry`).
2. `chester/*.py` importiert nicht `chester.capabilities` (Richtung nur andersherum).
3. `data.py`, `evals.py`, `chester/evalhistory.py` importieren nicht `agent_build`
   (die LLM-freien CLIs müssen ohne SelmaKit laufen).
4. Jede Capability erbt `AbstractCapability` und hat `get_instructions`.
5. **Dateigrößen-Ratsche:** keine Datei wächst über ihren heutigen Stand hinaus, neue
   Dateien bekommen ein echtes Limit. Ein harter 350-Zeilen-Cap wie bei OpenAI wäre
   hier ein Monsterrefactoring (`chester/capabilities/discovery.py` hat 1.730 Zeilen,
   `chester/citymodel.py` 1.001) — die Sperrklinke stoppt den Verfall, ohne den
   Bestand anzufassen. Mit demselben Ventil wie oben: anheben erlaubt, aber sichtbar.

Achtung auf **Sensorkonflikte**: Regel 5 (Dateilänge) und `PLR0915`
(Funktionslänge) ziehen in verschiedene Richtungen — im Bericht verlagerten sie
Komplexität, statt sie zu senken. Beim Aktivieren beobachten, nicht blind zuschalten.

Erster Handgriff ist nicht „ich schreibe `lint.sh`", sondern: der Agent schreibt
`lint.sh` und die Strukturtests gegen die Regeln, die schon in `CLAUDE.md` stehen. Im
Artikel waren die Linter ebenfalls agent-generiert.

**Schritt 1c — Typprüfung in der CI (Entscheidung revidiert).** Eine frühere Fassung
dieses Dokuments hatte `mypy` verworfen: „auf 13k bislang ungeprüften Zeilen eine
Fehlerwand, die den Agenten lahmlegt". Die Prämisse war schlicht falsch, wie die
Messung oben zeigt — der Code ist zu 83 % annotiert. Nachgemessen mit
`mypy --ignore-missing-imports`:

- **ganzes Paket `chester/`: 34 Fehler in 12 von 41 Dateien**,
- **die acht reinen Kernmodule: 11 Fehler, 6 der 8 Dateien fehlerfrei.**

Keine Fehlerwand, sondern eine Nachmittagsaufgabe. Inhaltlich zwei Sorten: echte
`union-attr`-Fälle (ein `dict | None`, auf dem ungeprüft `.get` aufgerufen wird — ein
potenzieller `AttributeError`), und ein Artefakt aus `geocache.py`, wo eine Methode
`list()` innerhalb der Klasse den Builtin verdeckt und damit fünf `list[Dataset]`-
Annotationen für statische Werkzeuge unlesbar macht (zur Laufzeit harmlos, dort löst
die Annotation korrekt auf).

Was von der alten Entscheidung **bleibt**: `mypy` gehört **nicht** in den
`PostToolUse`-Hook — dafür ist es zu langsam, und Typfehler an einer halbfertigen Datei
sind Lärm. Was sich **ändert**: `mypy` gehört in die CI aus Schritt 0, mit derselben
Ratschen-Mechanik wie die Dateigrößen — eine eingecheckte Baseline-Zahl, die nicht
wachsen darf. Die reinen Kernmodule werden sofort auf null gebracht und dort scharf
gestellt, der Rest friert auf dem heutigen Stand ein.

Der eigentliche Gewinn ist aber nicht der Sensor, sondern der **Guide**: Eine
annotierte Signatur sagt dem Coding-Agenten, was er schreiben soll, *bevor* er
schreibt. Das ist Harnessbarkeit im Sinne des vorigen Abschnitts — ein Vorteil, den
Chester über pydantic-ai schon bezahlt, bisher aber nicht eingelöst hat.

**Schritt 1b — Skills für den Coding-Agenten.** Alles bisher Genannte sind Sensoren;
das hier ist die Guide-Hälfte und gehört in denselben Arbeitsgang. `.claude/skills/`
für die wiederkehrenden Arbeitsgänge dieses Repos — „eine Capability hinzufügen", „einen
Daten-Connector hinzufügen", „einen Test-Prompt in die Eval-Bank aufnehmen". Sie kosten
kaum Kontext (nur Name und Beschreibung liegen ständig an, der Rumpf wird bei Bedarf
geladen) und verhindern genau die Fehler, die H1 und H2 sonst melden müssen. Vorlage
sind Chesters eigene neun Skills unter `skills/` — dieselbe Form, andere Ebene.

### Schritt 2 — Review-Subagent  · 1–2 h

`.claude/agents/*.md`, schmal geschnitten statt einer breiten Instanz. Grundlage ist
`git diff` (daher Schritt 0 zuerst). Zwei sinnvolle Zuschnitte für dieses Repo:

- **Konventionen** — Sprachregelung Docs-Deutsch/Code-Englisch, Benennung.
- **Tool-Vertrag** — geht ein neues Pfadargument durch `resolve_path`? Stempelt ein
  schreibendes Tool den Provenance-Sidecar und gibt den Output-Pfad im Rückgabe-Dict
  zurück (sonst findet die Validierung das Ergebnis nicht)? Trägt ein neues
  bbox-Feature-Tool die `warning` bei fehlendem `place`?

Das ist genau die Regelklasse, die ein Linter nicht greifen kann, weil sie semantisch
ist — und die hier nachweislich wiederkehrt. Verdrahtung zunächst weich über
`.claude/commands/review.md` (manueller Aufruf), nicht als Stop-Hook.

### Schritt 3 — `CLAUDE.md` verschlanken  · laufend

Die Code-Map — der Löwenanteil der 844 Zeilen — wandert in eine neue Datei
`doc/code-map.md` (**die es noch nicht gibt; sie entsteht in genau diesem Schritt**);
`CLAUDE.md` bleibt als Landkarte mit den harten Regeln bei ~100–150 Zeilen. Nebeneffekt:
Die Code-Map wird damit *öffentlich*, anders als `CLAUDE.md` selbst. Beim Umzug ist die
Altersschichtung zu glätten — die neun jüngeren Capabilities sind ausführlich
beschrieben, die sechs ältesten erscheinen nur als Modulnamen; dokumentiert ist also,
was zuletzt gebaut wurde, nicht was am wichtigsten ist.

Dazu ein Doc-Linter in der CI mit drei Prüfungen: (1) Jeder in `*.md` referenzierte
Pfad existiert — bei einem dicht querverlinkten `doc/`-Baum eine reale Fehlerquelle.
(2) Keine veröffentlichte Datei verlinkt über die Veröffentlichungsgrenze (§3) hinweg.
(3) Jede Datei in `doc/` ist vom `README.md` aus erreichbar — der Umkehrfehler zum
toten Link, ein verwaistes Ziel.

Bewusst zuletzt: Die Code-Map ist echter Navigationswert, kein Ballast. Erst wenn Hook
und Reviewer die Regeln erzwingen, kann man sie gefahrlos aus dem Prompt nehmen —
vorher tauscht man Kontextbudget gegen Regeltreue.

### Laufend — Pflege und Drift-Überwachung

- **Jeder Stolperstein wird zu einer Zeile Doku oder einer Regel.** Kostet nichts,
  braucht keinen Aufbau, und ist der eigentliche Wachstumsmechanismus des Harness.
- **Aufräum-Lauf** in Anlehnung an den „garbage collection day": ein wiederkehrender
  Agentenlauf, der `doc/` gegen den echten Code prüft und Abweichungen meldet.
- **Drift-Beobachtung** als eigene Zeitstufe (Böckelers dritte Kategorie, neben
  „schnell vor dem Commit" und „teuer danach"): toter Code, Qualität der
  Testabdeckung, veraltete Abhängigkeiten.
- **Mutationstests** — kein Nice-to-have, sondern der Sensor auf den Sensor. Chesters
  384 Tests sind größtenteils **agentengeschrieben** und zugleich der Behaviour-Sensor
  des Projekts: eine Prüfinstanz, deren eigene Verlässlichkeit ungeprüft ist. Genau
  davor warnt der Praxisbericht — KI-generierte Tests erhöhen die Abdeckung, sind aber
  „often not very assertion-heavy, giving us a false sense of security in test
  effectiveness — mutation testing helps us monitor that gap". Praktikabel nur für die
  reinen Kernmodule (`geofacts`, `adminlevels`, `plausibility`, `geocache`: schnell,
  deterministisch, kein QGIS) und **inkrementell, von Hand angestoßen** statt laufend
  (auch sie ließ es nicht kontinuierlich mitlaufen).
- **Tech-Debt-Verzeichnis** — bei OpenAI ein eigener `docs/technical-debt/`-Zweig,
  in Chester bislang nicht vorhanden.

## 5. Bewusst nicht übernommen

Der Artikel beschreibt fünf Monate, drei bis sieben Engineers, 1.500 PRs, ein
TypeScript-Monorepo mit 750 pnpm-Paketen, neues Produkt auf grüner Wiese. Chester ist
ein Paket mit 13.357 Zeilen, ein Entwickler, weitgehend fertig
(Forschungsvehikel, kein Produkt). Daraus folgt:

| Nicht übernommen | Begründung |
|---|---|
| **Observability-Stack pro Worktree** (Chrome DevTools Protocol, LogQL/PromQL) | Chester ist kein UI-Produkt. Die Verifikationsschleife heißt hier schlicht `uv run pytest` und `uv run evals.py --gate`. |
| **`mypy` im `PostToolUse`-Hook** | Zu langsam für jeden Edit, und Typfehler an einer halbfertigen Datei sind Lärm. **Nur der Hook ist gestrichen** — in der CI ist die Typprüfung seit der Nachmessung eingeplant (Schritt 1c); die frühere Begründung „Fehlerwand" war widerlegt. |
| **Harter 350-Zeilen-Cap** | Ersetzt durch die Ratsche (Schritt 1, Regel 5). |
| **Stop-Hook mit `exit 2`** (Review hart verdrahtet) | Ohne Zähler oder State-Datei eine Endlosschleife. Falls doch, dann nach dem Muster von `chester/gate.py`: einmal zurückschicken, dann durchlassen. |
| **Package-Privacy / Dependency-Edges als Selbstzweck** | Dort zentral, *weil* es 750 Pakete gibt. Hier genügen drei Importkontrakte. |
| **Self-hosted Runner** (CI mit QGIS + Ollama auf der eigenen Maschine) | Verlockend, weil damit die aussagekräftigen Schichten in der Pipeline liefen. Er erbt aber den Zustand genau der Maschine, deren Zustand geprüft werden soll — der Reproduzierbarkeitsnachweis, der einzige Grund für den Runner (Schritt 0), fiele weg, die beweglichen Teile blieben. Die Fachprüfung läuft stattdessen lokal über den `check`-Einstiegspunkt. |
| **Durchsatzzahlen als Ziel** (3,5 PRs/Engineer/Tag) | Kein sinnvoller Maßstab für ein Ein-Personen-Forschungsprojekt. Der Artikel betont zudem selbst: **wenige blockierende Gates, kurze Wege** — die Gates dürfen nicht schneller wachsen als ihr Nutzen. |

Unverändert gilt dagegen die Latte für Reviews: mindestens dieselbe wie bei
handgeschriebenem Code.

### Wozu das Ganze — die Rolle des Menschen

Böckeler beschreibt, was ein Mensch unausgesprochen mitbringt und ein Agent nicht hat:
*„no social accountability, no aesthetic disgust at a 300-line function, no intuition
that 'we don't do it that way here,' and no organisational memory."* Ein Harness ist
der Versuch, genau das zu externalisieren — und stößt dabei an eine Grenze. Ihr
Schlusssatz ist zugleich das Maß, an dem Phase H sich messen lassen sollte:

> *„A good harness should not necessarily aim to fully eliminate human input, but to
> direct it to where our input is most important."*

Das deckt sich mit Chesters eigener Haltung auf der Produktseite: Auch dort ersetzt die
Validierung nicht das Urteil, sondern setzt einen kompetenten Prüfer am Ende der Kette
voraus. Dieselbe Bescheidenheit, zwei Ebenen.

## 6. Quellen

- [Harness engineering for coding agent users](https://martinfowler.com/articles/harness-engineering.html)
  — Birgitta Böckeler (Thoughtworks), 02.04.2026. **Die Taxonomie** (Guides/Sensoren,
  computational/inferentiell, die drei Regelungsdimensionen); Grundlage von §2.
- [Maintainability sensors for coding agents](https://martinfowler.com/articles/sensors-for-coding-agents.html)
  — dieselbe Autorin, 27.05.2026. **Der Praxisbericht** zur Taxonomie: welche Sensoren
  sie tatsächlich gebaut hat, was trug und was nicht. Quelle für das Ventil, die vier
  KI-Fehlermuster, die Kopplungs-Absage und die Aufwertung der Mutationstests.
- [Harness engineering: leveraging Codex in an agent-first world](https://openai.com/index/harness-engineering/) — OpenAI, 11.02.2026.
  **Der Erfahrungsbericht** (n=1, fünf Monate).
  *(Hinweis: der Artikel liefert bei automatisiertem Abruf HTTP 403; die Details oben
  sind aus drei Sekundärquellen trianguliert.)*
- [ZenML LLMOps Database: Harness Engineering](https://www.zenml.io/llmops-database/harness-engineering-building-software-where-humans-steer-and-agents-execute)
- [Chang Wan: Lessons from OpenAI's Agent-First Development Experiment](https://www.cwblogs.com/posts/harness-engineering-lessons-from-openais-agent-first-development/)
- [ignorance.ai: The Emerging Harness Engineering Playbook](https://www.ignorance.ai/p/the-emerging-harness-engineering)
- [everything is a ralph loop](https://ghuntley.com/loop/) — Geoffrey Huntley, 17.01.2026
- [Claude Code: Hooks](https://code.claude.com/docs/en/hooks) · [Subagents](https://code.claude.com/docs/en/sub-agents)
- [ARCHITECTURE.md als Muster](https://matklad.github.io/2021/02/06/ARCHITECTURE.md.html) — Alex Kladov
