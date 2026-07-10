"""Plane B field registry — study & site metadata.

These fields span two wizard stages, split by how many values a study has:

* **Global** (``GLOBAL_GROUPS``: Publication, Site & climate) — exactly one value
  per study. Collected on the *Site Information* stage.
* **Treatment-level** (``TREATMENT_GROUPS``: Soil, Crop & management) — often
  *differ between treatments* (contrasting rotations, tillage, amended vs control
  soil…). Collected on the *Soil & Management* stage, where the contributor marks
  each group global or per-treatment and, if per-treatment, fills a value for each.

Field ids match the manuscript metadata contract (manuscript plan §6) on purpose,
so both fill sources target the same field:
  * the language-model module's ``manuscript_facts.metadata`` (Phase B), and
  * leftover CSV columns matched by the same suggestion engine.

Reuses ``CanonicalField`` so the suggestion engine works unchanged; ``options``
turns a field into a dropdown, and ``aliases`` seed CSV-column matching.
"""
from __future__ import annotations

from .canonical import CanonicalField

_USDA_TEXTURE = (
    "Sand", "Loamy sand", "Sandy loam", "Loam", "Silt loam", "Silt",
    "Sandy clay loam", "Clay loam", "Silty clay loam", "Sandy clay",
    "Silty clay", "Clay",
)

SITE_FIELDS: list[CanonicalField] = [
    # --- Publication ---
    CanonicalField("pub_title", "Title", "Publication", dtype="string",
                   aliases=("title", "paper title", "study title")),
    CanonicalField("pub_lead_author", "Lead author", "Publication", dtype="string",
                   aliases=("author", "lead author", "first author")),
    CanonicalField("pub_year", "Year", "Publication", dtype="int",
                   aliases=("year", "publication year", "pub year")),
    CanonicalField("pub_doi", "DOI", "Publication", dtype="string",
                   aliases=("doi", "digital object identifier")),
    CanonicalField("pub_citation", "Citation", "Publication", dtype="string",
                   aliases=("citation", "reference")),
    CanonicalField("pub_link", "Link", "Publication", dtype="string",
                   aliases=("link", "url", "paper url", "paper link", "web link")),
    CanonicalField("pub_abstract", "Abstract", "Publication", dtype="text",
                   aliases=("abstract", "summary")),

    # --- Site & climate ---
    CanonicalField("site_name", "Site name", "Site & climate", dtype="string",
                   aliases=("site", "site name", "location", "field site", "station")),
    CanonicalField("site_latitude", "Latitude", "Site & climate", unit="°", required=True,
                   aliases=("latitude", "lat")),
    CanonicalField("site_longitude", "Longitude", "Site & climate", unit="°", required=True,
                   aliases=("longitude", "lon", "long", "lng")),
    CanonicalField("country", "Country", "Site & climate", dtype="string",
                   aliases=("country", "nation")),
    CanonicalField("map", "Mean annual precipitation (MAP)", "Site & climate", unit="mm",
                   aliases=("map", "mean annual precipitation", "annual precipitation",
                            "annual rainfall", "mean annual rainfall")),
    CanonicalField("mat", "Mean annual temperature (MAT)", "Site & climate", unit="°C",
                   aliases=("mat", "mean annual temperature", "annual temperature",
                            "mean annual air temperature")),

    # --- Soil ---
    CanonicalField("sand", "Sand", "Soil", unit="%",
                   aliases=("sand", "sand content", "sand percent", "sand pct")),
    CanonicalField("silt", "Silt", "Soil", unit="%",
                   aliases=("silt", "silt content", "silt percent")),
    CanonicalField("clay", "Clay", "Soil", unit="%",
                   aliases=("clay", "clay content", "clay percent")),
    CanonicalField("texture_class", "Texture class", "Soil", dtype="string",
                   options=_USDA_TEXTURE,
                   aliases=("texture", "texture class", "soil texture", "usda texture")),
    CanonicalField("ph", "Soil pH", "Soil",
                   aliases=("ph", "soil ph", "ph water", "ph h2o")),
    CanonicalField("bulk_density", "Bulk density", "Soil", unit="g cm⁻³",
                   aliases=("bulk density", "bd", "soil bulk density")),
    CanonicalField("som", "Soil organic matter (SOM)", "Soil", unit="%",
                   aliases=("som", "soil organic matter", "organic matter", "soc",
                            "soil organic carbon", "organic carbon")),

    # --- Crop & management ---
    CanonicalField("primary_crop", "Primary crop", "Crop & management", dtype="string",
                   required=True,
                   aliases=("crop", "primary crop", "main crop", "planted crop", "species")),
    CanonicalField("secondary_crop", "Secondary crop", "Crop & management", dtype="string",
                   aliases=("secondary crop", "rotation crop", "second crop")),
    CanonicalField("cover_crop", "Cover crop", "Crop & management", dtype="string",
                   aliases=("cover crop", "cover")),
    CanonicalField("tillage", "Tillage practice", "Crop & management", dtype="string",
                   aliases=("tillage", "tillage practice", "till", "tillage system")),
    CanonicalField("management", "Management", "Crop & management", dtype="string",
                   aliases=("management", "management practice", "treatment", "system")),
    CanonicalField("flux_instrument", "Flux measurement method", "Crop & management",
                   dtype="string",
                   # No bare "chamber" alias — it collides with "Chamber_ID" columns.
                   aliases=("flux instrument", "flux method", "flux measurement method",
                            "measurement method")),
]

_BY_ID = {f.id: f for f in SITE_FIELDS}

# Which groups belong to which wizard stage. A study has exactly one value for a
# GLOBAL group; a TREATMENT group may be global *or* vary by treatment.
GLOBAL_GROUPS = ("Publication", "Site & climate")
TREATMENT_GROUPS = ("Soil", "Crop & management")


def get(field_id: str) -> CanonicalField | None:
    return _BY_ID.get(field_id)


def _groups_in_order(wanted: tuple[str, ...]) -> list[str]:
    seen: list[str] = []
    for f in SITE_FIELDS:
        if f.group in wanted and f.group not in seen:
            seen.append(f.group)
    return seen


def groups() -> list[str]:
    seen: list[str] = []
    for f in SITE_FIELDS:
        if f.group not in seen:
            seen.append(f.group)
    return seen


def global_groups() -> list[str]:
    return _groups_in_order(GLOBAL_GROUPS)


def treatment_groups() -> list[str]:
    return _groups_in_order(TREATMENT_GROUPS)


def global_fields() -> list[CanonicalField]:
    """One-per-study fields (Site Information stage)."""
    return [f for f in SITE_FIELDS if f.group in GLOBAL_GROUPS]


def treatment_fields() -> list[CanonicalField]:
    """Fields that may vary by treatment (Soil & Management stage)."""
    return [f for f in SITE_FIELDS if f.group in TREATMENT_GROUPS]
