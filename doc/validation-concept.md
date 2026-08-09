# Ergebnis-Validierung — ein Konzept

> Dieses Dokument bündelt Chesters **Korrektheits**-Prüfung zu einer Systematik.
> Es baut auf drei Ebenen auf (billig/deterministisch → visuell → redundant) und
> sagt für jede: *was es schon gibt*, *was zu erstellen wäre* und *welche Funktion
> in SelmaKit dafür fehlt*. Der visuelle Kanal (Ebene 2) hat sein eigenes
> Begleitdokument [`visual-validation.md`](./visual-validation.md); die
> Aggregat-Eskalation (Ebene 3) knüpft an [`data-escalation.md`](./data-escalation.md)
> an.
>
> **Status: Ebene 1 gebaut (V1: Attribut-Vollständigkeit + Plausibilitätsbänder;
> V2: Topologie — `check_topology`, inkl. Dangles via `network=True`; offen nur die
> Einheiten-Sanity), Ebene 2 gebaut **und ins Gate verdrahtet (V4, Stufe ≥2, beratend)**,
> Ebene 3 gebaut **(V5: `cross_check`-Tool + `cross-check`-Skill + Gate-Auto-Check
> Stufe 3, beratend)**.
> Das erzwingende Gate ist als stufenbasierter `output_validator` +
> `/valid_level` **gebaut** (§4.1, `chester/gate.py`, in `gateway.py` *und* `ask.py`
> registriert) — die dafür nötigen SelmaKit-Hooks (Passthrough + run-geschnittener
> Artefakt-Zugriff, §5) sind vorhanden. Aktiv erzwungen wird **Ebene 1 (Level 1)**: ein
> in *diesem* Run erzeugtes **und** in der Antwort erwähntes Dataset wird strukturell
> geprüft (leer / kaputte-leere-Null-Geometrie / fehlende CRS), ein echter Defekt löst
> **einmalig** `ModelRetry` aus. Die Level-2/3-Zweige erzwingen heute denselben
> Level-1-Boden und rufen best-effort Hooks (`_visual_problems`/`_redundancy_problems`),
> die mit V4/V5 scharf werden — ohne Oberflächenänderung.**

## 0. These

Ein Geo-Ergebnis ist objektiv richtig oder falsch — aber „Korrektheit" ist **keine
eine Eigenschaft, sondern mehrere Dimensionen** (ISO 19157: logische Konsistenz,
Vollständigkeit, Positions-/thematische Genauigkeit, zeitliche Qualität,
Nutzbarkeit). Entscheidend fürs Design:

- **Nur die logische Konsistenz und ein Teil der Vollständigkeit sind selbstständig
  prüfbar** — ohne externe Wahrheit, billig, deterministisch. Das ist Ebene 1.
- **Positions- und thematische Genauigkeit** — die eigentliche „stimmt es wirklich"-
  Frage — brauchen per Definition Referenzdaten, die Chester meist nicht hat. Nur
  näherungsweise über Redundanz (Ebene 3) erreichbar, formal (Confusion-Matrix/RMSE)
  gar nicht ohne Ground Truth.

Folgerung für die **Erzwingung**: Ein Pflicht-Gate kann realistisch nur den
Ebene-1-Boden garantieren („nicht offensichtlich kaputt"), nicht „korrekt". Das ist
wertvoll — es fängt Chesters häufigste *echte* Fehler (falscher Extent → leer/zu
viel, falsche CRS, falsche Einheit, degenerierte Geometrie) — aber die Doc-Sprache
muss ehrlich bleiben: erzwungen wird ein *struktureller Boden*, nicht Wahrheit.

Chester behandelt die Validierung heute als **Instruktion**, nicht als erzwungene
Phase: die Tools existieren, aber nichts hindert das Modell daran, ein Ergebnis ohne
Prüfung zu melden (siehe §4).

---

## Ebene 1 — strukturell (billig, deterministisch, keine Referenz)

Die klassische GIS-QA-Werkzeugkiste (ESRI Data Reviewer, QGIS-Topologieprüfung,
FME-Checklisten), in-process über den Geo-Stack — nie ein `qgis_process`-Subprozess
pro Datei (zu langsam; dieselbe Regel wie `chester/geofacts.py`).

### Was es gibt

| Prüfung | Ort | Status |
|---|---|---|
| CRS-Stimmigkeit (metrisch fürs Messen) | `check_crs` (`capabilities/validation.py`) | ✅ |
| Geometrie-Validität (OGC), leer/Null | `sanity_check_result` → `geofacts.vector_facts(full=True)` (`geom_null/empty/invalid` via shapely `is_valid`) | ✅ |
| Feature-Zahl in Band | `sanity_check_result(min_features, max_features)` | ✅ rudimentär |
| Geometrietyp-Erwartung | `sanity_check_result(expected_geometry)` | ✅ |
| Leere Attributspalten | `geofacts` (`columns_empty`, `populated_columns`) | ✅ (nur berichtet, nicht bewertet) |
| Attribut-Vollständigkeit (Null/Platzhalter/Range je Feld) | `geofacts.attribute_facts` + `sanity_check_result(required, ranges)` | ✅ **V1 gebaut** |
| Plausibilität / Magnitude-Band | `chester/plausibility.py` (`BANDS`) + `sanity_check_result(magnitude_field, magnitude)` | ✅ **V1 gebaut** |
| Topologie (Selbstschnitt/Überlappung/Duplikate/Lücken) | `geofacts.topology_facts` + `check_topology`-Tool | ✅ **V2 gebaut** |

### Was fehlt

1. ~~**Topologie**~~ — **V2 gebaut** (`check_topology`): paarweise Überlappungen
   (räumlicher Self-Join), Selbstschnitt (`is_valid`/`is_simple`), Duplikat-Geometrien
   und Lücken (Löcher in der vereinigten Deckung) — alle *in-process*. **Netz-Topologie:**
   `check_topology(network=True)` erkennt **Dangles** (freie Linienenden / degree-1-Knoten)
   in-process, mit optionalem `dangle_length` für kurze Überstände (die
   `rmdangle`-Idee). Bewusst *nicht* über GRASS: dieser QGIS-Build **listet** zwar den
   GRASS-Provider (307 Algos), hat aber **kein lauffähiges GRASS-Backend** — `grass:*`
   scheitert zur Laufzeit („GRASS was not found"). Die in-process-Lösung läuft überall.
2. ~~**Attribut-Vollständigkeit / Wertebereich**~~ — **V1 gebaut**: `attribute_facts`
   zählt Null/Platzhalter (`"NULL"`, `-9999`, `""`)/Out-of-range je Feld,
   `sanity_check_result(required=…, ranges=…)` bewertet gegen die Erwartung, und das
   Gate zieht die „Spalte komplett Sentinel = fehlgeschlagener Join"-Prüfung
   (strikter Satz ohne `""`) in seinen Level-1-Boden.
3. ~~**Plausibilität / Magnitude**~~ — **V1 gebaut**: `chester/plausibility.py` hält
   die `(min, max, unit)`-Bänder (Gebäudehöhe 1–200 m, Fläche, Dichte, Hangneigung,
   Höhe …), `sanity_check_result(magnitude_field, magnitude)` warnt vor
   implausiblen Werten. **Offen bleibt** die explizite **Einheiten-Sanity** (Meter
   vs. Grad, m² vs. km² aus CRS + Formel) — sie bleibt vorerst bei `check_crs` +
   Instruktion.

### Was zu erstellen wäre

- ✅ **`geofacts.attribute_facts(path, required=[...], ranges={...},
  placeholder_strings=…, placeholder_numbers=…)`** — Null-/Platzhalter-/
  Out-of-range-Zählung je Feld, plus `all_placeholder` (jede besetzte Zelle ein
  Sentinel) und `missing_required`. **V1 gebaut.**
- ✅ **Erweiterung von `sanity_check_result`** um `required`, `ranges`,
  `magnitude_field`+`magnitude` — warnt vor Platzhalter-gesättigten Spalten,
  fehlenden Pflichtfeldern, Out-of-range- und implausiblen Werten (`warnings`
  wächst, kein neues Tool). **V1 gebaut.**
- ✅ **Domänen-Bänder `chester/plausibility.py`** (`BANDS`): je Größe (Höhe, Fläche,
  Dichte, Hangneigung, Höhe …) ein `(min, max, unit)`-Band + `check_value`/
  `check_series`; von `sanity_check_result`/Skills referenziert, kein Rätselraten.
  **V1 gebaut.**
- ✅ **Reiner Reader `geofacts.topology_facts(path, check_overlaps=, max_overlap_features=)`**
  → `{invalid, not_simple, duplicate_geometries, self_overlaps, union_holes,
  overlap_checked, feature_count}`. **V2 gebaut.**
- ✅ **Neues Tool `check_topology(path, check_overlaps=, max_features=)`** in
  `GeoValidationCapability` — getrennt vom Standard-Sanity-Check, damit der billig
  bleibt. **V2 gebaut.**
  - **Design-Entscheidung:** der teure paarweise Teil läuft **in-process** (geopandas
    `sjoin` mit STRtree, `union_all`), **nicht** über `qgis_process` — die
    geofacts-Regel „nie ein Subprozess pro Datei" gilt auch hier (ein `sjoin` ist
    billiger als Qt-Init + Provider-Load). „Opt-in/teuer" ist deshalb der
    Overlap/Gap-Scan selbst: er wird oberhalb `max_features` (Default 20 000) oder mit
    `check_overlaps=False` übersprungen (`overlap_checked` meldet, ob er lief).
- ⏳ **Einheiten-Sanity** (Meter vs. Grad, m² vs. km² aus CRS + Formel) — noch offen,
  bleibt vorerst bei `check_crs` + Instruktion.

**Aufwand:** relativ gering, rein additiv, deterministisch. Kein SelmaKit-Bedarf.
V1 (Attribut/Plausibilität) **und** V2 (Topologie inkl. Dangles via
`check_topology(network=True)`) sind gebaut; offen bleibt nur die Einheiten-Sanity.

---

## Ebene 2 — visuell (`inspect_map`)

Ein zweiter Kanal neben den Zahlen: das Ergebnis rendern und *ansehen* — fängt, was
Zahlen verpassen (falscher Extent, falsche Skala, kaputter Join → einfarbige
Choroplethe, „ausblutende" Klassifikation).

### Was es gibt — **gebaut**

- `inspect_map(layers=[...])` (`capabilities/mapoutput.py`): statischer
  PNG-Snapshot (matplotlib) + Fakten-Summary je Layer, zurückgegeben als
  Bild-Content zum Ansehen.
- **Fallback-Vision-Modell**: `via_vision_model=True` schickt den Snapshot an
  `model.vision_model` (z. B. `ollama/llava:latest`), das ein schriftliches Urteil
  zurückgibt — für den text-only Hauptfall (Chesters Default `gemma4:26b` sieht
  nicht selbst).
- Der `review-result`-Skill trägt die Politik. Vollständiges Design:
  [`visual-validation.md`](./visual-validation.md).

### Erzwingung — **V4 gebaut**

- **Ins Gate verdrahtet:** auf Validierungsstufe ≥2 rendert das Gate das gemeldete,
  strukturell saubere Ergebnis und holt ein Vision-Urteil ein
  (`gate._visual_problems` → `mapoutput._render_snapshot` + `_ask_vision_model`).
  Das Urteil ist **beratend**: es wird als Notiz an die Antwort gehängt, löst **keinen**
  Retry aus (das Vision-Urteil ist subjektiv, §7 des Begleitdocs) — anders als der
  harte Level-1-Boden. Nur das erste gemeldete Ergebnis wird geprüft (Kosten-Deckel).
- **Snapshot mit Basiskarte:** `_render_snapshot` legt best-effort eine OSM-Basiskarte
  unter die Vektoren (`contextily`), sonst wäre Fehlplatzierung (Ozean/falsche
  Hemisphäre) auf weißem Grund nicht erkennbar. Fehlt Netz/`contextily`, fällt es auf
  den schlichten Plot zurück.
- **Bleibt konfigurationsabhängig:** ohne `model.vision_model` ist die Prüfung inert
  (dokumentierte Einschränkung, kein Fehler) — Stufe 2 verhält sich dann wie Stufe 1.
- **Phase D (Abnahme-Eval)** ist als opt-in Test gebaut
  (`test_visual_check_catches_misplaced_layer`, lon/lat-Vertauschung → Ozean,
  `llm`+`network`-markiert).

---

## Ebene 3 — Redundanz / Kreuzvergleich (mittel, braucht zweite Quelle/Methode)

Fängt Positions-/thematische Fehler *ohne* formale Ground Truth, indem dasselbe
Ergebnis über einen unabhängigen Weg gegengeprüft wird.

### Bausteine (vor V5 vorhanden, jetzt vom `cross_check`-Tool/Skill genutzt)

- **Aggregat-Eskalationskette**: `region_hierarchy(code)` (`chester/adminlevels.py`)
  liefert die Containment-Kette (Gemeinde → Kreis → Land → Bund). Damit ist der
  „Summe der Gemeinden ≈ Kreis-Total"-Abgleich *möglich* — die Kette existiert,
  der Vergleich ist aber nicht als Prüfung verdrahtet.
- **Zwei-Wege-Höhe als Daten vorhanden**: LoD2 `measured_height` (`fetch_lod2`)
  vs. DSM−DTM — beide Methoden existieren, ein automatischer Abgleich nicht.
- **place-vs-bbox**: die bbox-Feature-Tools geben eine `warning`, wenn ohne `place`
  aufgerufen (ein impliziter Redundanzhinweis, kein Vergleich).

### Erzwingung / Werkzeuge — **V5 gebaut**

- ✅ **`cross_check`-Tool** (`GeoValidationCapability`) mit drei Modi:
  - `mode="reasonableness"` — Zahl vs. bekannte Referenz (relative Toleranz);
  - `mode="aggregate"` — ein Feld über einen Layer summieren und gegen einen
    `expected_total` prüfen (Summe der Gemeinden ≈ Kreis-Total; Elternwert über
    `region_hierarchy`), backed by `geofacts.measure_layer`;
  - `mode="two_method"` — zwei Layer auf einem Schlüssel joinen und die
    Differenzverteilung berichten (LoD2 `measured_height` vs. DSM−DTM), backed by
    `geofacts.compare_layers`.
- ✅ **`cross-check`-Skill** trägt die Politik (wann/wie: die drei Muster oben,
  Orchestrierung `stats_table` + `region_hierarchy` + `cross_check`) — die im Konzept
  empfohlene „erst Skill-Politik"-Reihenfolge, hier zusammen mit dem Tool.
- ✅ **Gate-Auto-Check (Stufe 3)**: der eine *voraussetzungslose* Redundanzfall —
  eine gespeicherte `area`/`length`-Spalte gegen die neu berechnete Geometrie
  (`geofacts.area_length_consistency`), **beratend** (Notiz, kein Retry). Alles
  Fallabhängige (zweite Quelle nötig) bleibt Tool + Skill, weil das Gate den
  Erwartungswert/die zweite Quelle nicht autonom kennt.

### Außerhalb des Konzepts (der Vollständigkeit halber)

Formale Genauigkeit — **Confusion-Matrix** (thematisch) / **RMSE gegen
Kontrollpunkte** (Position) — nur sinnvoll, wenn der Nutzer Referenzdaten
mitbringt. Nicht selbst-prüfbar, daher kein Kern-Feature.

---

## 4. Erzwingung — von der Instruktion zur Phase

Heute sind alle drei Ebenen **instruktionsgetrieben**: die Tools existieren, aber
kein Code hindert das Modell daran, ein Ergebnis ungeprüft zu melden. Der Doc-Satz
„Validierung ist Pflichtphase" beschreibt eine Absicht, keinen Mechanismus.

### Empfohlenes Gate (ergebnis-basiert, nicht ritual-basiert)

Ein `output_validator`, der *nach* der finalen Antwort läuft:

1. Finde Datasets, die **in diesem Run erzeugt** (Provenance-Sidecar `created_at`)
   **und** in der Antwort **erwähnt** wurden (Basename-Match) — beide Bedingungen
   halten reine Q&A-/Zahl-Antworten und unsichtbare Zwischendateien unberührt.
2. Laufe die **Ebene-1-Checks in-process** (leer? kaputte Geometrie? Messung auf
   4326? Wertebereich?) — billig, kein Modell-Roundtrip.
3. Alles ok → Antwort unverändert durch (kein Retry, keine Kosten im Normalfall).
4. Echter Defekt → **einmalig** `pydantic_ai.ModelRetry` mit Datei + konkretem
   Mangel; danach mit angehängter Warnung durchlassen (kein Loop-Trap bei schwachen
   Modellen).

So erzwingt das Gate den *Ausgang* (das gemeldete Ergebnis wurde geprüft und ist
nicht offensichtlich kaputt), nicht das *Ritual* (wurde ein Tool aufgerufen) — und
bleibt im gesunden Normalfall still.

### 4.1 Stufen — `/valid_level` (gebaut)

Die Strenge des Gates ist **pro Session** über eine numerische Stufe einstellbar,
analog zu `/verbose` und `/think` (Session-Meta, kein Neustart, keine Config-
Änderung). Die Stufen entsprechen den drei Ebenen dieses Dokuments und sind
**kumulativ** — Stufe *n* läuft alle Prüfungen der Stufen 1…*n*:

| Level | Bedeutung | Ebene | Kosten |
|---|---|---|---|
| **0** | keine Validierung — Gate ganz aus, Antwort geht unverändert durch | — | keine |
| **1** | **strukturell** (leer? kaputte/leere/Null-Geometrie? fehlende CRS? unlesbar?) — in-process, deterministisch. **Default beim Start.** | 1 | billig, kein Modell-Roundtrip |
| **2** | zusätzlich **visuell**: Ergebnis rendern und (per Vision-Modell) auf groben Extent-/Skalen-/Join-Fehler prüfen | 1 + 2 | Render + ein Vision-Roundtrip; braucht gesetztes `model.vision_model` |
| **3** | zusätzlich **Redundanz**: Kreuzvergleich gegen eine zweite Quelle/Methode (Aggregat-Konsistenz über `region_hierarchy`, Zwei-Methoden-Höhe …) | 1 + 2 + 3 | mittel, teils fallabhängig / braucht zweite Quelle |

Bedienung:

- `/valid_level` — zeigt die aktuelle Stufe (mit Kurzbeschreibung).
- `/valid_level <n>` — setzt die Stufe für diese Session (`0`–`3`).

**Umsetzung.** Das Gate lebt in `chester/gate.py` (`make_validation_gate` →
`output_validator`-Coroutine), registriert über `agent_build.register_validation_gate`
aus **beiden** Entrypoints (`gateway.py` *und* `ask.py`), sodass es eine echte
Loop-Phase ist, nicht web-only. Der `/valid_level`-Command wohnt in
`register_geo_commands` (webchat) und schreibt die Stufe in die Session-Meta;
das Gate liest sie über dieselbe `SessionProxy` (`ctx.deps` = Session-Key), Default
Level 1. Die „erzeugt *und* erwähnt"-Auswahl nutzt den run-geschnittenen
`tool_returns(ctx)` (§5): jeder String im Tool-Ergebnis, der als Geodaten-Datei
auflöst (`resolve_path`) **und** dessen Basename/Stamm in der Antwort steht, wird
geprüft. Bei Level 0 kehrt das Gate sofort zurück; ab Level 1 laufen die
strukturellen Checks, die höheren Stufen rufen zusätzlich die Ebene-2/3-Hooks. Ein
echter Defekt löst **einmalig** `ModelRetry` aus (Datei + konkreter Mangel);
ein bereits erneuerter Run (bzw. ein erschöpftes Retry-Budget) wird mit angehängter
Warnung durchgelassen (Loop-Trap-Schutz, via `ctx.retry`/`max_retries`).

**Bewusst weggelassen:** die im Konzept genannte „Messung auf geografischer CRS"-
Prüfung ist *intentabhängig* (ein WGS84-Kartenlayer ist völlig in Ordnung) — als
Pflicht-Retry würde sie Fehlalarme produzieren, also bleibt sie beim
`check_crs`-Tool + der Instruktion, nicht im erzwingenden Gate. Level 1 erzwingt nur
den fehlerfreien Boden (leer / kaputt / keine CRS / unlesbar).

**Ehrlichkeitshinweis:** Deterministisch, billig und *hart erzwungen* (Retry) ist
**Level 1**. **Level 2 (V4)** und **Level 3 (V5)** sind gebaut, aber **beratend**:
Level 2 rendert + fragt `model.vision_model` (inert ohne Vision-Modell → verhält sich
wie Level 1); Level 3 prüft automatisch nur den voraussetzungslosen Fall (gespeicherte
`area`/`length` vs. Geometrie) und hängt eine Notiz an — der fallabhängige
Kreuzvergleich (zweite Quelle nötig) läuft über das `cross_check`-Tool + den
`cross-check`-Skill, nicht autonom im Gate. Kein beratender Befund löst je einen Retry
aus. Die Oberfläche bleibt über alle Stufen stabil.

---

## 5. SelmaKit-Hooks (vorhanden)

Chester erweitert SelmaKit, forkt es nicht. Das Gate braucht saubere Hooks statt
Zugriff auf Interna — die sind **vollständig vorhanden**, es besteht **kein offener
SelmaKit-Bedarf** mehr. Dieser Abschnitt ist damit kein Anforderungskatalog mehr,
sondern der Bau-Zeiger: *welche* Fläche das Gate (V3) benutzt.

1. **`output_validator`-Passthrough** (SelmaKit `495cbb6`). Der SelmaKit-Wrapper
   `selmakit.agent.Agent` reicht `Agent.output_validator(func)` an den inneren
   pydantic-ai `Agent` durch (analog zum `@agent.command`-Passthrough). Chester
   registriert das Gate damit in `agent_build.py` (wie `register_geo_commands`), ohne
   auf das private `gw.agent._agent` zu greifen.
2. **Run-geschnittener Artefakt-Zugriff** (SelmaKit `dec0e62`). Aus dem Paket-Root
   importierbar (`from selmakit import run_messages, tool_returns`), nutzbar
   **innerhalb** eines `output_validator`:
   - `run_messages(ctx)` → die `ModelMessage`s seit Run-Beginn (Analogon zu
     `AgentRunResult.new_messages()`).
   - `tool_returns(ctx)` → `[(tool_name, content), …]` der `ToolReturnPart`s **nur
     dieses Runs**, in Aufrufreihenfolge — das Gate kommt so ohne Message-Walk an die
     Tool-Ergebnisse.

   Beide filtern `ctx.messages` über das **öffentliche `run_id`-Feld** (dieselbe Basis
   wie pydantic-ais `new_messages()`), also ohne Zeitfenster-Heuristik oder
   Message-Layout-Rekonstruktion. Das Gate soll sie direkt verwenden.
3. **Retry-Schutz.** `ctx.retry`/`max_retries` tragen den Loop-Trap-Schutz (§4,
   Schritt 4: einmalig `ModelRetry`, dann durchlassen); `ModelRetry` ist aus
   `pydantic_ai` importierbar. Keine Lücke.

**Chesters Rest-Aufgabe** (bewusst *nicht* SelmaKit-Sache): aus einem `ToolReturnPart`
den/die Dateipfad(e) zu ziehen — die Tool-Ergebnisse sind Chester-spezifische Dicts
ohne SelmaKit-weite „output_path"-Konvention. Das übernimmt das Gate über die
Provenance-Sidecars (`chester/provenance.py`) + Basename-Match gegen den Antworttext
(§4, Schritt 1).

---

## 6. Phasen (Vorschlag)

| Phase | Inhalt | Ebene | Aufwand | SelmaKit? |
|---|---|---|---|---|
| **V1** ✅ | `geofacts.attribute_facts` + `required`/`ranges`/`magnitude` in `sanity_check_result`; `chester/plausibility.py`-Bänder; Gate-Konsum (Sentinel-Sättigung). **Gebaut** (Einheiten-Sanity offen) | 1 | gering | nein |
| **V2** ✅ | `geofacts.topology_facts`/`dangle_facts` + `check_topology`-Tool (in-process `sjoin`/`union_all`; `network=True` → Dangle-Erkennung via Knotengrad, `dangle_length` für kurze Überstände). **Gebaut** | 1 | gering–mittel | nein |
| **V3** ✅ | SelmaKit-Hooks (§5) **+** stufenbasiertes, ergebnis-basiertes Gate (`chester/gate.py`, registriert via `agent_build.register_validation_gate`) mit `/valid_level` (§4.1) — **gebaut** | 4 | mittel | ja — Hooks erledigt |
| **V4** ✅ | Visueller Kanal ins Gate (Stufe ≥2): `gate._visual_problems` rendert + fragt `model.vision_model` (beratende Notiz), Snapshot mit OSM-Basiskarte (`contextily`); Phase-D-Eval opt-in. **Gebaut** | 2 | gering | nein |
| **V5** ✅ | `cross_check`-Tool (reasonableness/aggregate/two_method, backed by `geofacts.measure_layer`/`compare_layers`) + `cross-check`-Skill + Gate-Auto-Check (`area_length_consistency`, Stufe 3, beratend). **Gebaut** | 3 | mittel | nein |

**Reihenfolge-Logik:** V1/V2 heben den erzwingbaren Boden, V3 **erzwingt** ihn, V4/V5
ergänzen die beratenden Kanäle (visuell, Redundanz). **V1–V5 sind gebaut** — das Gate
erzwingt die strukturellen Level-1-Checks (inkl. V1-Attribut-/Sentinel-Prüfung) hart,
liefert ab Stufe 2 ein beratendes Vision-Urteil (V4) und ab Stufe 3 den
Area-vs-Geometrie-Auto-Check (V5); daneben stehen die opt-in Werkzeuge `check_topology`
(V2) und `cross_check` (V5) + der `cross-check`-Skill. **Damit ist Phase V im Kern
abgeschlossen.** Offen bleibt nur die Einheiten-Sanity. Die Netz-Topologie (Dangles)
ist mit `check_topology(network=True)` in-process abgedeckt (kein lauffähiges
GRASS-Backend auf dieser Maschine, s. o.).
