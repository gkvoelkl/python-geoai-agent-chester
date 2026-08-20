# Wie wichtig sind ausgereifte Tools für lokale Standard-Modelle?

> Dieses Dokument erklärt **ein** Vorhaben — im Projekt kurz *die
> Kompensationsfrage* —, und zwar so, dass man es ohne Vorwissen über Chesters
> Innenleben versteht. Es ist ein **Versuchsplan, kein Ergebnisbericht**: Es sagt,
> was gemessen werden soll, mit welchem Aufbau und woran der Plan scheitern würde.
> Der Messapparat, auf dem das aufsetzt — Prompt-Bank, Judge, Tool-Coverage,
> Historie —, steht in [`agent-test-prompts.md`](./agent-test-prompts.md); die
> Prüfschicht, um die es in Stufe H3 geht, in
> [`validation-concept.md`](./validation-concept.md). Der Arbeitsstand (welcher
> Schritt läuft gerade) wird projektintern geführt und steht nicht hier.
>
> **Status: geplant, nichts gemessen (2026-08-20). Nächster Schritt ist der Pilot
> aus §6.**

## 1. Was überhaupt gefragt wird

Ein Geo-Agent besteht aus zwei Teilen: dem **Sprachmodell** und dem **Drumherum**
(Konnektoren, Werkzeugzuschnitt, Instruktionen, Validierungs-Gate). Wenn etwas
nicht funktioniert, gibt es zwei Reflexe: „nimm ein größeres Modell" oder „bau
bessere Werkzeuge".

Die übliche Frage lautet „was ist wichtiger?" — die ist unbeantwortbar und auch
uninteressant. Die nützliche Variante ist:

> **Angenommen, ich habe nur ein kleines Modell auf meinem Laptop — wie weit kann
> ich das mit gutem Werkzeugbau nach oben ziehen?**

Das ist deshalb die richtige Frage, weil sich am Modell nichts ändern lässt (ein
26B-Modell bleibt ein 26B-Modell), am Werkzeugkasten aber alles. Wenn die Antwort
„ziemlich weit" lautet, ist das eine Aussage, die für jeden gilt, der lokal
arbeitet — und genau das ist einen Vortrag wert.

## 2. Der Messtrick: das Modell festhalten, den Werkzeugkasten wegnehmen

Das Verfahren heißt **Ablation** — man baut Teile *aus* und schaut, was passiert.
Der Aufbau ist:

- **Aufgaben:** dieselben, immer. Zwölf Prompts aus der Test-Bank.
- **Modell:** dasselbe, immer (jedenfalls innerhalb einer Messreihe).
- **Verändert wird nur eines:** wie viel Werkzeugkasten der Agent hat.

Und zwar in vier Stufen, die aufeinander aufbauen:

| Stufe | Was der Agent hat | Was ihm fehlt |
|---|---|---|
| **H0** *nackt* | QGIS-Zugriff, Dateisystem, rohe Datenbeschaffung | jede Führung: keine Instruktionen, keine Abkürzungen, keine Warnungen, kein Gate |
| **H1** *+ Wissen* | zusätzlich alle Instruktionen und Skills | Werkzeuge sind weiterhin roh |
| **H2** *+ Werkzeugführung* | zusätzlich die geprüften Abkürzungen, `resolve_path`, das bbox-`warning` im Rückgabewert | keine Prüfung am Ende |
| **H3** *+ Erzwingung* | alles — Gate, `cross_check`, visuelle Validierung | (= Chester heute) |

**H3 ist der heutige Chester.** H0 ist „jemand hat an einem Nachmittag QGIS an ein
LLM geklemmt". Dazwischen liegt alles, was in einem Jahr gebaut wurde.

**Warum H0 die Datenbeschaffung behält.** Nimmt man dem Agenten auf H0 auch die
`fetch_*`-Werkzeuge, ist keine Aufgabe der Bank mehr lösbar — 34 von 35 Prompts
brauchen zuerst Daten. H0 läge bei 0 %, und zwar aus einem trivialen Grund:
gemessen wäre „ohne Daten geht nichts", nicht „wie viel trägt der Werkzeugkasten".
Weggenommen wird auf H0 die **Führung**, nicht die **Möglichkeit**.

## 3. Wie das konkret aussieht — ein Beispiel

Der Bank-Prompt `buffer-schools-500m`: *„Lege eine 500-Meter-Einzugszone um alle
Schulen in Regensburg an."*

- **Auf H0** muss das Modell alles selbst wissen: dass es erst Schulen holen muss,
  dass es auf die Stadtgrenze zuschneiden sollte (statt auf ein Rechteck), dass ein
  500-m-Puffer ein metrisches CRS braucht, wie der QGIS-Algorithmus heißt.
  Erwarteter typischer Ausgang: Puffer in Grad statt Metern, oder 101 Schulen statt
  84, weil auf der Bounding-Box gearbeitet wurde.
- **Auf H1** steht all das in den Instruktionen. Frage: Liest das kleine Modell es
  und hält sich dran? Der bbox-Fall sagt: **nein, nicht zuverlässig.**
- **Auf H2** kommt das Wissen nicht mehr als Ansage, sondern als Werkzeugverhalten:
  Das Tool gibt bei fehlendem `place` ein `warning` zurück, die Abkürzung macht die
  Umprojektion selbst. Der bereits vorliegende Befund lautet: *hier* kippte es
  (101 → 84 Schulen, 1544 → 1225 GTFS-Haltestellen).
- **Auf H3** prüft das Gate am Ende und schickt das Modell bei einem echten Defekt
  zurück.

Für jede Stufe gibt es ein Urteil vom Judge: bestanden oder nicht. Zwölf Aufgaben ×
vier Stufen = eine Tabelle mit vier Bestehensquoten. **Das ist die eigentliche
Messung.** Alles andere ist Deutung.

## 4. Wozu die drei Modelle

Eine einzelne Zahl („mit Werkzeugkasten 60 % statt 20 %") ist noch nichts wert,
weil die Bezugsgröße fehlt. Deshalb zwei zusätzliche Bezugspunkte, jeweils nur auf
H0 und H3 gemessen:

- **Die Decke:** ein großes gehostetes Modell. Es zeigt, was mit einem starken
  Modell *ohne* Werkzeugkasten geht.
- **Der Boden:** `llama3.1:8b`, von dem bekannt ist, dass es am Tool-Calling
  scheitert. Es zeigt, dass der Werkzeugkasten **nicht zaubert** — unterhalb einer
  gewissen Modellgröße hilft er gar nichts. Dieser Negativbefund ist wichtig, sonst
  klingt die These nach Werbung.

Daraus kommt die Zahl:

```
Kompensationsgrad K = (kleines Modell mit Werkzeugkasten − kleines Modell nackt)
                      ─────────────────────────────────────────────────────────
                      (großes Modell nackt             − kleines Modell nackt)
```

Rechenbeispiel mit den erwarteten Werten: klein/nackt 20 %, klein/voll 60 %,
groß/nackt 50 %. Dann ist K = (60−20)/(50−20) = 40/30 = **1,3**. Aussage im
Klartext: *„Das kleine lokale Modell mit gutem Werkzeugkasten schlägt das große
Modell ohne."* Käme 0,5 heraus, hieße es: „der Werkzeugkasten holt die halbe
Modelllücke auf" — auch das ein ordentliches Ergebnis.

Zusätzlich interessant sind die **Sprünge zwischen den Stufen**: H0→H1 (Wissen),
H1→H2 (Werkzeuge), H2→H3 (Zwang). Die bisherige Erfahrung sagt, der mittlere Sprung
ist der größte — *„ein Rückgabewert wirkt stärker als eine Systemanweisung"*. Das
ist eine Vorhersage, die die Messung widerlegen kann. Genau das macht sie zu
Forschung und nicht zu einer Illustration.

## 5. Warum das Arbeit ist

Heute lassen sich H0/H1/H2 gar nicht fahren — Chester ist fest verdrahtet auf H3.
Es braucht einen Schalter: `CHESTER_HARNESS_LEVEL=0..3`, ausgewertet an **einer**
Stelle (`agent_build.geo_capabilities()`), Vorgabe unverändert H3, damit sich am
Normalbetrieb nichts ändert.

Die unangenehme Stelle: Die Instruktionen stecken **in** den Capabilities. Wenn H0
einfach nur Capabilities weglässt, verschwinden mit den Werkzeugen auch die
Instruktionen — dann sind H0 und H1 gar nicht sauber getrennt. Es braucht also je
Capability einen Kurzmodus. Das sind geschätzt 150–200 Zeilen und der teuerste
Posten des Plans.

Dazu kommt Kleinkram: `evals.py --harness-level`, ein Feld `harness_level` in der
Historie, eine Matrix-Ansicht Modell × Stufe im Report, und das Prompt-Set
einfrieren (zwölf Aufgaben, quer über die Kategorien) — nach dem Start nicht mehr
anfassen, sonst misst man sich selbst.

## 6. Deshalb erst der Pilot

Bevor irgendetwas gebaut wird: **drei Aufgaben, zwei Stufen, ein Modell, ein halber
Tag.** H0 wird dabei von Hand hergestellt — in `geo_capabilities()` die Liste
zusammenstreichen und `/valid_level 0` setzen. Kein Schalter, kein Test, nichts
Dauerhaftes.

Die einzige Frage daran: *Ist da überhaupt ein Unterschied?* Wenn H0 und H3 nah
beieinander liegen, gibt es nichts zu messen — dann bleibt die Kompensationsfrage
eine **qualitative** These, belegt am bbox-Fall, und der Rest des Plans entfällt.
Das ist ausdrücklich ein erlaubter Ausgang und spart vier Nächte Rechenzeit und
zwei Tage Bauarbeit.

## 7. Zeitrahmen

12 Aufgaben × 4 Stufen × 2 Wiederholungen = 96 Läufe für das kleine Modell, plus je
48 für Boden und Decke (nur H0/H3). Zusammen **192 Läufe**, bei gemessenen ~7,5 min
pro Lauf rund **24 Stunden Rechenzeit** — also vier Nächte, nach Modellen sortiert.

| Phase | Was | Dauer |
|---|---|---|
| P0 | Pilot, ohne Code | ½ Tag |
| P1 | Schalter bauen | 1–2 Tage |
| P2 | Messapparat (`--harness-level`, Historie, Report) | ½–1 Tag |
| P3 | 192 Läufe | 4 Nächte |
| P4 | Auswertung, Text | 1 Tag |

## 8. Was am Ende dasteht

Ein Satz, den heute niemand belegen kann:

> *„Ein 26B-Modell auf einem Laptop erreicht mit einem sorgfältig gebauten
> Werkzeugkasten X % der Aufgaben, an denen es mit Rohzugriff scheitert — und davon
> entfällt der größte Teil nicht auf bessere Instruktionen, sondern darauf, das
> Wissen in die Rückgabewerte der Werkzeuge zu verlegen."*

Damit wird aus der üblichen Modellfrage („welches lokale Modell schafft
Geo-Tool-Calling?") etwas Haltbareres: eine Aussage über **Werkzeugbau**, die nicht
veraltet, sobald das nächste Modell erscheint. Ein Modellranking ist in sechs
Monaten Altpapier; „das Wissen gehört in den Rückgabewert" gilt auch dann noch.

## 9. Was diesen Plan kippen könnte

Vier Dinge, die vorher benannt sind, damit sie hinterher nicht wegerklärt werden:

- **Kein Effekt im Piloten.** Dann ist die These qualitativ und bleibt es (§6).
- **Bodeneffekt trotz Korrektur.** Falls auch mit rohen `fetch_*`-Werkzeugen fast
  alles auf H0 scheitert, misst K nur noch „lösbar/unlösbar". Erkennbar daran, dass
  die H0-Fehler sämtlich in der Beschaffung liegen und nicht in der Analyse — dann
  das Prompt-Set auf `fixture`-Aufgaben verschieben.
- **Die Stufen sind nicht sauber trennbar.** H2 bringt zwangsläufig
  Werkzeug-Docstrings und damit Prompt-Text mit; ΔH2 ist deshalb eine *Obergrenze*
  für den Effekt „Werkzeug", keine reine Schätzung. Muss im Text so stehen.
- **Zu wenige Wiederholungen.** Zwei Läufe je Zelle tragen keine
  Prozentpunkt-Vergleiche. Ergebnisse nur in groben Stufen berichten (deutlich
  besser / gleich / schlechter), K auf eine Nachkommastelle.
