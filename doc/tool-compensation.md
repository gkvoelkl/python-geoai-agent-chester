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
> daraufhin von vier Stufen auf zwei Zellen umgestellt (§2, §6), am 2026-09-08 ein
> zweites Mal geändert (die Vergleichszelle bekam denselben Werkzeugkasten wie die
> Basiszelle) und am 2026-09-13 um die Zelle **F−** ergänzt: dasselbe gehostete
> Modell, aber über seine Produktoberfläche und ohne Chester. Damit ist auch die
> Werkzeugachse mit einem bewegten Faktor messbar, ohne dass dafür etwas gebaut
> werden muss (§2).**

## 1. Was überhaupt gefragt wird

Ein Geo-Agent besteht aus zwei Teilen: dem **Sprachmodell** und dem **Drumherum**
(Konnektoren, Werkzeugzuschnitt, Instruktionen, Validierungs-Gate). Wenn etwas
nicht funktioniert, gibt es zwei Reflexe: „nimm ein größeres Modell" oder „bau
bessere Werkzeuge".

Die übliche Frage lautet „was ist wichtiger?" — die ist unbeantwortbar und auch
uninteressant. Die nützliche Variante zerfällt in **zwei Achsen**, und jede lässt
sich nur sauber messen, wenn man auf ihr genau *einen* Faktor bewegt:

| Achse | Was variiert | Was konstant bleibt | Beantwortet |
|---|---|---|---|
| **Modell** | lokal ↔ Frontier | derselbe Werkzeugkasten | Was kauft mehr Modell? |
| **Werkzeugkasten** | vollständig ↔ nackt | dasselbe Modell | Was trägt der Werkzeugbau? |

Die Wettfrage des Projekts — *schlägt ein kleines lokales Modell mit sorgfältig
gebautem Werkzeugkasten ein Frontier-Modell mit Rohzugriff?* — bewegt **beide**
Achsen auf einmal. Sie ist praktisch relevant (vor genau dieser Entscheidung steht,
wer so etwas baut: Geld in ein größeres Modell oder Zeit in den Werkzeugbau), aber
als Messung ist sie mehrdeutig: Ein Unterschied ließe sich weder dem Modell noch dem
Werkzeugkasten zuschreiben.

Deshalb wird sie in die zwei Achsen zerlegt und **die Modellachse zuerst gemessen**.

## 2. Der Aufbau: zwei Zellen, ein Faktor

Verglichen werden Aufstellungen an **derselben ganzen Bank**. Eine frühere Fassung
wählte zwölf stratifizierte Aufgaben aus, um Rechenzeit zu sparen; das entfiel am
2026-08-24, weil jede Auswahl hinterher zu rechtfertigen wäre („warum diese zwölf?").
Drei Zellen sind definiert, und seit dem 2026-09-13 liegen von allen dreien Läufe vor:

| Zelle | Modell | Werkzeugkasten | Status |
|---|---|---|---|
| **L+** | lokales 26B-Modell auf dem Laptop | Chester vollständig — Konnektoren, geprüfte Operationen, Instruktionen, Validierungs-Gate | Basiszelle, läuft |
| **F+** | `claude-sonnet-5`, gehostet | **derselbe** Chester, identisch konfiguriert | läuft |
| **F−** | `claude-sonnet-5`, **über die Produktoberfläche** (claude.ai im Browser) | keiner — kein Chester, nur die Allzweckwerkzeuge des Produkts (Websuche, Ortssuche) | läuft |

**Zwei Achsen, je ein Faktor.** L+ ↔ F+ bewegt das **Modell** bei festgehaltenem
Werkzeugkasten; F+ ↔ F− bewegt den **Werkzeugkasten** bei festgehaltenem Modell. Die
zweite ist die Kompensationsfrage selbst.

**L+ und F+ laufen auf derselben Maschine, mit derselben Bank, derselben
Konfiguration und demselben Gate.** Der einzige Unterschied ist der Wert von
`model.model` in `.chester/chester.json` — Chesters LLM-Schicht ist reine
Konfiguration, ein Modellwechsel also kein Codewechsel. Das ist der Grund, warum
diese Zelle überhaupt sauber herstellbar ist.

**Der Vergleich L+ ↔ F+ beantwortet die Kompensationsfrage nicht.** Er sagt, was mehr
Modell bei festgehaltenem Werkzeugkasten bringt — nicht, was der Werkzeugkasten trägt.
Dafür ist **F+ ↔ F−** da: dasselbe Modell, einmal mit Chester und einmal ohne.

**Was F− ist und was es nicht ist** (Zuschnitt vom 2026-09-13). F− ist das, was ein
Mensch ohne Chester bekommt: dasselbe Frontier-Modell, bedient über sein Produkt. Das
ist ausdrücklich **nicht** dasselbe wie ein Modell ohne jedes Werkzeug — die Oberfläche
bringt Websuche und eine Ortssuche mit, und genau deshalb ist die Zelle kein trivialer
Nullpunkt: Sie kann Fakten beschaffen, nur keine Geometrie rechnen. Gemessen an zwei
Rückhalte-Aufgaben erreichte sie 2/5 und 1/5 Kriterien, nicht 0/5.

Zwei Vorfassungen tragen denselben Buchstaben und sind **nicht** gemeint: ein
Frontier-Modell mit absichtlich nacktem Geo-Werkzeugkasten (gestrichen am 2026-09-08,
weil es Modell *und* Werkzeugkasten zugleich bewegte) und ein reiner API-Aufruf ohne
Systemprompt und ohne Werkzeuge (existiert als Gegenprobe in `frontier.py`, ist aber
keine Zelle dieser Reihe). Wer ältere Zahlen unter „F−" liest, muss prüfen, welche der
drei gemeint war.

**Der Preis dieses Zuschnitts, offen benannt.** F− ist weniger kontrolliert als die
beiden anderen Zellen: Das Produkt ändert sich über die Zeit, es gibt keine
Tokenabrechnung je Lauf, die Denkstufe steht auf der Voreinstellung der Oberfläche
(„Mittel", während F+ mit `effort: high` fährt), und die Zelle kann keine Dateien
erzeugen — eine Karte entsteht dort als Widget im Chat, nicht als prüfbares Artefakt.
Festgelegt ist dafür das **Modell**: `claude-sonnet-5` in F− wie in F+. Ein Lauf auf
einem anderen Modell gehört nicht in die Zelle, sonst sind die Brüche über die Aufgaben
nicht addierbar. Modell, Denkstufe und die benutzten Produktwerkzeuge werden je Lauf
mitgeschrieben.

**Offen bleibt die Werkzeugachse auf dem lokalen Modell** — L−, dasselbe 26B-Modell
ohne Führung. Sie ist verschoben, nicht gestrichen; F+ ↔ F− beantwortet dieselbe Frage
zuerst auf dem gehosteten Modell, weil diese Zelle ohne Bauarbeit herstellbar ist.

**Warum eine werkzeuglose Zelle die Datenbeschaffung behalten muss.** Nimmt man dem
Agenten auch jeden Weg an Daten, ist keine Aufgabe der Bank mehr lösbar —
nachgezählt: **alle** Prompts der Bank führen ein Beschaffungswerkzeug in
`tools_expected`. Die Zelle läge bei 0 %, und zwar aus einem trivialen Grund: gemessen
wäre „ohne Daten geht nichts", nicht „wie viel trägt der Werkzeugkasten". Weggenommen
wird die **Führung**, nicht die **Möglichkeit**. Für F− erledigt sich das von selbst,
weil die Produktoberfläche ihre eigene Websuche mitbringt; für das künftige L− bleibt
es eine Bauvorgabe.

**Eine frühere Fassung dieses Plans hatte vier Stufen** — vom nackten Agenten über
„plus Instruktionen" und „plus Werkzeugführung" bis zum vollständigen Chester — und
wollte daraus eine Kennzahl berechnen. Der Vorversuch hat gezeigt, dass die dafür
nötige Auflösung nicht erreichbar ist (§6). Zwei Zellen je Achse sind das, was die
Daten tragen.

**Die Konfiguration, unter der gemessen wird**, gehört mit ins Protokoll, weil sie
den Werkzeugkasten definiert, den beide Zellen teilen: `geodata.use_qgis: false`,
also der QGIS-lose Zweig mit 19 Fähigkeiten und 85 Werkzeugen. Der Rechenkern
(GeoPandas · rasterio · networkx) ist damit für beide Modelle derselbe; der
QGIS-Katalog steht keinem zur Verfügung. Beides steht seit dem 2026-09-12 in **jeder
Zeile** der Historie: die Zelle als gesetztes Etikett (`CHESTER_EVAL_CELL`) — abgeleitet
werden kann sie nicht, weil F+ und F− dasselbe Modell fahren und L+ und F+ denselben
Werkzeugkasten —, dazu der Schalter `use_qgis`. Fehlt das Etikett, gilt der Lauf als
*unbekannt* und nicht als Basiszelle; die Auswertung meldet Widersprüche zwischen
Etikett, Modell und Schalter, statt sie zu verrechnen.

## 3. Wie das konkret aussieht — ein Beispiel

Der Bank-Prompt `buffer-schools-500m`: *„Lege eine 500-Meter-Einzugszone um alle
Schulen in Regensburg an."*

Vier Dinge muss ein Agent hier von sich aus richtig machen: erst die Schulen holen,
auf die **Stadtgrenze** zuschneiden statt auf ein Rechteck, für einen 500-m-Puffer in
ein metrisches CRS wechseln, und das Ergebnis prüfen. Ein nackter Agent muss das
alles wissen; typischer Ausgang sonst: Puffer in Grad statt Metern, oder 101 Schulen
statt 84, weil auf der Bounding-Box gearbeitet wurde.

- **Auf L+ und F+ gleichermaßen** kommt dieses Wissen nicht als Ansage, sondern als
  Werkzeugverhalten: Das Tool gibt bei fehlendem `place` ein `warning` zurück, die
  geprüfte Operation reprojiziert selbst, und am Ende prüft das Gate das Ergebnis.
  **Beide Modelle bekommen diese Hilfe.** Die Frage der ersten Messung ist deshalb
  nicht, ob das Frontier-Modell ohne Führung zurechtkommt, sondern ob es die
  angebotene Führung **besser nutzt** — sie früher aufgreift, seltener am Gate
  hängenbleibt, weniger Umwege läuft.
- **Auf L− — der offenen Zelle** fiele diese Hilfe weg, und zwar bei gleichem
  Modell. Erst dieser Vergleich beziffert, was der Werkzeugkasten trägt.

Der bereits vorliegende Einzelbefund, an dem die Kompensationsthese hängt, ist genau
von dieser zweiten Art — **gleiches Modell, nur besserer Werkzeugkasten**: 101 → 84
Schulen, 1544 → 1225 GTFS-Haltestellen. Es kippte nicht, als das Wissen in den
Instruktionen stand, sondern als es in den **Rückgabewert** des Werkzeugs wanderte.
Dass dieser Beleg auf der Werkzeugachse liegt und nicht auf der Modellachse, ist
kein Zufall, sondern der Grund, warum L− die interessantere der beiden offenen
Zellen ist.

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
Die ganze Bank × zwei Zellen × drei Wiederholungen — das ist die Messung. Alles
andere ist Deutung.

## 4. Was am Ende verglichen wird

Keine Kennzahl, sondern Brüche. Für jede Aufgabe steht am Ende etwas wie „L+ 3/3,
F+ 2/3", und darüber eine Gesamtaussage in groben Stufen: gleichauf, oder eine Zelle
deutlich besser.

Das ist bewusst grob. Drei Läufe je Zelle tragen keinen Prozentpunkt-Vergleich —
„3/3 gegen 1/3" ist ehrlich, „100 % gegen 33 %" behauptet eine Genauigkeit, die die
Daten nicht haben.

Interessanter als die Quote ist ohnehin die **Fehlerverteilung**: Woran scheitert
welche Zelle? Falsche Daten, falsches CRS, falsches Werkzeug, Abbruch,
Halluzination? Bei gleichem Werkzeugkasten wird diese Verteilung besonders
aussagekräftig, denn sie kann nicht mehr auf fehlende Werkzeuge geschoben werden:
Was das lokale Modell häufiger falsch macht, macht es mit denselben Werkzeugen in
der Hand falsch.

**Was der Vergleich liefert — und was nicht.** Die Differenz L+ ↔ F+ ist eine
**Obergrenze dafür, was mehr Modell auf diesem Werkzeugkasten noch kaufen kann**.
Genau diese Zahl braucht die praktische Entscheidung „Geld ins Modell oder Zeit in
den Werkzeugbau": Ist die Differenz klein, ist weiteres Modellgeld auf dieser
Aufgabenklasse schlecht investiert. Ist sie groß, weiß man, wie viel der
Werkzeugkasten noch **nicht** ausgleicht.

Was er nicht liefert: den Beitrag des Werkzeugkastens selbst. Der steht auf der
anderen Achse — seit dem 2026-09-13 als **F+ ↔ F−** messbar (§2), auf dem lokalen
Modell (L−) weiterhin offen. Ein knappes Ergebnis auf der Modellachse wäre also
**kein** Beleg für „der Werkzeugkasten macht das Modell egal"; dafür ist die
Werkzeugachse zuständig. Es wäre ein Beleg dafür, dass die Modellachse auf diesem Aufbau
flach ist, mehr nicht.

## 5. Warum das überhaupt Arbeit ist

Für die erste Messung: **fast nicht mehr.** F+ ist ein Wert in
`.chester/chester.json` — `model.model` von `ollama/…` auf den gehosteten Anbieter
umstellen, API-Key in `.env`. Kein Zweig, kein Umbau, nichts, was committet werden
müsste. Das ist die Auszahlung einer alten Entscheidung: Die LLM-Schicht ist reine
Konfiguration, ein Modellwechsel deshalb kein Codewechsel.

Der frühere teuerste Posten — die nackte Zelle von Hand herstellen: Fähigkeiten-Liste
zusammenstreichen, geprüfte Operationen stilllegen, Gate abschalten, ein halber Tag —
**wandert mit auf die offene Werkzeugachse (L−)**. Er ist nicht erledigt, nur nicht
mehr Voraussetzung der ersten Messung.

Und die noch frühere Fassung brauchte einen Schalter mit vier Stufen und je
Capability einen Kurzmodus, geschätzt 150–200 Zeilen. Der entfällt endgültig: Er
diente allein dazu, „plus Instruktionen" von „plus Werkzeugführung" zu trennen, und
diese Trennung gibt es nicht mehr.

Dazu kommt Kleinkram: ein Feld in der Ergebnishistorie, das die Zelle festhält, und
eine Vergleichsansicht im Report. Das Prompt-Set wird **zum 2026-09-09 eingefroren**
— nach dem Start nicht mehr anfassen, sonst misst man sich selbst. Zuletzt geändert
wurde es am Tag davor, und zwar ausschließlich pfadneutral: Erfolgskriterien, die
ein bestimmtes Werkzeug vorschrieben, nennen jetzt das *Ergebnis*
(„die erreichbare Fläche folgt dem Wegenetz" statt „über `qgis_service_area`"), und
`tools_expected` führt zu jedem Werkzeug auch sein gleichwertiges Geschwister. Beides
ist Vorbedingung für einen fairen Modellvergleich: Sonst bestraft die Bank ein
Modell dafür, dass es einen anderen, genauso richtigen Weg nimmt.

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

43 Aufgaben × 3 Wiederholungen = 129 Läufe je Zelle. Bei gemessenen ~13 Minuten je
lokalem Lauf sind das rund **28 Stunden für L+** und, weil ein gehostetes Modell
schneller antwortet, etwa 8 Stunden für F+ — zusammen **vier Nächte**, nicht zwei.
Die alte Schätzung stammte aus der Zwölf-Aufgaben-Fassung; die Bank ist inzwischen
auf 43 gewachsen. Wer schneller fertig sein muss, kürzt **nicht** an den
Wiederholungen — der Vorversuch (§6) zeigt, warum — sondern misst auf einer Teilmenge,
die **vorher** benannt wird.

| Phase | Was | Dauer |
|---|---|---|
| P0 | Vorversuch, ohne Code | ✔ erledigt |
| P1 | ~~Vergleichszelle von Hand herstellen~~ | entfällt — F+ ist ein Konfigurationswert |
| P2 | Messapparat (Zellen-Feld, Vergleichsansicht) | ✔ erledigt (2026-09-12) |
| P3 | ~70 Läufe | 2 Nächte |
| P4 | Auswertung, Text | 1 Tag |
| P5 | Werkzeugachse gehostet: Zelle F− (Browser) | läuft — kein Bau nötig |
| P6 | Werkzeugachse lokal: Zelle L− bauen und messen | offen, ½ Tag + 1 Nacht |

**Eine harte Voraussetzung**, die vor allem anderen steht: F+ braucht einen
API-Zugang zu einem Frontier-Modell. Ohne den ist dieser Aufbau nicht messbar.

## 8. Was am Ende dasteht

Nach der ersten Messung ein Satz dieser Form:

> *„Auf demselben Werkzeugkasten, derselben Maschine und denselben Aufgaben liegt
> ein 26B-Modell auf einem Laptop \<so weit\> hinter einem Frontier-Modell — und die
> Fehler, die es zusätzlich macht, sind \<von dieser Art\>."*

Liegt die Differenz nahe null, ist das die stärkere Aussage: Dann kauft mehr Modell
auf dieser Aufgabenklasse nichts mehr, weil der Werkzeugkasten die Decke bereits
erreicht. Liegt sie hoch, beziffert sie, wie viel Kopf der Werkzeugbau noch hat.
**Beide Ausgänge sind verwertbar**; das ist der Vorteil davon, nur einen Faktor zu
bewegen.

Erst zusammen mit der Werkzeugachse (F+ ↔ F−, und später L−) entsteht daraus die
Aussage, auf die das Vorhaben zielt und die niemand heute belegen kann:

> *„Der Werkzeugkasten trägt ein kleines lokales Modell über \<diese Distanz\> —
> und der Unterschied liegt nicht im Wissen, sondern darin, dass das Wissen in den
> Rückgabewerten der Werkzeuge steckt."*

Damit wird aus der üblichen Modellfrage („welches lokale Modell schafft
Geo-Tool-Calling?") etwas Haltbareres: eine Aussage über **Werkzeugbau**, die nicht
veraltet, sobald das nächste Modell erscheint. Ein Modellranking ist in sechs
Monaten Altpapier; „das Wissen gehört in den Rückgabewert" gilt auch dann noch.

Im ungünstigen Fall steht dort das Gegenteil — dass das Frontier-Modell auch mit
identischem Werkzeugkasten klar vorne liegt und der Werkzeugbau die Lücke nicht
schließt. Auch das wäre eine brauchbare Aussage, und sie würde hier genauso stehen.

## 9. Was diesen Plan kippen könnte

Fünf Dinge, vorher benannt, damit sie hinterher nicht wegerklärt werden:

- **Kein Zugang zum Frontier-Modell.** Dann entfällt die Vergleichszelle, und die
  Kompensationsfrage bleibt eine qualitative These, belegt am bbox-Fall aus §3. Ein
  zulässiger Ausgang, kein Scheitern.
- **Streuung auch bei drei Wiederholungen.** Der Vorversuch zeigt sie deutlich; drei
  Läufe könnten zu wenig sein. Erkennbar daran, dass viele Aufgaben bei 2/3 landen —
  dann ist der Unterschied zwischen den Zellen nicht mehr ablesbar.
- **Deckeneffekt** — die neue Gefahr dieses Zuschnitts, und die Kehrseite des alten
  Bodeneffekts. Wenn der Werkzeugkasten die Aufgaben so weit vorbereitet, dass
  **beide** Zellen fast alles lösen, misst der Vergleich nichts mehr. Erkennbar
  daran, dass L+ und F+ beide nahe 3/3 liegen. Dann ist nicht die Messung kaputt,
  sondern die Bank zu leicht für diese Frage — und die Antwort lautet nicht
  „nachschärfen" (die Bank ist eingefroren), sondern: berichten, und die Aussage auf
  die Fehlerverteilung und die Laufkosten stützen statt auf die Quote.
- **Ungleiche Rahmenbedingungen trotz gleichem Werkzeugkasten.** Zwei bleiben:
  Kontextfenster und Zeitbudget. `model.timeout_seconds` ist auf das lokale Modell
  eingestellt; ein gehostetes Modell läuft darunter nie ins Limit, das lokale
  gelegentlich schon. Ein Abbruch wegen Zeit ist deshalb **als solcher zu
  berichten**, nicht als inhaltlicher Fehlschlag.
- **Die Kompensationsfrage bleibt unbeantwortet.** Der Zuschnitt vom 2026-09-08
  tauscht Mehrdeutigkeit gegen Unvollständigkeit: Statt eines Vergleichs, der zwei
  Faktoren mischt, gibt es jetzt einen sauberen — und eine offene zweite Achse. Wer
  aus L+ ↔ F+ eine Aussage über den Werkzeugbau macht, überdehnt den Befund (§4).
