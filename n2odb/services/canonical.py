"""Canonical field registry for the upload workbench (column mapping target).

These are the fields a contributor's data columns get mapped onto. They mirror the
ingestion target ``RawMeasurementTreatment`` (Plane A time-series + Plane C per-date
events); QC/anomaly columns the pipeline computes itself are intentionally omitted.

Each entry carries a friendly ``label`` and ``unit`` (for the dual-label UI), the
expected ``dtype`` (used to sanity-check a suggestion), a ``group`` (for visual
grouping), and ``aliases`` — known header spellings that seed the deterministic
suggestion engine (``services/suggest.py``). Keep ``id`` equal to the model field
name so downstream mapping/promote code can use it directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CanonicalField:
    id: str
    label: str
    group: str
    dtype: str = "float"          # float | int | date | string | bool
    unit: str | None = None
    required: bool = False
    aliases: tuple[str, ...] = ()
    # For form rendering of Plane B fields: a fixed choice list makes it a <select>.
    options: tuple[str, ...] = ()


# Ordered so the mapping UI lists the important fields first.
CANONICAL_FIELDS: list[CanonicalField] = [
    # --- Core (Plane A, the point of the dataset) ---
    CanonicalField(
        "date", "Measurement date", "Core", dtype="date", required=True,
        aliases=("date", "sampling date", "measurement date", "sample date",
                 "collection date", "datetime", "day", "sampling day", "obs date"),
    ),
    CanonicalField(
        "n2oflux", "Nitrous oxide flux (N₂O)", "Core", unit="g N ha⁻¹ d⁻¹", required=True,
        # NB: no bare "flux" alias — it collides with CO2/CH4 flux columns, which
        # are common and out of scope.
        aliases=("n2o", "n2o flux", "nitrous oxide", "nitrous oxide flux",
                 "f n2o", "n2o emission", "n2o n", "n2o flux rate", "n2o efflux"),
    ),
    CanonicalField(
        "fluxstandarderror", "Flux standard error", "Core", unit="g N ha⁻¹ d⁻¹",
        aliases=("flux se", "n2o se", "flux standard error", "se flux",
                 "flux std error"),
    ),
    CanonicalField(
        # Identifier column: which treatment/plot each row belongs to. It isn't a
        # measurement, but it lives in the time-series table and its distinct values
        # seed the per-treatment soil/management step (Plane B). No "block" alias —
        # a block is a spatial replicate, not a treatment.
        "treatment", "Treatment / plot", "Core", dtype="string",
        aliases=("treatment", "treatment id", "treatment name", "treatment code",
                 "trt", "trt id", "trt code", "plot", "plot id", "plotid"),
    ),

    # --- Soil & climate covariates (Plane A) ---
    CanonicalField(
        "vwc", "Volumetric water content (VWC)", "Soil & climate", unit="%",
        aliases=("vwc", "volumetric water content", "soil moisture", "moisture",
                 "water content", "swc", "theta", "soil water content"),
    ),
    CanonicalField(
        "wfps", "Water-filled pore space (WFPS)", "Soil & climate", unit="%",
        aliases=("wfps", "water filled pore space", "water-filled pore space",
                 "pore space", "percent wfps"),
    ),
    CanonicalField(
        "soilt", "Soil temperature", "Soil & climate", unit="°C",
        aliases=("soilt", "soil temp", "soil temperature", "tsoil", "t soil",
                 "soil t"),
    ),
    CanonicalField(
        "airt", "Air temperature", "Soil & climate", unit="°C",
        aliases=("airt", "air temp", "air temperature", "tair", "t air", "air t"),
    ),
    CanonicalField(
        "airtmax", "Air temperature (max)", "Soil & climate", unit="°C",
        aliases=("airtmax", "tmax", "max air temp", "air temp max", "maximum air temperature"),
    ),
    CanonicalField(
        "airtmin", "Air temperature (min)", "Soil & climate", unit="°C",
        aliases=("airtmin", "tmin", "min air temp", "air temp min", "minimum air temperature"),
    ),
    CanonicalField(
        "precip", "Precipitation", "Soil & climate", unit="mm",
        aliases=("precip", "precipitation", "rain", "rainfall", "ppt", "rain mm"),
    ),
    CanonicalField(
        "soilnh4", "Soil ammonium (NH₄⁺)", "Soil & climate", unit="mg N kg⁻¹",
        aliases=("soilnh4", "nh4", "ammonium", "nh4 n", "soil nh4", "nh4+",
                 "soil ammonium"),
    ),
    CanonicalField(
        "soilno3", "Soil nitrate (NO₃⁻)", "Soil & climate", unit="mg N kg⁻¹",
        aliases=("soilno3", "no3", "nitrate", "no3 n", "soil no3", "no3-",
                 "soil nitrate"),
    ),
    CanonicalField(
        "doy", "Day of year", "Soil & climate", dtype="int",
        aliases=("doy", "day of year", "julian day", "jday", "julian date"),
    ),

    # --- Events (Plane C, sparse per-date interventions) ---
    CanonicalField(
        "nitrogenapplied", "Nitrogen applied", "Events", unit="kg N ha⁻¹",
        aliases=("nitrogen applied", "n applied", "n rate", "n_rate", "napplied",
                 "fertilizer n", "fertiliser n", "n fertilizer", "n fertiliser",
                 "n input", "nitrogen rate"),
    ),
    CanonicalField(
        "nitrogenform", "Nitrogen form", "Events", dtype="string",
        aliases=("nitrogen form", "n form", "fertilizer type", "fertiliser type",
                 "n source", "fertilizer form"),
    ),
    CanonicalField(
        "nitrogentype", "Nitrogen type", "Events", dtype="string",
        aliases=("nitrogen type", "n type"),
    ),
    CanonicalField(
        "plantedcrop", "Crop", "Events", dtype="string",
        aliases=("crop", "planted crop", "cropping", "crop species", "plant"),
    ),
    CanonicalField(
        "tillage", "Tillage event", "Events", dtype="int",
        aliases=("tillage", "tilled", "till", "tillage event"),
    ),
    CanonicalField(
        "harvest", "Harvest event", "Events", dtype="int",
        aliases=("harvest", "harvested", "harvest event"),
    ),
    CanonicalField(
        "irrigationapplied", "Irrigation applied", "Events", unit="mm",
        aliases=("irrigation applied", "irrigation", "irrig", "water applied",
                 "irrigation amount"),
    ),
    CanonicalField(
        "management", "Management", "Events", dtype="string",
        aliases=("management", "mgmt", "management practice"),
    ),
]

_BY_ID = {f.id: f for f in CANONICAL_FIELDS}


def get(field_id: str) -> CanonicalField | None:
    return _BY_ID.get(field_id)


def groups() -> list[str]:
    """Distinct groups, in first-seen order."""
    seen: list[str] = []
    for f in CANONICAL_FIELDS:
        if f.group not in seen:
            seen.append(f.group)
    return seen
