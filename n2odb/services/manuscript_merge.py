"""Merge deterministic-parser output with the language-model module's output.

Policy (manuscript plan §4): when a deterministic parser and the module both
propose a value for the same field, prefer the deterministic number, keep the
module's ``source_quote``, and flag a mismatch if they disagree beyond a
tolerance. Fields only one side proposes pass through unchanged.
"""
from __future__ import annotations

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


def derive_metadata(merged: dict) -> dict:
    """Add values implied by other fields, leaving explicit values untouched.

    A bare DOI yields its canonical resolver URL as ``pub_link``, so a paper whose
    PDF never prints its own URL still gets a usable link. Non-destructive.
    """
    from .promote import doi_link

    out: dict = {k: dict(v) if isinstance(v, dict) else v for k, v in (merged or {}).items()}
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
