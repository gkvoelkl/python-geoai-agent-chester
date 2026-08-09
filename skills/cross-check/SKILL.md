---
name: cross-check
description: Confirm a geo result by an independent route before finalising — redundancy validation (level 3). Use when a number or layer can be checked a second way: sum-of-parts vs a known parent total (aggregate consistency via region_hierarchy), a value vs a known reference figure (reasonableness), or two methods for the same quantity (two_method, e.g. LoD2 measured height vs DSM−DTM). Orchestrates stats_table / region_hierarchy / qgis_field_sum with cross_check.
version: 1
---

# Cross-check a result (redundancy validation)

Correctness is a loop phase. `check_crs`/`sanity_check_result` catch structural
faults and `inspect_map` catches the visually obvious — but **positional and
thematic accuracy** ("is the number actually right?") can only be approached
through **redundancy**: confirm the same result along an *independent* route.
That is what `cross_check` is for (three modes).

## When to use
- A result that can be confirmed a **second way** — an official figure, a sum that
  should match a known total, or a quantity for which two methods exist.
- Especially after a statistics join (choropleth) or a height/area derivation.

## Pattern 1 — aggregate consistency (sum of parts ≈ parent value)
A per-Gemeinde table should sum to roughly the Kreis/Land total.
1. Determine the parent code with `region_hierarchy(code)` (Gemeinde `09375117` →
   Kreis `09375` → Land `09`).
2. Get the parent value (`stats_table` for the parent level, or a known figure).
3. `cross_check(mode="aggregate", path=<gemeinde table>, field=<value column>,
   expected_total=<parent value>, tolerance=0.05)`.
- **Rule: escalate the scope, keep the granularity** — never report the
  higher-level aggregate as the value of a missing unit (see
  `doc/data-escalation.md`). The sum only *checks* the parts, it does not
  *replace* them.

## Pattern 2 — reasonableness (number vs a known reference)
Check a computed number against a known figure (population, area, length).
- `cross_check(mode="reasonableness", value=<computed>, expected=<reference>,
  tolerance=0.1)`. The reference can come from `stats_table`, `web_search` or the
  task itself.

## Pattern 3 — two-method agreement (two_method)
Two independent methods for the same quantity should agree.
- **Building height:** LoD2 `measured_height` (`fetch_lod2`) vs DSM−DTM. Join both
  layers on a shared key (or intersect them spatially), then
  `cross_check(mode="two_method", path=<a>, field="measured_height", path_b=<b>,
  field_b=<height_diff>, key=<id>, tolerance=0.15)`.
- The result reports the distribution of the difference (mean/median/max, absolute
  and relative). A large deviation means one method is wrong (wrong DTM? wrong
  vertical datum?).

## Afterwards
- Within tolerance (`ok: true`) → result confirmed, carry on.
- Outside it (`ok: false`) → do **not** round cosmetically; diagnose the faulty
  step (wrong join key? wrong unit? wrong extent?) and recompute. This is a loop,
  not a final step.

## Related
The gate's automatic check (validation level 3, `/valid_level 3`) only covers the
input-free case — a stored `area`/`length` column against the geometry. Everything
case-dependent (needing a second source) goes through this skill plus
`cross_check`.
