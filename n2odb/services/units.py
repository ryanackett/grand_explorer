"""Per-field unit options for the mapping step.

Contributors' data arrives in a wide variety of units, so instead of asking them
to match GRAND's canonical unit, the mapping step lets them declare **what unit
their column is in**; conversion to canonical happens downstream. Each list below
is ordered with the canonical/most-common unit first (used as the default).

``extract_unit_hint`` pulls a likely unit out of a header (a parenthetical, a
trailing token, or a ``°C``-style suffix) so the dropdown can pre-select it —
casting the widest net while staying editable.
"""
from __future__ import annotations

import re

_FLUX_UNITS = [
    "g N/ha/d", "kg N/ha/d", "mg N/m²/h", "µg N/m²/h", "g N/ha/h",
    "mg N/m²/d", "ng N/m²/s", "µmol N/m²/s",
]
_TEMP_UNITS = ["°C", "°F", "K"]
_CONC_UNITS = ["mg N/kg", "µg N/g", "mg/kg", "ppm", "kg N/ha"]
_DEPTH_UNITS = ["mm", "cm", "in"]

# field id -> ordered unit choices (canonical first). Fields not listed here have
# no unit concept (dates, day-of-year, categorical/text fields) — or are reported
# consistently enough that a unit choice is unnecessary (VWC/WFPS are effectively
# always %, so no conversion is needed).
UNIT_OPTIONS: dict[str, list[str]] = {
    "n2oflux": _FLUX_UNITS,
    "fluxstandarderror": _FLUX_UNITS,
    "soilt": _TEMP_UNITS,
    "airt": _TEMP_UNITS,
    "airtmax": _TEMP_UNITS,
    "airtmin": _TEMP_UNITS,
    "precip": _DEPTH_UNITS,
    "soilnh4": _CONC_UNITS,
    "soilno3": _CONC_UNITS,
    "nitrogenapplied": ["kg N/ha", "g N/m²", "lb N/acre"],
    "irrigationapplied": ["mm", "cm", "in", "L/m²"],
}


def options_for(field_id: str) -> list[str]:
    return UNIT_OPTIONS.get(field_id, [])


def canonical_unit(field_id: str) -> str | None:
    """The canonical unit for a field — the first option (see module docstring)."""
    opts = UNIT_OPTIONS.get(field_id)
    return opts[0] if opts else None


# Linear conversion factors *to the canonical unit*, grouped by the canonical unit
# (the first entry in each UNIT_OPTIONS list). value_canonical = value_source * factor.
# Temperature is affine, not a factor, so it's handled separately below.
#
# Areal/time factors: 1 ha = 10 000 m², 1 d = 24 h = 86 400 s.
_FACTORS: dict[str, dict[str, float]] = {
    # flux → g N/ha/d
    "g N/ha/d": {
        "g N/ha/d": 1.0, "kg N/ha/d": 1000.0, "g N/ha/h": 24.0,
        "mg N/m²/h": 240.0,          # /1000 g ·10 000 m²/ha ·24 h/d
        "µg N/m²/h": 0.24,           # /1e6 g ·10 000 ·24
        "mg N/m²/d": 10.0,           # /1000 ·10 000
        "ng N/m²/s": 0.864,          # /1e9 ·10 000 ·86 400
        "µmol N/m²/s": 12096.0,      # ·14 g N/mol ·1e-6 ·10 000 ·86 400 (µmol of N, not N₂O)
    },
    # soil mineral N concentration → mg N/kg
    "mg N/kg": {"mg N/kg": 1.0, "µg N/g": 1.0, "mg/kg": 1.0, "ppm": 1.0},
    # depth (precip/irrigation) → mm
    "mm": {"mm": 1.0, "cm": 10.0, "in": 25.4, "L/m²": 1.0},
    # nitrogen applied → kg N/ha
    "kg N/ha": {"kg N/ha": 1.0, "g N/m²": 10.0, "lb N/acre": 1.120851},
}


def _to_celsius(value: float, from_unit: str) -> float:
    if from_unit == "°F":
        return (value - 32.0) * 5.0 / 9.0
    if from_unit == "K":
        return value - 273.15
    return value


def convert(field_id: str, value, from_unit: str | None):
    """Convert ``value`` (in ``from_unit``) to ``field_id``'s canonical unit.

    Best-effort and non-destructive: an unknown/blank source unit, a unit already
    canonical, or a unit with no known conversion for that field is returned
    unchanged — normalization must never silently corrupt a value it can't convert.
    """
    if value is None:
        return None
    target = canonical_unit(field_id)
    if not target or not from_unit or from_unit == target:
        return value
    if target == "°C":
        return _to_celsius(value, from_unit)
    factor = (_FACTORS.get(target) or {}).get(from_unit)
    if factor is None:
        return value
    return value * factor


_PARENS = re.compile(r"[\(\[\{]([^)\]\}]+)[\)\]\}]")
_DEGREE = re.compile(r"[°˚º]\s*([CFK])\b", re.IGNORECASE)
_TRAILING = re.compile(r"[ _\-]([a-zA-Zµ%]{1,4})$")
# Only treat a trailing token as a unit when it's a recognisable one, so
# "Rain_mm" -> "mm" but "plot_id" stays blank.
_TRAILING_UNITS = {"mm", "cm", "m", "in", "km", "c", "f", "k", "kg", "g",
                   "mg", "ug", "ppm", "ha", "pct"}


def extract_unit_hint(header: str) -> str:
    """Best-effort raw unit string pulled from a column header ("" if none)."""
    if not header:
        return ""
    m = _PARENS.search(header)
    if m:
        return m.group(1).strip()
    m = _DEGREE.search(header)
    if m:
        return "°" + m.group(1).upper()
    m = _TRAILING.search(header.strip())
    if m and m.group(1).lower() in _TRAILING_UNITS:
        return m.group(1).strip()
    return ""
