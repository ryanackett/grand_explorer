"""Plane C event registry — dated field operations.

The Timeline stage collects the study's *events*: the dated field operations a paper
reports (fertiliser applications, plantings, harvests, tillage passes). Like the
soil/management stage, an event type may be **global** (the same schedule across the
whole study) or **per-treatment** (contrasting rotations, split N applications…), and
a study usually has *several* events of each type — so each type holds a list.

Each ``EventType`` lists the fields to collect for one operation. ``contract_type``
links a type to the extraction module's event ``type`` (``manuscript_extract``
remaps "fertilisation" → "input_application" in its contract), so pre-fill can group
the model's events under the right section.

``options_key`` on a field marks it as a strict dropdown whose values the *view*
resolves from live DB data (e.g. the existing ``NitrogenType`` vocabulary) — the
registry stays free of DB imports, exactly as ``site_fields`` does.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EventFieldDef:
    key: str
    label: str
    dtype: str = "string"          # string | float | date
    unit: str | None = None
    options_key: str | None = None  # if set, the view fills a strict dropdown


@dataclass(frozen=True)
class EventType:
    id: str
    label: str
    fields: tuple[EventFieldDef, ...]
    # The extraction contract's event ``type`` this section corresponds to. Defaults
    # to ``id``; fertilisation differs because ``_to_contract`` renames it.
    contract_type: str = ""

    @property
    def contract(self) -> str:
        return self.contract_type or self.id


EVENT_TYPES: list[EventType] = [
    EventType("fertilisation", "Fertilization", (
        EventFieldDef("date", "Date", "date"),
        EventFieldDef("n_amount", "N applied", "float", unit="kg N ha⁻¹"),
        EventFieldDef("form", "Fertilizer form", "string"),
        EventFieldDef("type", "Fertilizer type", "string", options_key="nitrogen_type"),
    ), contract_type="input_application"),
    EventType("planting", "Planting", (
        EventFieldDef("date", "Date", "date"),
        EventFieldDef("crop", "Planted crop", "string"),
    )),
    EventType("harvest", "Harvest", (
        EventFieldDef("date", "Date", "date"),
    )),
    EventType("tillage", "Tillage", (
        EventFieldDef("date", "Date", "date"),
        EventFieldDef("instrument", "Tillage instrument", "string"),
    )),
]

_BY_ID = {t.id: t for t in EVENT_TYPES}
# Extra non-schema keys we still persist alongside a field (the unit paired with a
# numeric amount). Kept out of ``fields`` so it isn't rendered as its own control.
_EXTRA_KEYS = {"fertilisation": {"n_unit"}}


def types() -> list[EventType]:
    return EVENT_TYPES


def get(type_id: str) -> EventType | None:
    return _BY_ID.get(type_id)


def allowed_keys(type_id: str) -> set[str]:
    """Field keys a section may persist (its controls + any paired extras)."""
    et = _BY_ID.get(type_id)
    if et is None:
        return set()
    return {f.key for f in et.fields} | _EXTRA_KEYS.get(type_id, set())


def for_template(options: dict | None = None) -> list[dict]:
    """Serialize the registry for the page, resolving each dropdown's option list.

    ``options`` maps an ``options_key`` to its allowed values (from the view).
    """
    options = options or {}
    out: list[dict] = []
    for et in EVENT_TYPES:
        out.append({
            "id": et.id,
            "label": et.label,
            "contract": et.contract,
            "fields": [
                {
                    "key": f.key,
                    "label": f.label,
                    "dtype": f.dtype,
                    "unit": f.unit,
                    "options": list(options.get(f.options_key, [])) if f.options_key else [],
                }
                for f in et.fields
            ],
        })
    return out
