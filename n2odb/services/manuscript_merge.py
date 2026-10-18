"""Merge deterministic-parser output with the language-model module's output.

Policy (manuscript plan §4): when a deterministic parser and the module both
propose a value for the same field, prefer the deterministic number, keep the
module's ``source_quote``, and flag a mismatch if they disagree beyond a
tolerance. Fields only one side proposes pass through unchanged.
"""
from __future__ import annotations

import re

# Relative tolerance for "these two numbers disagree" on coordinates and the like.
_REL_TOLERANCE = 0.01


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _disagree(a, b) -> bool:
    fa, fb = _as_float(a), _as_float(b)
    if fa is None or fb is None:
        return str(a).strip().lower() != str(b).strip().lower()
    scale = max(abs(fa), abs(fb), 1e-9)
    return abs(fa - fb) / scale > _REL_TOLERANCE


# Fields where the language model outranks the deterministic parser when both
# propose a value. For every other field the deterministic number wins (§4); for
# the abstract the module is told to copy the text verbatim, while the heading
# parser can pick up footnote/correspondence lines, so the module value is kept
# and the regex entry is only a fallback.
_MODULE_PRIORITY = frozenset({"pub_abstract"})


def _filled(entry) -> bool:
    return isinstance(entry, dict) and entry.get("value") not in (None, "")


def merge_metadata(regex_meta: dict, module_meta: dict) -> dict:
    """Return merged ``{field_id: entry}`` favouring deterministic values.

    Inputs and output are §5 metadata dicts. Non-destructive: neither input is
    mutated.
    """
    merged: dict = {}
    for field_id, entry in (module_meta or {}).items():
        merged[field_id] = dict(entry)

    for field_id, det in (regex_meta or {}).items():
        det = dict(det)
        mod = merged.get(field_id)
        if mod is None:
            merged[field_id] = det
            continue
        # Both propose: keep the deterministic value, borrow the module's quote if
        # the deterministic one is missing, and flag any real disagreement.
        if field_id in _MODULE_PRIORITY and _filled(mod):
            combined = dict(mod)
            if not combined.get("source_quote") and det.get("source_quote"):
                combined["source_quote"] = det["source_quote"]
            merged[field_id] = combined
            continue
        combined = dict(det)
        if not combined.get("source_quote") and mod.get("source_quote"):
            combined["source_quote"] = mod["source_quote"]
        if _disagree(det.get("value"), mod.get("value")):
            combined["mismatch"] = {
                "regex_value": det.get("value"),
                "module_value": mod.get("value"),
            }
        merged[field_id] = combined
    return merged


# --- unit normalisation (before pre-fill) -------------------------------------
#
# The model reports values and units exactly as the paper writes them ("42°24′ N",
# "365 g kg−1"); the site/soil forms expect GRAND's canonical units. Convert what we
# can recognise, keep the original alongside, and leave anything ambiguous (ranges,
# depth profiles, unknown units) untouched — never silently corrupt a value.

_ONE_NUMBER = re.compile(r"^\s*(?P<num>[-+−]?\d+(?:[.,]\d+)?)\s*(?P<rest>[^\d]*)$")
_DASH = re.compile(r"[−–—]")
# field -> [(unit regex, factor to canonical, canonical unit)]
_UNIT_RULES = {
    **{f: [(r"g\s*/?\s*kg|g\s*kg\s*-?\s*1", 0.1, "%")] for f in ("sand", "silt", "clay", "som")},
    "bulk_density": [(r"kg\s*/?\s*m\s*-?\s*3|kg\s*m", 0.001, "g cm⁻³"),
                     (r"(?-i:\bMg\b)|\bt\s*/?\s*m", 1.0, "g cm⁻³")],
    "map": [(r"\bcm\b", 10.0, "mm"), (r"\bin(?:ch(?:es)?)?\b", 25.4, "mm"), (r"^\s*m\b", 1000.0, "mm")],
}


def _coordinate(value, unit) -> tuple[float, bool] | None:
    """(signed decimal degrees, sign trustworthy) from a DMS or decimal string.

    A DMS value conventionally carries its hemisphere, so DMS without one is flagged;
    a plain unsigned decimal follows the signed convention (positive = N/E).
    """
    from .manuscript_regex import _DEG_DEC_RE, _DMS_RE, _dms_to_decimal

    s = f"{value} {unit or ''}".strip()
    m = _DMS_RE.search(s)
    if m and (m.group("min") or m.group("sec") or m.group("pre_hemi") or m.group("post_hemi")):
        if any(float((m.group(k) or "0").replace(",", ".")) >= 60 for k in ("min", "sec")):
            return None  # impossible minutes/seconds (a garbled read): leave it as written
        hemi = m.group("pre_hemi") or m.group("post_hemi")
        return _dms_to_decimal(m.group("deg"), m.group("min"), m.group("sec"), hemi), bool(hemi)
    m = _DEG_DEC_RE.search(s)
    if m:
        v = float(m.group("num").replace(",", "."))
        hemi = m.group("pre_hemi") or m.group("post_hemi")
        neg = m.group("sign") == "-" or (hemi or "").upper() in ("S", "W")
        return (-v if neg else v), True
    m = _ONE_NUMBER.match(_DASH.sub("-", str(value)))
    if m:
        v = float(m.group("num").replace(",", "."))
        hemi = re.search(r"\b([NSEW])\b", f"{m.group('rest')} {unit or ''}", re.IGNORECASE)
        if hemi and hemi.group(1).upper() in ("S", "W"):
            v = -abs(v)
        return v, True
    return None


def _convert(field_id: str, entry: dict) -> dict:
    value, unit = entry.get("value"), entry.get("unit") or ""
    if field_id in ("site_latitude", "site_longitude"):
        got = _coordinate(value, unit)
        if got is None:
            return entry
        v, signed = got
        limit = 90.0 if field_id == "site_latitude" else 180.0
        if abs(v) > limit:  # e.g. a garbled "151°82′" — leave for the contributor
            return entry
        out = {**entry, "value": round(v, 6), "unit": "°"}
        if str(value).strip() != str(out["value"]):
            out.update(original_value=value, original_unit=entry.get("unit"))
        if not signed:
            out["note"] = "Hemisphere not stated in the paper text; check the sign."
        return out
    m = _ONE_NUMBER.match(_DASH.sub("-", str(value or "")))
    if not m:
        return entry  # range, list, or text: leave it
    num = float(m.group("num").replace("−", "-").replace(",", "."))
    unit_text = _DASH.sub("-", f"{unit} {m.group('rest')}").strip()
    if field_id == "mat":
        from .units import _to_celsius
        src = "°F" if re.search(r"°?\s*F\b", unit_text) else "K" if re.fullmatch(r"K", unit_text) else None
        if not src:
            return entry
        conv, canon = _to_celsius(num, src), "°C"
    else:
        rule = next(((f, c) for rx, f, c in _UNIT_RULES.get(field_id, [])
                     if re.search(rx, unit_text, re.IGNORECASE)), None)
        if not rule:
            return entry
        conv, canon = num * rule[0], rule[1]
    return {**entry, "value": f"{round(conv, 4):g}", "unit": canon,
            "original_value": value, "original_unit": entry.get("unit")}


def normalize_units(metadata: dict) -> dict:
    """Return metadata with coordinates in signed decimal degrees and soil/climate
    values in GRAND's canonical units. Non-destructive."""
    return {k: _convert(k, v) if _filled(v) else v for k, v in (metadata or {}).items()}


def derive_metadata(merged: dict) -> dict:
    """Add values implied by other fields and normalise units, leaving explicit
    values untouched otherwise.

    A bare DOI yields its canonical resolver URL as ``pub_link``, so a paper whose
    PDF never prints its own URL still gets a usable link. Coordinates and numeric
    soil/climate values are converted to canonical units (``normalize_units``) so the
    pre-fill lands in the form's units. Non-destructive.
    """
    from .promote import doi_link

    out: dict = normalize_units(
        {k: dict(v) if isinstance(v, dict) else v for k, v in (merged or {}).items()})
    if _filled(out.get("pub_link")):
        return out
    doi_entry = out.get("pub_doi")
    if _filled(doi_entry):
        link = doi_link(doi_entry.get("value"))
        if link:
            out["pub_link"] = {
                "value": link,
                "source": "derived",
                "confidence": 1.0,
            }
    return out
