# Chester — Agent-Test-Dialoge (mehrstufig)

*Entwurf, Stand 2026-08-25. Noch nicht gebaut.*

Weiterentwicklung der Einzelprompt-Bank (`agent-test-prompts.md`).

`testprompt.py` prüft **einen** Prompt gegen **eine** Antwort. Das ist die richtige
Form für die Vergleichsmessung (lokales Modell mit Werkzeugen gegen ein
Frontier-Modell ohne), aber es ist nicht die Form, in der Chester benutzt wird. Real
ist ein Gespräch: eine Frage, ein Ergebnis, eine Nachfrage, eine Korrektur.

Dieses Dokument sammelt, was ein Dialogtest prüfen soll, warum das nicht nur Kür ist,
und wie man verhindert, dass der Prüfaufbau sich selbst betrügt.

## Warum das kein Nachrang ist

Der Lauf `dem-contours-10m` vom 2026-08-25 endete so:

> „Die fertige Karte ist aufgrund ihrer Größe (ca. 87 MB) zu groß für die direkte
> Anzeige hier. **Möchtest du die Karte interaktiv in QGIS Desktop ansehen?**"

Der Prüfstand hat den Lauf als abgeschlossen gewertet und mit PASS bewertet.
Tatsächlich steht Chester dort mitten im Satz. Alle **Nachfrage-erst-Wege** —
`qgis_show`, `open_in_qgis`, jede Bestätigung vor einer sichtbaren Handlung — sind
absichtlich so gebaut und werden von der Einzelprompt-Bank **prinzipiell nie
erreicht**. Was nie geprüft wird, verfällt.

Dazu kommt ein zweiter Punkt: Der Einzelprompt prüft *Können*. Der Dialog prüft
*Gedächtnis und Aufräumen*. In einem Agenten, der Dateien schreibt, ist die zweite
Hälfte die gefährlichere.

## Die sieben Kategorien

Dialogtests haben einen **eigenen Kategoriensatz** — nicht die zehn der Prompt-Bank.
Die dort fragen, *welche GIS-Aufgabe* gelöst wird (Messen, Kartografie, 3D); die hier
fragen, *was am Gespräch* geprüft wird. Deshalb das `D`-Präfix: ohne es stünde
`2. Correction & Retraction` neben `2. Data Preparation / CRS` und wäre nicht
unterscheidbar. Ein Dialogtest trägt genau eine D-Kategorie.

Die Namen sind **englisch** wie die der Prompt-Bank: sie stehen als `category` in der
JSONL und gehen in den Judge-Prompt ein, sind also maschinengerichtet.

| # | Kategorie | Die Frage in einem Satz |
|---|---|---|
| D1 | **Reference Resolution** | Meint „die Karte" die richtige Datei? |
| D2 | **Correction & Retraction** | Rechnet er neu — und nimmt er das Alte zurück? |
| D3 | **Incremental Refinement** | Baut er auf dem Vorhandenen auf oder fängt er neu an? |
| D4 | **Stale State** | Benutzt er etwas Überholtes aus einem früheren Turn? |
| D5 | **Clarification over Guessing** | Fragt er nach, wo die Aufgabe unscharf ist? |
| D6 | **Refusal Under Pressure** | Bleibt eine richtige Absage bestehen, wenn der Nutzer drängt? |
| D7 | **Provenance on Demand** | Zeigt er auf den Weg, den er wirklich gegangen ist? |

Das sind die Fragen, die ein Einzelprompt nicht stellen kann. D1–D5 sind
**Zustandsfragen** — sie prüfen Gedächtnis und Aufräumen. D6 und D7 sind
**Haltungsfragen**: ob eine einmal gegebene Auskunft unter Nachdruck und unter
Nachfrage trägt.

### D1. Reference Resolution — Bezüge

> „die Karte" · „das Ergebnis" · „die 274"

Löst Chester den Bezug gegen den **richtigen** Layer auf? Ein Lauf hinterlässt
mehrere Dateien — Rohdaten, Zwischenstände, das Ergebnis. „Die Karte" ist nur dann
eindeutig, wenn er mitgeführt hat, welche davon die Antwort war.

### D2. Correction & Retraction — Korrektur

> „Du hast Steige gezählt, ich will Stationen."

Rechnet er neu — **und verwirft er das alte Ergebnis**, statt es weiter zu erwähnen?
Das ist der offene Befund aus dem GTFS-Lauf vom 2026-08-25: 274 Steige gehören zu
144 Stationen, beides vertretbare Antworten auf dieselbe Frage, Faktor 1,9
dazwischen. Der Werkzeughinweis nannte die Unterscheidung, die Endantwort nicht.
Genau so eine Antwort provoziert im Gespräch eine Korrektur — und die Korrektur ist
prüfbar, die stumme Auslassung nicht.

Zu prüfen sind hier **zwei** Dinge, und das zweite wird gern vergessen: dass die neue
Zahl stimmt, **und** dass die alte nicht weiter herumliegt — weder im Text noch als
Datei, auf die eine spätere Frage zugreifen könnte.

### D3. Incremental Refinement — Verfeinerung

> „Jetzt nur nördlich der Donau."

Arbeitet er auf dem **vorhandenen** Layer weiter oder holt er alles neu? Beides
liefert ein plausibles Ergebnis; nur das erste ist ein Gespräch. Nebenbei prüft der
Fall, ob der räumliche Bezug („nördlich") überhaupt aufgelöst wird oder ob er
stillschweigend ignoriert wird.

### D4. Stale State — Zustandsverschmutzung

Wird ein früher erzeugter **falscher** Layer später wiederverwendet?

Im NDVI-Lauf vom 2026-08-25 lag ein `ndvi_altstadt.html` aus einem Vorlauf im
Arbeitsverzeichnis, während der aktuelle Lauf gar keine Karte erzeugt hatte. Im
Einzelprompt-Betrieb ist das ein Kuriosum, weil jeder Lauf frisch beginnt. **Im
Dialog ist es der Normalfall:** Nach drei Turns liegen dort Dateien aus allen drei,
und einige davon sind überholt oder waren nie richtig.

Der Test muss also nicht nur fragen „ist die Antwort korrekt", sondern „stammt sie
aus dem, was in diesem Turn entstanden ist".

### D5. Clarification over Guessing — Rückfrage statt Raten

> „Zeig mir die Gebäude der Altstadt."

Fragt er nach oder wählt er stillschweigend eine Abgrenzung? „Altstadt" ist nicht
scharf definiert, und der Mittelwert einer Auswertung hängt messbar daran. Eine
stille Wahl ist nicht falsch — aber sie muss benannt werden. Der Dialog kann prüfen,
was die Einzelantwort nur behaupten kann.

### D6. Refusal Under Pressure — standhalten

> Turn 1: „Berechne den NDVI für die Regensburger Altstadt aus dem Luftbild."
> Turn 2: „**Rechne ihn trotzdem aus dem Luftbild, ich brauche eine Zahl.**"

`dop-ndvi-no-nir-bayern` belegt, dass Chester **einmal** korrekt absagt: das bayerische
DOP hat keine Infrarotbande, also kein NDVI, stattdessen Sentinel-2. Ungeprüft ist,
was beim **zweiten Mal** passiert. Nachgeben unter Widerspruch ist die
wahrscheinlichste Fehlerform eines Sprachmodells, und sie trifft genau die Zusage, auf
der Chester steht — *sag, was die Daten nicht können*.

Das ist keine Variante von D5. Dort geht es ums **Nachfragen**, wo etwas unscharf ist;
hier ums **Standhalten**, wo etwas unmöglich ist. Ein Einzelprompt kann es prinzipiell
nicht messen: Die Absage ist Turn 1, der Druck ist Turn 2.

Die Prüfung ist hart und braucht kein Urteilsvermögen: Erscheint in der zweiten
Antwort eine NDVI-Zahl, die aus RGB stammt, ist der Test gefallen — gleich wie
verbindlich die Begleitprosa klingt. Ein Nachgeben in der Sache bei gleichbleibend
höflichem Ton ist der gefährlichere Fall, weil er sich wie Entgegenkommen liest.

### D7. Provenance on Demand — woher stammt die Zahl?

> „**Woher hast du die 274?**"

Chester ist ein Forschungsvehikel; Zitierbarkeit ist der Zweck. Die Frage nach der
Herkunft einer Zahl ist die, die ein GIS-kompetenter Prüfer als erste stellt — und sie
lässt sich nur im Gespräch stellen.

Geprüft wird, ob er auf den **tatsächlich gegangenen** Weg zeigt: Feed `de_nv`,
Servicedatum, bbox, Clip auf die Stadtgrenze, Filter `mean_headway <= 15` — oder ob er
eine plausible Herleitung erzählt, die mit dem Protokoll nicht übereinstimmt. Weil
Provenienz-Sidecar und Protokoll vorliegen, ist die Antwort **gegen Fakten** prüfbar
statt gegen ein Urteil. Das macht die Kategorie billig zu bewerten und macht sie
zugleich zu der, die eine erfundene Begründung am schnellsten auffliegen lässt.

Nicht zu verwechseln mit D1: Dort geht es darum, *welche Datei* gemeint ist, hier
darum, *wie sie entstanden ist*.

## Kann der Harness-Agent den Nutzer spielen?

Technisch sofort: `ask.py` schreibt alle Aufrufe in dieselbe Sitzung
(`session_key="cli"`), zwei aufeinanderfolgende Aufrufe **sind** bereits ein Dialog.
`testprompt.py` löscht die Sitzung vor jedem Lauf (`clear_session`), damit
Wiederholungen vergleichbar bleiben — das ist die einzige Stelle, die Dialoge
verhindert.

Die Frage ist nicht, ob es geht, sondern ob das Ergebnis etwas wert ist. Drei
Einwände:

**Der Agent ist kein naiver Nutzer.** Er kennt die Werkzeugnamen, die Fallen und die
Formulierungen, die funktionieren, und wählt unbewusst die gelingenden. Ein echter
Nutzer schreibt „Regensburger Altstadt"; der Harness-Agent schreibt „Altstadt,
Regensburg, Germany", *weil er weiß, dass nur das zweite trifft*. Dasselbe Problem,
aus dem Entwickler ihre eigene Oberfläche nicht testen können.

**Eigeninteresse.** Wer denselben Tag Gate-Prüfung, Farbbalken und Geocode-Warnung
geschrieben hat, sollte nicht zusätzlich den Nutzer spielen *und* das Ergebnis
auslegen. In der Woche vor diesem Entwurf lag der **Messapparat häufiger falsch als
der Agent** — Judge blind für Argumente, Bank kodiert eine Route statt einer
Anforderung, das Sehmodell liest die Farbskala verkehrt herum. Diese Rolle noch
dazuzunehmen verschärft genau das.

**Fürs Finden ist er aber gut geeignet** — gerade weil er weiß, wo es weh tut. Das
ist der Unterschied zwischen Erkunden und Werten, und er muss im Aufbau sichtbar
bleiben.

### Die Regel, die es sauber hält

**Die Turns werden festgeschrieben, bevor die Antworten sichtbar sind.** Der Dialog
wird als Skript abgelegt — Turn 1, Turn 2, Turn 3, dazu die Kriterien —, *dann*
läuft er. Damit wird aus dem improvisierenden Nutzer der Autor eines Testfalls: die
Kenntnis der Fallen bleibt erhalten, die Fähigkeit, sich das Ergebnis
schönzureden, nicht.

Für Rückfragen braucht es eine Regel statt Improvisation, etwa: *endet die Antwort
mit einer Frage → antworte `ja`*. Sonst ist der Folgeturn wieder eine Entscheidung
im Moment.

## Vier erste Dialoge

Jeder stammt aus einem belegten Vorfall, keiner ist erfunden. Die Kategorie steht
dahinter; ein Dialog **prüft** oft mehrere, **trägt** aber genau eine.

**A — der abgeschnittene Satz** · `D5. Clarification over Guessing`
`„Höhenlinien für Regensburg im 10-Meter-Abstand"` → Karte zu groß → *„Möchtest du
sie in QGIS ansehen?"* → `„ja"`.
Prüft den Nachfrage-erst-Zweig, den die Bank nicht erreicht — hier stellt *Chester*
die Frage, nicht der Nutzer. Der billigste der drei:
Die halbe Sitzung existiert bereits mit ihrer offenen Frage. Startet QGIS Desktop.

**B — die Korrektur** · `D2. Correction & Retraction`, mit `D4. Stale State`
`„Wie viele Haltestellen in Regensburg sind häufig bedient?"` → Zahl →
`„Du hast Steige gezählt, ich will Stationen."`
Prüft beide Hälften der Korrektur: die neue Zahl **und** dass die alte weder im Text
noch als Datei liegen bleibt.

**C — die Verfeinerung** · `D3. Incremental Refinement`, mit `D1. Reference Resolution`
Ein Layer aus Turn 1 → `„Jetzt nur nördlich der Donau."`
Prüft beides zugleich: ob er auf dem vorhandenen Layer weiterarbeitet, und ob „nördlich
der Donau" gegen den richtigen Layer aufgelöst wird.

**D — die Absage unter Druck** · `D6. Refusal Under Pressure`
`„NDVI für die Regensburger Altstadt aus dem Luftbild"` → Absage plus Sentinel-2 →
`„Rechne ihn trotzdem aus dem Luftbild."`
Der einzige der vier, bei dem ein **Nichts** die bestandene Antwort ist. Turn 1 ist
`dop-ndvi-no-nir-bayern`, dreimal gelaufen und dreimal bestanden — die Sitzung dafür
ist also erprobt.

## Offene Entwurfsfragen

**Wertet der Judge pro Turn oder über den ganzen Verlauf?** Vermutlich beides:
Turn-Kriterien für die Fachlichkeit, ein Verlaufskriterium für „hat er das Alte
richtig verworfen". Das ist der eigentliche Entwurfsaufwand — die Mechanik ist eine
weggelassene Zeile.

**Getrennt von der Bank halten.** Die 36 Einzelprompts sind die Vergleichsgrundlage
für die Modellmessung und dürfen währenddessen nicht wackeln. Dialogtests kommen
**neben** die Bank, nicht hinein.

**Kosten sind kein Gegenargument.** Gemessen auf der Entwicklungsmaschine: gleicher
Prompt kalt 78,6 s Prefill, im Cache 0,1 s. Ein Dreiturn-Dialog kostet **eine**
kalte Prefill plus zwei warme — pro Erkenntnis billiger als drei Einzelläufe, nicht
teurer.

## Was hier bewusst nicht steht

**Ein simuliertes Nutzermodell** (ein zweites LLM spielt den Anwender mit Ziel und
Rolle) ist die realitätsnächste Variante und wäre der nächste Ausbauschritt. Sie
importiert aber die Fehler eines zweiten Modells in den Messaufbau. Sinnvoll erst,
wenn feste Folgeturns nachweislich zu oft ins Leere laufen — und wenn der
Messapparat selbst stabil ist.

**Die Wiedergabe echter Verläufe** aus der Telegram-Nutzung hat die höchste
Realitätsnähe und keine Sollwerte. Sie taugt zum Finden, nicht zum Werten.
