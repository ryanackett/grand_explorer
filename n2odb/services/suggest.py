"""Deterministic column-mapping suggestion engine.

Given a source column header (and, optionally, its inferred dtype), guess which
canonical field it maps to, with a confidence score the UI colours by. Layered
cheapest/highest-precision first (roadmap §5):

1. Normalise the header (lowercase, strip parenthetical units, split camelCase,
   unaccent, drop punctuation).
2. Exact match against the curated alias dictionary (``canonical.py``).
3. Fuzzy match (``rapidfuzz.token_set_ratio``) against the aliases for the
   residual, with a light dtype-compatibility adjustment.

LLM assist for the still-ambiguous residual is a later, opt-in layer and is not
part of this deterministic module.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

from . import canonical

# Score (0..100) thresholds → surfaced as a suggestion, and coloured by confidence.
_MIN_SCORE = 74            # below this we offer no suggestion
_EXACT_CONFIDENCE = 1.0    # an exact alias hit always outranks any fuzzy match
_FUZZY_CAP = 0.97          # fuzzy can approach but never reach an exact hit


@dataclass
class Suggestion:
    field_id: str | None
    confidence: float
    source: str  # "alias" | "fuzzy" | "none"

    def as_dict(self) -> dict:
        return {"canonical_field": self.field_id, "confidence": round(self.confidence, 3),
                "source": self.source}


_PARENS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
# Split camelCase only after 2+ lowercase letters, so "soilTemp" → "soil Temp"
# but short chemistry tokens like "pH" stay intact (not "p H").
_CAMEL = re.compile(r"(?<=[a-z]{2})(?=[A-Z])")
_SEP = re.compile(r"[_\-/.,;:]+")
_WS = re.compile(r"\s+")


def normalize(header: str) -> str:
    """Fold a raw header to a comparable form: no units-in-parens, no camelCase,
    no accents, no punctuation, single-spaced, lowercase."""
    if not header:
        return ""
    s = _PARENS.sub(" ", str(header))        # drop "(%)", "(kg N/ha)", …
    s = _CAMEL.sub(" ", s)                     # soilTemp → soil Temp
    s = _SEP.sub(" ", s)                       # n2o_flux → n2o flux
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("%", " ").replace("+", " ").replace("°", " ")
    s = _WS.sub(" ", s).strip().lower()
    return s


# Precompute normalized match terms per field (id + label + aliases).
def _match_terms(f: canonical.CanonicalField) -> set[str]:
    terms = {normalize(f.id), normalize(f.label)}
    terms.update(normalize(a) for a in f.aliases)
    return {t for t in terms if t}


# Cache of {field_id: terms} and {id: field} keyed by the registry list's identity,
# so both Plane A (measurements) and Plane B (site fields) reuse the same engine.
_TERMS_CACHE: dict[int, dict[str, set[str]]] = {}
_BY_ID_CACHE: dict[int, dict[str, canonical.CanonicalField]] = {}


def _registry(fields):
    if fields is None:
        fields = canonical.CANONICAL_FIELDS
    key = id(fields)
    if key not in _TERMS_CACHE:
        _TERMS_CACHE[key] = {f.id: _match_terms(f) for f in fields}
        _BY_ID_CACHE[key] = {f.id: f for f in fields}
    return fields, _TERMS_CACHE[key], _BY_ID_CACHE[key]


# dtype families that are compatible with each other for the sanity adjustment.
_NUMERIC = {"float", "int"}


def _dtype_ok(field: canonical.CanonicalField, col_dtype: str | None) -> bool:
    if not col_dtype:
        return True
    if field.dtype in _NUMERIC:
        return col_dtype in _NUMERIC
    if field.dtype == "date":
        return col_dtype == "date"
    # string/bool targets accept anything (labels, codes, flags).
    return True


def suggest(header: str, col_dtype: str | None = None, fields=None) -> Suggestion:
    """Suggest a field for one header against ``fields`` (default: Plane A)."""
    _, terms_by_id, by_id = _registry(fields)
    norm = normalize(header)
    if not norm:
        return Suggestion(None, 0.0, "none")

    # 2) exact alias hit.
    for field_id, terms in terms_by_id.items():
        if norm in terms:
            return Suggestion(field_id, _EXACT_CONFIDENCE, "alias")

    # 3) fuzzy over aliases.
    best_field, best_score = None, 0
    for field_id, terms in terms_by_id.items():
        for term in terms:
            score = fuzz.token_set_ratio(norm, term)
            if score > best_score:
                best_field, best_score = field_id, score

    if best_field is None or best_score < _MIN_SCORE:
        return Suggestion(None, 0.0, "none")

    confidence = min(best_score / 100.0, _FUZZY_CAP)
    # Nudge confidence down when the column's data type contradicts the field
    # (e.g. a text column fuzzily matching a numeric field).
    if not _dtype_ok(by_id.get(best_field), col_dtype):
        confidence *= 0.6
    return Suggestion(best_field, confidence, "fuzzy")


def suggest_columns(headers: list[str], dtypes: list[str] | None = None, fields=None) -> list[dict]:
    """Suggest for every column, then resolve collisions: if two columns land on
    the same field, only the most-confident keeps it (1:1 mapping)."""
    dtypes = dtypes or [None] * len(headers)
    raw = [suggest(h, dtypes[i] if i < len(dtypes) else None, fields) for i, h in enumerate(headers)]

    # Keep the highest-confidence claimant per field; demote the rest to unmapped.
    best_for_field: dict[str, int] = {}
    for i, s in enumerate(raw):
        if s.field_id is None:
            continue
        cur = best_for_field.get(s.field_id)
        if cur is None or raw[i].confidence > raw[cur].confidence:
            best_for_field[s.field_id] = i
    winners = set(best_for_field.values())

    out = []
    for i, s in enumerate(raw):
        if s.field_id is not None and i not in winners:
            out.append(Suggestion(None, 0.0, "none").as_dict())
        else:
            out.append(s.as_dict())
    return out
