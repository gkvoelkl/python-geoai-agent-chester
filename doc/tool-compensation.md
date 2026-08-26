# Wie wichtig sind ausgereifte Tools für lokale Standard-Modelle?

> Dieses Dokument erklärt **ein** Vorhaben — im Projekt kurz *die
> Kompensationsfrage* —, und zwar so, dass man es ohne Vorwissen über Chesters
> Innenleben versteht. Es ist ein **Versuchsplan mit einem Vorversuch im Rücken**,
> kein Ergebnisbericht: Es sagt, was gemessen werden soll, mit welchem Aufbau, was
> der Vorversuch bereits gezeigt hat und woran der Plan scheitern würde. Der
> Messapparat, auf dem das aufsetzt — Prompt-Bank, Judge, Tool-Coverage, Historie —,
> steht in [`agent-test-prompts.md`](./agent-test-prompts.md); die Prüfschicht in
> [`validation-concept.md`](./validation-concept.md). Der Arbeitsstand (welcher
> Schritt läuft gerade) wird projektintern geführt und steht nicht hier.
>
> **Status: Vorversuch gelaufen (2026-08-22), Hauptmessung offen. Der Aufbau wurde
> daraufhin von vier Stufen auf zwei Zellen umgestellt (§2, §6).**

## 1. Was überhaupt gefragt wird

Ein Geo-Agent besteht aus zwei Teilen: dem **Sprachmodell** und dem **Drumherum**
(Konnektoren, Werkzeugzuschnitt, Instruktionen, Validierungs-Gate). Wenn etwas
nicht funktioniert, gibt es zwei Reflexe: „nimm ein größeres Modell" oder „bau
bessere Werkzeuge".

Die übliche Frage lautet „was ist wichtiger?" — die ist unbeantwortbar und auch
uninteressant. Die nützliche Variante ist eine Wettfrage:

> **Schlägt ein kleines lokales Modell mit sorgfältig gebautem Werkzeugkasten ein
> Frontier-Modell mit Rohzugriff?**

Das ist deshalb die richtige Frage, weil sie eine Entscheidung trifft, vor der
jeder steht, der so etwas baut: Geld in ein größeres Modell stecken oder Zeit in den
Werkzeugbau. Und sie ist beantwortbar — anders als „was ist wichtiger".

## 2. Der Aufbau: zwei Zellen

Verglichen werden zwei Aufstellungen an denselben zwölf Aufgaben aus der Test-Bank:

| Zelle | Modell | Werkzeugkasten |
|---|---|---|
| **L+** | ein lokales 26B-Modell auf dem Laptop | Chester vollständig — Konnektoren, geprüfte Abkürzungen, Instruktionen, Validierungs-Gate |
| **F−** | ein gehostetes Frontier-Modell | nackt: QGIS-Zugriff, Dateisystem, rohe Datenbeschaffung, sonst keine Führung |

**F− ist „jemand hat an einem Nachmittag QGIS an ein starkes LLM geklemmt".** L+ ist
das, was in einem Jahr Werkzeugbau entstanden ist, auf einem Modell, das auf einem
Laptop läuft. Ergebnis ist der Vergleich zweier Bestehensquoten — mehr nicht, und
das ist Absicht.

**Warum F− die Datenbeschaffung behält.** Nimmt man dem Agenten auch die
`fetch_*`-Werkzeuge, ist keine Aufgabe der Bank mehr lösbar — 34 von 35 Prompts
brauchen zuerst Daten. F− läge bei 0 %, und zwar aus einem trivialen Grund:
gemessen wäre „ohne Daten geht nichts", nicht „wie viel trägt der Werkzeugkasten".
Weggenommen wird die **Führung**, nicht die **Möglichkeit**.

**Eine frühere Fassung dieses Plans hatte vier Stufen** — vom nackten Agenten über
„plus Instruktionen" und „plus Werkzeugführung" bis zum vollständigen Chester — und
wollte daraus eine Kennzahl berechnen. Der Vorversuch hat gezeigt, dass die dafür
nötige Auflösung nicht erreichbar ist (§6). Zwei Zellen sind das, was die Daten
tragen.

## 3. Wie das konkret aussieht — ein Beispiel

Der Bank-Prompt `buffer-schools-500m`: *„Lege eine 500-Meter-Einzugszone um alle
Schulen in Regensburg an."*

- **Auf F−** muss das Modell alles selbst wissen: dass es erst Schulen holen muss,
  dass es auf die Stadtgrenze zuschneiden sollte (statt auf ein Rechteck), dass ein
  500-m-Puffer ein metrisches CRS braucht, wie der QGIS-Algorithmus heißt.
  Erwarteter typischer Ausgang: Puffer in Grad statt Metern, oder 101 Schulen statt
  84, weil auf der Bounding-Box gearbeitet wurde.
- **Auf L+** kommt dieses Wissen nicht als Ansage, sondern als Werkzeugverhalten:
  Das Tool gibt bei fehlendem `place` ein `warning` zurück, die Abkürzung
  reprojiziert selbst, und am Ende prüft das Gate das Ergebnis.

Der bereits vorliegende Einzelbefund, an dem die These hängt: *hier* kippte es —
101 → 84 Schulen, 1544 → 1225 GTFS-Haltestellen. Nicht als das Wissen in den
Instruktionen stand, sondern als es in den **Rückgabewert** des Werkzeugs wanderte.

**Ein zweiter Beleg, sauberer als der erste** (2026-08-26). Bis dahin lieferte
`osm_features(place=…)` alles, was die Stadtgrenze *berührt*, mit ungeschnittener
Geometrie — während die Werkzeugbeschreibung den Zuschnitt versprach. Auf die Frage
„wie viel Prozent des Waldes in Regensburg liegt an einer Straße?" hätte derselbe
Agent mit derselben Aufrufkette **20 %** geantwortet: Der Zähler war über den
Straßenpuffer implizit beschnitten, der Nenner nicht, und ein einziger Wald
(25,84 km², davon 0,71 km² in der Stadt) trug den Unterschied. Nachdem der Zuschnitt
ins Werkzeug wanderte, antwortete er **94,5 %** — der von Hand gerechnete Sollwert
ist 95,0 %. Was diesen Befund vom ersten unterscheidet: Der Sollwert stand **vor**
dem Lauf fest und **nicht** in der Bewertungsrubrik, der Agent bekam keine neue
Instruktion, und die Änderung lag ausschließlich im Rückgabewert des Werkzeugs. Es
ist derselbe Mechanismus wie beim bbox-`warning`, nur diesmal als Vorher-Nachher an
einer Zahl, die sich nicht wegdiskutieren lässt.

Für jede Zelle gibt es pro Aufgabe ein Urteil vom Judge: bestanden oder nicht.
Zwölf Aufgaben × zwei Zellen × drei Wiederholungen — das ist die Messung. Alles
andere ist Deutung.

## 4. Was am Ende verglichen wird

Keine Kennzahl, sondern Brüche. Für jede Aufgabe steht am Ende etwas wie „L+ 3/3,
F− 1/3", und darüber eine Gesamtaussage in groben Stufen: L+ deutlich besser,
gleichauf, oder schlechter als F−.

Das ist bewusst grob. Drei Läufe je Zelle tragen keinen Prozentpunkt-Vergleich —
„3/3 gegen 1/3" ist ehrlich, „100 % gegen 33 %" behauptet eine Genauigkeit, die die
Daten nicht haben.

Interessanter als die Quote ist ohnehin die **Fehlerverteilung**: Woran scheitert
F−? Falsche Daten, falsches CRS, falsches Werkzeug, Abbruch, Halluzination? Wenn
die F−-Fehler überwiegend im Zuschnitt liegen (bbox statt Grenze, Grad statt Meter)
und nicht in der Beschaffung, dann sagt das genau, *was* der Werkzeugkasten
leistet — und das ist die übertragbare Erkenntnis, nicht die Zahl.

**Der offene Preis dieses Zuschnitts:** F− unterscheidet sich von L+ in *zwei*
Faktoren gleichzeitig — anderes Modell **und** anderer Werkzeugkasten. Ein
Unterschied lässt sich also nicht sauber dem einen oder anderen zuschreiben. Das ist
der Preis dafür, dass die Frage praktisch entscheidbar bleibt; er gehört genannt,
nicht wegerklärt.

## 5. Warum das überhaupt Arbeit ist

Weniger, als es zunächst aussah. Chester ist fest auf den vollen Werkzeugkasten
verdrahtet, F− muss also von Hand hergestellt werden: auf einem Zweig die
Fähigkeiten-Liste zusammenstreichen, die geprüften Abkürzungen stilllegen, das Gate
abschalten. Ein halber Tag, nichts davon wird committet.

Die frühere Fassung brauchte dafür einen Schalter mit vier Stufen und je Capability
einen Kurzmodus — geschätzt 150–200 Zeilen und der teuerste Posten des ganzen Plans.
Der entfällt: Er diente allein dazu, „plus Instruktionen" von „plus
Werkzeugführung" zu trennen, und diese Trennung gibt es nicht mehr.

Dazu kommt Kleinkram: ein Feld in der Ergebnishistorie, das die Zelle festhält, eine
Vergleichsansicht im Report, und das Prompt-Set einfrieren (zwölf Aufgaben, quer
über die Kategorien) — nach dem Start nicht mehr anfassen, sonst misst man sich
selbst.

## 6. Der Vorversuch — und was er ergab

Bevor irgendetwas gebaut wurde: drei Aufgaben, ein Modell, ein halber Tag. Die
Auswahlregel war, dass die Aufgaben **mit** vollem Werkzeugkasten bestehen müssen —
was der Werkzeugkasten nicht löst, kann er auch nicht ausgleichen.

**Das Ergebnis war kein Effekt, sondern ein Messproblem.** Dieselben drei Aufgaben,
derselbe Werkzeugkasten, dasselbe Modell, drei Termine:

| Aufgabe | 18./19.08. | 20.08. | 22.08. |
|---|---|---|---|
| Radweg-Länge | bestanden | bestanden | bestanden |
| mittlere Höhe je Bezirk | bestanden | bestanden | **gescheitert** |
| Supermärkte in 10 Gehminuten | bestanden | bestanden | **gescheitert** |

Dazwischen wurde nichts weggenommen. Das ist die normale Streuung eines lokalen
26B-Modells — und sie ist **größer als der Effekt, den die Messung finden soll**.
Mit einem Lauf je Zelle hätte der Vergleich Rauschen gemessen und wie ein Befund
ausgesehen.

Das ist der erste veröffentlichungsfähige Ertrag dieses Vorhabens, und er gilt
unabhängig davon, wie die Hauptmessung ausgeht:

> **Ein Lauf je Zelle misst bei einem lokalen Modell dieser Größe nichts.** Wer
> Agenten-Benchmarks ohne Wiederholungen berichtet, berichtet Würfelwürfe.

Daraus folgen die drei Wiederholungen aus §3 — und ein angenehmer Nebeneffekt: Die
Basiszelle L+ ist zugleich die Stabilitätsprüfung. Aufgaben, die dort 0/3 oder 1/3
erreichen, werden als „auch mit Werkzeugkasten nicht lösbar" **berichtet**, nicht
aussortiert; sonst frisiert man die eigene Quote.

**Warum Wiederholungen jetzt bezahlbar sind.** Am selben Tag fiel auf, dass Chesters
Systemprompt sich nach jedem schreibenden Werkzeugaufruf änderte — eine Liste des
Datencaches stand mitten in den Instruktionen. Lokale Laufzeitumgebungen lesen einen
Prompt ab der ersten abweichenden Stelle neu ein; gemessen kostete ein unveränderter
Prompt 0,1 Sekunden, derselbe Prompt mit einer geänderten Zeile in der Mitte 52,8
Sekunden. Nachdem die Liste in einen Werkzeugaufruf verschoben wurde, fiel dieselbe
Aufgabe von 19,3 auf 10,5 Minuten. Auch das ist ein Ergebnis über Werkzeugbau:
**Was sich ändert, gehört nicht in den Systemprompt.**

## 7. Zeitrahmen

12 Aufgaben × 3 Wiederholungen = 36 Läufe für L+, dazu 3 Wiederholungen der
gewerteten Aufgaben für F−. Bei gemessenen ~13 Minuten je lokalem Lauf sind das rund
8 Stunden für L+ und, weil ein gehostetes Modell schneller antwortet, etwa 2,5
Stunden für F− — zusammen **zwei Nächte**.

| Phase | Was | Dauer |
|---|---|---|
| P0 | Vorversuch, ohne Code | ✔ erledigt |
| P1 | Zelle F− von Hand herstellen | ½ Tag |
| P2 | Messapparat (Zellen-Feld, Vergleichsansicht) | ½ Tag |
| P3 | ~70 Läufe | 2 Nächte |
| P4 | Auswertung, Text | 1 Tag |

**Eine harte Voraussetzung**, die vor allem anderen steht: F− braucht einen
API-Zugang zu einem Frontier-Modell. Ohne den ist dieser Aufbau nicht messbar.

## 8. Was am Ende dasteht

Im günstigen Fall ein Satz, den heute niemand belegen kann:

> *„Ein 26B-Modell auf einem Laptop löst mit einem sorgfältig gebauten
> Werkzeugkasten mehr Geo-Aufgaben als ein Frontier-Modell mit Rohzugriff — und der
> Unterschied liegt nicht im Wissen, sondern darin, dass das Wissen in den
> Rückgabewerten der Werkzeuge steckt."*

Damit wird aus der üblichen Modellfrage („welches lokale Modell schafft
Geo-Tool-Calling?") etwas Haltbareres: eine Aussage über **Werkzeugbau**, die nicht
veraltet, sobald das nächste Modell erscheint. Ein Modellranking ist in sechs
Monaten Altpapier; „das Wissen gehört in den Rückgabewert" gilt auch dann noch.

Im ungünstigen Fall steht dort das Gegenteil — dass das Frontier-Modell auch nackt
vorne liegt. Auch das wäre eine brauchbare Aussage, und sie würde hier genauso
stehen.

## 9. Was diesen Plan kippen könnte

Vier Dinge, vorher benannt, damit sie hinterher nicht wegerklärt werden:

- **Kein Zugang zum Frontier-Modell.** Dann entfällt die Vergleichszelle, und die
  Kompensationsfrage bleibt eine qualitative These, belegt am bbox-Fall aus §3. Ein
  zulässiger Ausgang, kein Scheitern.
- **Streuung auch bei drei Wiederholungen.** Der Vorversuch zeigt sie deutlich; drei
  Läufe könnten zu wenig sein. Erkennbar daran, dass viele Aufgaben bei 2/3 landen —
  dann ist der Unterschied zwischen den Zellen nicht mehr ablesbar.
- **Bodeneffekt.** Falls auch mit rohen `fetch_*`-Werkzeugen fast alles auf F−
  scheitert, misst der Vergleich nur noch „lösbar/unlösbar". Erkennbar daran, dass
  die F−-Fehler sämtlich in der Beschaffung liegen und nicht in der Analyse.
- **Zwei Faktoren auf einmal.** F− variiert Modell und Werkzeugkasten zugleich
  (§4). Der Vergleich beantwortet damit die praktische Frage, nicht die analytische.
