# Chester — Einordnung in den Rahmen „Autonomous GIS"

Wo steht dieses Projekt, gemessen an einem Rahmen, den es nicht selbst gesetzt hat?

Grundlage ist das Visionspapier von **Li, Ning et al. (2025)**, das *Autonomous GIS*
über fünf Ziele, fünf Autonomiestufen, fünf Kernfunktionen und drei Betriebsskalen
definiert (vollständige Angabe unten). Es ist kein Benchmark und keine Messung,
sondern eine Forschungsagenda von achtzehn Autorinnen und Autoren — und damit eine
brauchbare Fremdachse: Sie ist nicht auf Chester zugeschnitten, also sagt eine
Einordnung darauf etwas.

Jede Behauptung unten trägt ihren Beleg aus diesem Repository. Wo Chester eine Stufe
**nicht** erreicht, steht das ebenso.

## Die Autonomiestufen

Das Papier staffelt Autonomie nach dem Vorbild der Fahrzeugautonomie, Stufe 0
(manuell) bis 5.

| Stufe | Kurzdefinition (Li & Ning et al.) | Chester |
|---|---|---|
| 1 · routine-aware | führt **vom Menschen definierte** Workflows aus (Model Builder) | darunter |
| 2 · workflow-aware | erzeugt Workflows selbst — auf **bereitgestellten** Daten | erfüllt: `qgis_search`/`qgis_run` über 761 Algorithmen, geprüfte Abkürzungen, `qgis_python` als Rückfallweg |
| **3 · data-aware** | beschafft und wählt Daten selbst, erkennt „applicability **and drawbacks**" der Datentypen, meldet mangelhafte Daten zur Prüfung | **erfüllt — der Schwerpunkt des Projekts** (Belege unten) |
| **4 · result-aware** | versteht Ergebnisse und passt Daten, Modell und Workflow **iterativ** an; Mensch nur noch mit Aufsicht | **teilweise** (Belege und Grenze unten) |
| 5 · knowledge-aware | selbstwachsend: leitet aus Erfolgen und Fehlschlägen allgemeine Regeln ab und ändert damit den eigenen Entscheidungskern | **nein** |

Dazu die Skala: Das Papier unterscheidet `local`, `centralized` und `infrastructure`
nach den verfügbaren Ressourcen und nennt den *GIS Copilot for QGIS* als Prototyp
einer **Stufe 2 auf lokaler Skala**. Chester läuft ebenfalls lokal — ein Rechner, ein
QGIS, ein Modell über Ollama.

**Die Einordnung in einem Satz: Chester ist ein Stufe-3-System mit einem Fuß in
Stufe 4, auf der `local scale`.** Das ist zugleich die Forschungsfrage des Projekts:
Wie weit trägt ein Werkzeugkasten die Autonomie auf der *beschränktesten* Skala? —
siehe [`tool-compensation.md`](./tool-compensation.md).

### Warum Stufe 3 belegt ist

Stufe 3 verlangt nicht „kann Daten laden", sondern **Umgang mit Unsicherheit in der
Datenbeschaffung**. Fünf Belege, alle aus protokollierten Läufen:

- **Eskalation auf amtliche Daten, wenn die naheliegende Quelle nichts hergibt.**
  Aufgabe „mittlere Geländehöhe je Stadtbezirk in Regensburg" (2026-08-27): OSM führt
  dort **keine** Stadtbezirks-Polygone — `boundary=administrative` liefert nur die
  Ebenen 8/6/5, `place=suburb` 36 Objekte mit zwei Flächen. Der Agent ging von selbst
  über `geodata_search` auf den amtlichen WFS der Stadt (18 Bezirke) und nahm statt
  des 30-m-DEM das 1-m-DGM1. Konzept:
  [`geodata-search.md`](./geodata-search.md) (Eskalation über Verwaltungsebenen: §7).
- **Werkzeuge sagen, was die Daten nicht können.** `fetch_dop` meldet `has_nir`; ein
  NDVI aus dem bayerischen DOP wird verweigert und auf Sentinel-2 umgeleitet, statt
  eine Zahl aus RGB zu erfinden (die Probe `ndvi-without-nir` auf Test-Level 2 —
  vorher der Bank-Fall `dop-ndvi-no-nir-bayern`, dreimal gefahren, dreimal bestanden).
- **Der Zuschnitt wird berichtet, nicht angenommen.** `osm_features(place=…)` schneidet
  auf die Verwaltungsgrenze und meldet die Kosten (`features_trimmed`,
  `area_outside_km2`). Ohne das lieferte dieselbe Aufrufkette für „Anteil des
  straßennahen Waldes" 20 % statt 95 % — dieselbe Prosa, dieselbe Note, falsche
  Antwort ([`code-map.md`](./code-map.md), Eintrag `chester/osmclip.py`).
- **Die Bounding-Box-Warnung im Rückgabewert**, nicht in der Instruktion: Regensburger
  Schulen 101 → 84, GTFS-Haltestellen 1544 → 1225.
- **Die Abdeckung wird gemeldet, nicht unterstellt.** Ihr §2.3.7 nennt unter den
  Unsicherheiten der Datenvorbereitung wörtlich *„Does it adequately cover the study
  area?"* — seit dem 2026-08-29 beantworten `fetch_dem`/`fetch_dgm1`/`fetch_dop` das im
  Rückgabewert (`covers_request`, getrennt nach Ausdehnung und nodata), und
  `qgis_zonal_stats` markiert Zonen, die das Raster nur teilweise füllt. Ohne das ist ein
  Mittelwert über 40 % eines Bezirks eine Zahl wie jede andere. Geprüft am Regensburger
  DGM1: volles Raster stumm, Westhälfte allein → 43 % Abdeckung und 7 von 18 Bezirken
  markiert, der schlechteste bei 1 %.

Das Muster hinter allen fünfen: **Wissen über die Daten gehört in den Rückgabewert des
Werkzeugs, nicht in den Systemprompt.** Instruktionen haben in diesen Fällen messbar
nicht getragen, Rückgabewerte schon.

### Warum Stufe 4 nur teilweise gilt

Stufe 4 verlangt, Ergebnisse zu verstehen **und iterativ zu optimieren**. Chester hat
die erste Hälfte gebaut und die zweite bewusst nicht:

- Gebaut ist ein **erzwingendes Prüftor** (`chester/gate.py`): struktureller Boden mit
  genau *einem* erzwungenen Neuversuch, Plausibilitätsbänder, Extent-Prüfung,
  Sichtprüfung durch ein Sehmodell, Strenge je Sitzung über `/valid_level`. Konzept:
  [`validation-concept.md`](./validation-concept.md).
- **Nicht** gebaut ist die Optimierschleife: Chester probiert keine Varianten durch,
  vergleicht keine Modellparameter und wählt nicht das beste Ergebnis. Ein Retry ist
  eine Korrektur, keine Iteration.

Die Grenze ist eine Entscheidung, keine Lücke im Bauplan: Ein Prüftor, das beliebig oft
zurückschickt, kann auf einem lokalen Modell endlos kreisen; die Kosten dafür trägt
niemand. Wer Stufe 4 vollständig will, muss zuerst sagen, wann eine Iteration
abzubrechen ist.

### Warum Stufe 5 nicht gilt

Chester hat ein Langzeitgedächtnis, aber es **wächst nicht**: Es speichert Notizen,
leitet aber keine allgemeinen Regeln aus Erfolgen und Fehlschlägen ab und ändert den
Entscheidungskern nicht. Die Regelbildung findet hier statt — im Repository, durch
Menschen und dokumentierte Befunde. Das ist ausdrücklich **nicht** Stufe 5 des Papiers.

## Drei Stellen, an denen Chester etwas beiträgt

Ein Visionspapier benennt offene Fragen. Bei dreien liegt in diesem Repository etwas
Laufendes statt einer Absichtserklärung.

**1. Was „korrekt" heißt (ihr Ziel *self-verifying*, §2.2.3).** Das Papier fragt
wörtlich: *„What constitutes 'correctness'? What criteria should be applied to
attributes, tables, images, and visualizations? At what level of accuracy should
results be considered acceptable?"* — und nennt als kritische Aufgabe, *„clear and
practical criteria to address uncertainty"* zu definieren. Chester beantwortet das
nicht theoretisch, sondern als laufenden Code: vier Prüfebenen, ein erzwungener
Neuversuch, Plausibilitätsbänder je Domäne, Stufen 0–3 pro Sitzung.

Ein Unterschied gehört dazu: Sie verlangen **Schrittverifikation** („mitigate the
uncertainty propagation"), Chester prüft aus Kostengründen das **Endergebnis**. Das
ist eine Position mit Preis, keine Auslassung — sie ist in
[`validation-concept.md`](./validation-concept.md) begründet.

**2. Ein Prüf-Agent, und was mit ihm schiefgeht (ihr §5.4.1).** Das Papier wünscht
sich *„a comprehensive benchmarking framework or a dedicated reviewer agent"* zur
Bewertung von Workflow *und* Ergebnis. Chester hat beides — Judge, deterministische
Werkzeug-Coverage, Aufwandsmaße ([`agent-test-prompts.md`](./agent-test-prompts.md))
— und daraus einen unbequemen Befund: **Ein Prüf-Agent kann richtig urteilen und dabei
falsch begründen.** Zweimal an zwei aufeinanderfolgenden Tagen (2026-08-26/27) traf
der Judge die richtige Note und stützte sie auf eine erfundene Tatsache. Wer Prüf-
Agenten als Vertrauensanker vorschlägt, muss diesen Fall behandeln; ihr eigener Satz
*„transparency alone does not guarantee trustworthiness"* zeigt in dieselbe Richtung.

**3. Zu wissen, was man nicht kann (ihr §5.1.3).** Das Papier fordert einen Benchmark
für Grundfertigkeiten und hält fest, ein System solle *„self-aware, knowing what it
can and cannot do"* sein. Chesters Bank ist aufgabenorientiert und deckt das nur
teilweise ab — aber ein Fall prüft genau diese Selbstkenntnis: die verweigerte
NDVI-Berechnung aus einem Luftbild ohne Infrarotband. Eine Absage als bestandene
Antwort ist in einem Aufgaben-Benchmark ein Fremdkörper und in ihrem Sinne der Kern.

## Was Chester aus dem Papier nicht übernimmt

- **Stufe 5 / self-growing** samt Feintuning des Entscheidungskerns: auf einem
  Einzelrechner nicht zu haben, und es widerspräche der Regel, die Agentenbibliothek zu
  erweitern statt zu forken.
- **Die Skalen `centralized` und `infrastructure`**: Die Beschränkung auf einen
  einzelnen Rechner ist bei Chester nicht Mangel, sondern Versuchsbedingung.
- **Den Anspruch, GIS zu „demokratisieren".** Chester setzt eine GIS-kompetente
  Leserin voraus; ein Ergebnis, das niemand prüfen kann, hilft niemandem.

## Quelle

> Li, Z., Ning, H., Gao, S., Janowicz, K., Li, W., Arundel, S. T., Yang, C., Bhaduri,
> B., Wang, S., Zhu, A.-X., Gahegan, M., Shekhar, S., Ye, X., McKenzie, G., Cervone,
> G., & Hodgson, M. E. (2025). *GIScience in the era of Artificial Intelligence: a
> research agenda towards Autonomous GIS.* **Annals of GIS**, online veröffentlicht am
> 12. September 2025. DOI: [10.1080/19475683.2025.2552161](https://doi.org/10.1080/19475683.2025.2552161)
> · Preprint: [arXiv:2503.23633](https://arxiv.org/abs/2503.23633)

Zitate im Text stammen aus der arXiv-Fassung (v3, 54 Seiten); Abschnittsnummern
beziehen sich auf diese. Vorgängerarbeit derselben Erstautoren: Li & Ning (2023),
*Autonomous GIS: the next-generation AI-powered GIS*, International Journal of Digital
Earth, DOI: [10.1080/17538947.2023.2278895](https://doi.org/10.1080/17538947.2023.2278895).
