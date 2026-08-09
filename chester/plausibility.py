"""Domain plausibility bands — magnitude sanity for validation (pure, stdlib only).

Part of the validation level-1 floor (doc/validation-concept.md, Ebene 1 → V1).
Each band is a ``(min, max, unit)`` sane range for one physical magnitude: a
laser-measured LoD2 height of 5000 m or a 0 m² building footprint is a data error,
not a very tall / very small building. The bands are a deterministic reference the
model doesn't have to guess — not *truth* (that needs ground control), just a floor
that catches gross unit/magnitude mistakes (metres vs degrees, m² vs km²).

No dependencies beyond stdlib, like ``adminlevels``/``geofacts`` — one source of
truth shared by ``sanity_check_result`` and the skills.
"""

from __future__ import annotations

# magnitude key → (min, max, unit). Deliberately wide: these bound the *absurd*,
# not the typical. Extend as new magnitudes earn a band.
BANDS: dict[str, tuple[float, float, str]] = {
    "building_height": (1.0, 200.0, "m"),        # a storey to a skyscraper
    "building_area": (4.0, 200_000.0, "m2"),     # a shed to a hangar
    "population": (0.0, 40_000_000.0, "count"),
    "population_density": (0.0, 50_000.0, "per_km2"),
    "slope": (0.0, 90.0, "degree"),
    "elevation": (-500.0, 9_000.0, "m"),          # Dead Sea shore to Everest
    "area_m2": (0.0, 1e12, "m2"),                 # ≤ ~1 000 000 km²
    "length_m": (0.0, 1e8, "m"),                  # ≤ ~100 000 km
}


def band(magnitude: str) -> tuple[float, float, str] | None:
    """The ``(min, max, unit)`` band for a magnitude key, or ``None`` if unknown."""
    return BANDS.get(magnitude)


def check_value(magnitude: str, value) -> str | None:
    """Return a one-line problem string if ``value`` is outside the band, else ``None``.

    An unknown magnitude or a non-numeric value yields ``None`` (nothing to say),
    so callers can pass either safely.
    """
    b = BANDS.get(magnitude)
    if b is None:
        return None
    lo, hi, unit = b
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v < lo:
        return f"{value} {unit} is below the plausible {magnitude} range [{lo}, {hi}] {unit}"
    if v > hi:
        return f"{value} {unit} is above the plausible {magnitude} range [{lo}, {hi}] {unit}"
    return None


def check_series(magnitude: str, values) -> dict | None:
    """Count how many of ``values`` fall outside the band.

    Returns ``{magnitude, unit, min, max, checked, below, above, out_of_band}`` or
    ``None`` for an unknown magnitude. Non-numeric entries are skipped (not counted
    as out of band), so a mixed column doesn't produce spurious hits.
    """
    b = BANDS.get(magnitude)
    if b is None:
        return None
    lo, hi, unit = b
    below = above = checked = 0
    for x in values:
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        checked += 1
        if v < lo:
            below += 1
        elif v > hi:
            above += 1
    return {
        "magnitude": magnitude,
        "unit": unit,
        "min": lo,
        "max": hi,
        "checked": checked,
        "below": below,
        "above": above,
        "out_of_band": below + above,
    }
