"""A canned §5.2 contract object for tests and GUI verification.

Wire ``fake_extract_facts`` in place of ``manuscript_extract.extract_facts`` to
exercise the full review-form / event-timeline pre-fill without any model call
(manuscript plan §5.3, §10). It returns a couple of metadata keys and one event,
in the exact contract shape.
"""
from __future__ import annotations

SAMPLE_FACTS: dict = {
    "status": "ok",
    "metadata": {
        "pub_title": {
            "value": "Nitrous oxide emissions from a maize–wheat rotation",
            "unit": None,
            "source_quote": "Nitrous oxide emissions from a maize–wheat rotation under contrasting tillage",
            "confidence": 0.95,
        },
        "primary_crop": {
            "value": "maize",
            "unit": None,
            "source_quote": "The site was cropped to maize (Zea mays L.) during the 2018 season.",
            "confidence": 0.88,
        },
        "clay": {
            "value": 24.0,
            "unit": "%",
            "source_quote": "The soil is a clay loam (24% clay, 30% silt, 46% sand).",
            "confidence": 0.7,
        },
    },
    "events": [
        {
            "type": "input_application",
            "date_iso": "2018-05-03",
            "date_text": "early May 2018",
            "fields": [
                {"key": "amount", "label": "N applied", "value": 120, "unit": "kg N/ha"},
                {"key": "form", "label": "Fertiliser form", "value": "urea", "unit": None},
            ],
            "source_quote": "Urea was applied at 120 kg N ha-1 in early May 2018, before sowing.",
            "confidence": 0.82,
        }
    ],
    "model": "fixture",
    "generated_at": "2026-01-01T00:00:00Z",
    "usage": {},
}


def fake_extract_facts(
    text: str, already_have: list[str], option_choices: dict | None = None
) -> dict:
    """Return a copy of the canned contract, minus any ``already_have`` fields."""
    import copy

    facts = copy.deepcopy(SAMPLE_FACTS)
    for field_id in already_have or []:
        facts["metadata"].pop(field_id, None)
    return facts
