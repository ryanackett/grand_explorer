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
    # Whether this field is a column-mapping *target* in the UI. Some fields stay in the
    # registry (so the pipeline can emit them) but are never mapped by hand because we
    # derive them ourselves — e.g. DOY from the date, nitrogen type from the fertiliser
    # event. Hiding them prevents a contradictory hand-mapped value.
    mappable: bool = True


# Ordered so the mapping UI lists the important fields first.
CANONICAL_FIELDS: list[CanonicalField] = [
    # --- Core (Plane A, the point of the dataset) ---
    CanonicalField(
        # Identifier column, listed first: which treatment each row belongs to. It isn't
        # a measurement, but it lives in the time-series table and its distinct values
        # seed the per-treatment soil/management step (Plane B). Mandatory: every dataset
        # must say which treatment a row belongs to.
        "treatment", "Treatment identifier", "Core", dtype="string", required=True,
        aliases=("treatment", "treatment id", "treatment name", "treatment code",
                 "trt", "trt id", "trt code", "treatmentname"),
    ),
    CanonicalField(
        # Replication / plot within a treatment (a spatial replicate or plot id). Optional.
        # When mapped alongside `treatment`, `_normalized_treatments` averages replicates per
        # treatment×date (and computes N2O flux SE) — Treatment/RawMeasurementTreatment hold
        # treatment-averaged data. It isn't a measurement column itself (the values are the
        # grouping key, not stored per-row).
        "replication", "Replication / plot identifier", "Core", dtype="string",
        aliases=("replication", "replicate", "rep", "rep id", "block", "block id",
                 "plot", "plot id", "plotid", "subplot", "field replicate"),
    ),
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
        "airt", "Air temperature (average)", "Soil & climate", unit="°C",
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
        "irrigationapplied", "Irrigation applied", "Soil & climate", unit="mm",
        aliases=("irrigation applied", "irrigation", "irrig", "water applied",
                 "irrigation amount"),
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
        # Derived from the measurement date, never hand-mapped.
        "doy", "Day of year", "Soil & climate", dtype="int", mappable=False,
        aliases=("doy", "day of year", "julian day", "jday", "julian date"),
    ),

    # --- Events (Plane C, sparse per-date interventions) ---
    CanonicalField(
        "nitrogenapplied", "Nitrogen application event (quantity)", "Events", unit="kg N ha⁻¹",
        aliases=("nitrogen applied", "n applied", "n rate", "n_rate", "napplied",
                 "fertilizer n", "fertiliser n", "n fertilizer", "n fertiliser",
                 "n input", "nitrogen rate"),
    ),
    CanonicalField(
        "nitrogenform", "Nitrogen application event (N form)", "Events", dtype="string",
        aliases=("nitrogen form", "n form", "fertilizer type", "fertiliser type",
                 "n source", "fertilizer form"),
    ),
    CanonicalField(
        # Derived from the fertilisation event (Timeline stage), never hand-mapped.
        "nitrogentype", "Nitrogen type", "Events", dtype="string", mappable=False,
        aliases=("nitrogen type", "n type"),
    ),
    CanonicalField(
        "plantedcrop", "Planting event (crop)", "Events", dtype="string",
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
        # Treatment-level attribute captured in the Soil & Management stage, not a
        # per-date measurement column — never hand-mapped here.
        "management", "Management", "Events", dtype="string", mappable=False,
        aliases=("management", "mgmt", "management practice"),
    ),
]

_BY_ID = {f.id: f for f in CANONICAL_FIELDS}


def get(field_id: str) -> CanonicalField | None:
    return _BY_ID.get(field_id)


def mappable_fields() -> list[CanonicalField]:
    """Fields offered as column-mapping targets (excludes derived ones like DOY)."""
    return [f for f in CANONICAL_FIELDS if f.mappable]


def groups() -> list[str]:
    """Distinct groups, in first-seen order."""
    seen: list[str] = []
    for f in CANONICAL_FIELDS:
        if f.group not in seen:
            seen.append(f.group)
    return seen
