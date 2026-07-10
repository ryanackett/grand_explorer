"""Deterministic parsers over extracted document text.

Cheap, reliable, and a useful cross-check on the language-model module's output.
These run regardless of whether the module is enabled (manuscript plan §4).

Each parser returns entries shaped like a §5 ``metadata`` value:

    {"value": <str|number>, "source_quote": <str>, "source": "regex", "confidence": 1.0}

``parse_all`` returns a ``{field_id: entry}`` dict keyed by the review-form field
ids used by the wizard (``pub_doi``, ``site_latitude``, ``site_longitude``), ready
to merge into ``manuscript_facts["metadata"]``.
"""
from __future__ import annotations

import re

# --- DOI ---------------------------------------------------------------------

# Core DOI syntax. The trailing set is greedy but we trim common trailing
# punctuation afterwards (DOIs frequently sit at the end of a sentence).
_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_DOI_TRAILING = ").,;:'\"]}>"


def find_doi(text: str) -> dict | None:
    if not text:
        return None
    m = _DOI_RE.search(text)
    if not m:
        return None
    doi = m.group(0).rstrip(_DOI_TRAILING)
    return {
        "value": doi,
        "source_quote": _snippet(text, m.start(), m.end()),
        "source": "regex",
        "confidence": 1.0,
    }


# --- Coordinates -------------------------------------------------------------

# Casting a wide net: journals differ wildly in how they typeset coordinates, so
# the glyph classes below stay deliberately permissive.
#
# Degree mark: ° (U+00B0), º ordinal (U+00BA), ˚ ring-above (U+02DA), combining
#   ring (U+030A), ∘ ring-operator (U+2218). Real agronomy PDFs use the ring
#   forms, not just °.
# Minutes: prime (U+2032), apostrophe, right single quote, acute accent.
# Seconds: double prime (U+2033), quote, right double quote, or two apostrophes.
_DEG = "[°º˚̊∘]"
_PRIME = "['′’´]"
_DPRIME = "(?:[\"″”]|'')"
_HEMI = "[NSEW]"
# Decimal separator: this is a global database, so accept a European decimal comma
# ("42,4°N") as well as a period — but only inside the marked patterns below,
# where a degree glyph / hemisphere / axis label disambiguates it. The bare
# decimal-pair pattern keeps period-only, so a US "42, 4" separator is never
# mistaken for one number.
_NUM3 = r"\d{1,3}(?:[.,]\d+)?"     # degrees: 0–3 int digits, optional fraction
_NUM3F = r"\d{1,3}[.,]\d+"          # degrees: fraction required
_NUM2 = r"\d{1,2}(?:[.,]\d+)?"     # minutes/seconds

# DMS with an optional leading OR trailing hemisphere; minutes and seconds
# optional, and minutes may themselves be decimal (42°24.5′N). Examples caught:
# 33°52'11"S · 42˚24′ N · N 42°24' · 151°12′30″ E. The two look-behinds keep the
# degree part from matching the tail of a decimal ("40.0°N" → not "0°N") and keep
# the leading hemisphere from gluing onto a preceding word ("rain42°N").
_DMS_RE = re.compile(
    r"(?<![A-Za-z])(?:(?P<pre_hemi>" + _HEMI + r")\s*)?"
    r"(?<![\d.,])(?P<deg>\d{1,3})\s*" + _DEG + r"\s*"
    r"(?:(?P<min>" + _NUM2 + r")\s*" + _PRIME + r"\s*)?"
    r"(?:(?P<sec>" + _NUM2 + r")\s*" + _DPRIME + r"\s*)?"
    r"(?P<post_hemi>" + _HEMI + r")?",
    re.IGNORECASE,
)

# Decimal degrees carrying an explicit degree symbol, with an optional hemisphere
# on either side and an optional sign, e.g. "40.0°N", "-85.38° W", "N 42.4°".
# The degree symbol is required: a bare "N"/"E" after a number is almost always
# nitrogen ("5.2 N ha-1") or an exponent ("1.5E3"), not a coordinate.
_DEG_DEC_RE = re.compile(
    r"(?<![A-Za-z])(?:(?P<pre_hemi>" + _HEMI + r")\s*)?"
    r"(?P<sign>[-+])?(?P<num>" + _NUM3F + r")\s*" + _DEG + r"\s*(?P<post_hemi>" + _HEMI + r")?",
    re.IGNORECASE,
)

# Axis-labelled value: "latitude 42.4", "lat. 42°24'N", "long -85.4". The label
# names the axis, so no hemisphere is needed — but we still require the value to
# look coordinate-ish (a decimal, a degree/prime glyph, minutes, or a hemisphere)
# so a stray "long 85 days" is not misread.
_LABELED_RE = re.compile(
    r"\b(?P<label>latitude|longitude|lat|lon|long)\b\.?\s*[:=]?\s*"
    r"(?P<sign>[-+])?(?P<val>" + _NUM3 + r")\s*(?P<deg_sym>" + _DEG + r")?\s*"
    r"(?:(?P<min>" + _NUM2 + r")\s*" + _PRIME + r"\s*)?"
    r"(?:(?P<sec>" + _NUM2 + r")\s*" + _DPRIME + r"\s*)?"
    r"(?P<hemi>" + _HEMI + r")?",
    re.IGNORECASE,
)

# A decimal pair, each number optionally carrying a degree symbol
# ("-33.87, 151.21" or "42.4°, -85.4°"). Accepted only near a coordinate cue,
# since two adjacent decimals are otherwise meaningless.
_DECIMAL_PAIR_RE = re.compile(
    r"(?P<lat>[-+]?\d{1,3}\.\d+)\s*" + _DEG + r"?\s*[,;]?\s+"
    r"(?P<lon>[-+]?\d{1,3}\.\d+)\s*" + _DEG + r"?"
)
_COORD_ANCHOR = re.compile(
    r"coordinat|latitude|longitude|\blat\b|\blon\b|located\s+at|situated\s+at|\bgps\b",
    re.IGNORECASE,
)
# How far back to look for a coordinate cue before an otherwise-unmarked pair.
_ANCHOR_WINDOW = 45


def _to_float(s: str) -> float:
    """Parse a number that may use a European decimal comma."""
    return float(s.replace(",", "."))


def _dms_to_decimal(deg: str, minute: str | None, sec: str | None, hemi: str | None) -> float:
    value = _to_float(deg)
    if minute:
        value += _to_float(minute) / 60.0
    if sec:
        value += _to_float(sec) / 3600.0
    if hemi and hemi.upper() in ("S", "W"):
        value = -value
    return round(value, 6)


def _axis_of(hemi: str | None) -> str | None:
    if not hemi:
        return None
    h = hemi.upper()
    if h in ("N", "S"):
        return "lat"
    if h in ("E", "W"):
        return "lon"
    return None


def _valid(value: float, axis: str) -> bool:
    if axis == "lat":
        return -90.0 <= value <= 90.0
    return -180.0 <= value <= 180.0


def _entry(value: float, start: int, end: int, text: str, confidence: float) -> dict:
    return {
        "value": round(value, 6),
        "source_quote": _snippet(text, start, end),
        "source": "regex",
        "confidence": confidence,
    }


def find_coordinates(text: str) -> dict:
    """Return ``{"site_latitude": entry, "site_longitude": entry}`` for coordinates.

    Precision-first for a scientific-paper corpus — every candidate must carry an
    explicit geographic marker (a hemisphere, a degree glyph, or an axis label),
    so the dense numeric prose of a methods section does not yield garbage. Within
    that guard the net is wide: it covers DMS and decimal degrees, hemispheres on
    either side, the many degree/prime glyphs journals use, axis-labelled values,
    and cue-anchored decimal pairs. Tried highest-confidence first:

    1. DMS with a hemisphere (``33°52'11"S``, ``42˚24′ N``).
    2. Decimal degrees with a degree symbol + hemisphere (``40.0°N``).
    3. Axis-labelled values (``latitude 42.4``, ``long -85.4``).
    4. A decimal pair (``-33.87, 151.21``, ``42.4°, -85.4°``) only when a
       coordinate cue precedes it.

    There is deliberately no bare "first two decimals" fallback — on real papers
    it reliably produced meaningless coordinates.
    """
    if not text:
        return {}

    found: dict[str, dict] = {}

    def take(field: str, value: float, start: int, end: int, conf: float):
        found.setdefault(field, _entry(value, start, end, text, conf))

    def both() -> bool:
        return "site_latitude" in found and "site_longitude" in found

    def field_for(axis: str) -> str:
        return "site_latitude" if axis == "lat" else "site_longitude"

    # 1) DMS with a hemisphere (either side).
    for m in _DMS_RE.finditer(text):
        hemi = m.group("post_hemi") or m.group("pre_hemi")
        axis = _axis_of(hemi)
        if axis is None:
            continue
        value = _dms_to_decimal(m.group("deg"), m.group("min"), m.group("sec"), hemi)
        if _valid(value, axis):
            take(field_for(axis), value, m.start(), m.end(), 1.0)
    if both():
        return found

    # 2) Decimal degrees with a degree symbol + hemisphere.
    for m in _DEG_DEC_RE.finditer(text):
        hemi = m.group("post_hemi") or m.group("pre_hemi")
        axis = _axis_of(hemi)
        if axis is None:
            continue
        value = _to_float(m.group("num"))
        if m.group("sign") == "-" or hemi.upper() in ("S", "W"):
            value = -abs(value)
        if _valid(value, axis):
            take(field_for(axis), value, m.start(), m.end(), 0.95)
    if both():
        return found

    # 3) Axis-labelled values (the label fixes the axis).
    for m in _LABELED_RE.finditer(text):
        # Require the value to look coordinate-ish, not a bare integer.
        val = m.group("val")
        if not (m.group("deg_sym") or m.group("min") or m.group("hemi")
                or "." in val or "," in val):
            continue
        axis = "lat" if m.group("label").lower().startswith("lat") else "lon"
        value = _dms_to_decimal(m.group("val"), m.group("min"), m.group("sec"), None)
        hemi = m.group("hemi")
        if hemi and hemi.upper() in ("S", "W"):
            value = -abs(value)
        elif m.group("sign") == "-":
            value = -value
        if _valid(value, axis):
            take(field_for(axis), value, m.start(), m.end(), 0.9)
    if both():
        return found

    # 4) Decimal pair, only when a coordinate cue precedes it.
    for m in _DECIMAL_PAIR_RE.finditer(text):
        window = text[max(0, m.start() - _ANCHOR_WINDOW):m.start()]
        if not _COORD_ANCHOR.search(window):
            continue
        lat_v, lon_v = float(m.group("lat")), float(m.group("lon"))
        if _valid(lat_v, "lat") and _valid(lon_v, "lon"):
            take("site_latitude", lat_v, m.start("lat"), m.end("lat"), 0.55)
            take("site_longitude", lon_v, m.start("lon"), m.end("lon"), 0.55)
            break

    return found


# --- aggregation -------------------------------------------------------------


def parse_all(text: str) -> dict[str, dict]:
    """Run every deterministic parser and return a metadata-shaped dict."""
    metadata: dict[str, dict] = {}
    doi = find_doi(text)
    if doi:
        metadata["pub_doi"] = doi
    metadata.update(find_coordinates(text))
    return metadata


# --- helpers -----------------------------------------------------------------

_SNIPPET_PAD = 60


def _snippet(text: str, start: int, end: int, pad: int = _SNIPPET_PAD) -> str:
    """A short verbatim window around a match, for provenance display."""
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    quote = text[lo:hi].strip()
    if lo > 0:
        quote = "…" + quote
    if hi < len(text):
        quote = quote + "…"
    return quote
