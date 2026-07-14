"""Promote an approved submission into the live GRAND tables.

Design (agreed with the maintainer):
  * **Create-fresh, no reconciliation.** Every promotion creates its own
    Publication → Site → Experiment → Treatment* → RawMeasurementTreatment* rows.
    A duplicate DOI is *flagged*, never merged (a second dataset for the same paper
    is legitimate).
  * **One atomic transaction, top-down** so each parent PK exists before its children
    reference it — all-or-nothing, so a failure leaves the live DB untouched.
  * **Idempotent by construction.** Promotion is refused unless the submission is in a
    promotable status, and the created keys are recorded on ``submission.promotion``;
    its presence blocks any second attempt — the structural guard against DB
    contamination from a double click or re-open.
  * **Never errors on over-length text.** Char values are trimmed to each column's
    ``max_length`` with a warning, so a promotion can't fail on a stray long string.
"""
from __future__ import annotations

import datetime as _dt
import re

from django.db import transaction
from django.db.models import Q

from ..models import Experiment, Publication, RawMeasurementTreatment, Site, Treatment

# Statuses a promote may run from — never a draft (not submitted), an already-promoted
# one, or a rejected one.
_PROMOTABLE = {"submitted", "in_review", "approved"}

# RawMeasurementTreatment columns whose ids match our canonical field ids 1:1.
_RAW_FLOAT = ("n2oflux", "vwc", "wfps", "soilt", "airt", "airtmax", "airtmin", "precip",
              "soilnh4", "soilno3", "nitrogenapplied", "irrigationapplied", "fluxstandarderror")
_RAW_INT = ("harvest", "tillage", "doy")
_RAW_TEXT = ("nitrogenform", "nitrogentype", "plantedcrop")

_DOI_PREFIX = re.compile(r"^\s*(?:https?://(?:dx\.)?doi\.org/|doi:)\s*", re.I)


class PromotionError(RuntimeError):
    """Raised when a submission cannot be promoted (bad status, missing required data)."""


# --- DOI handling ------------------------------------------------------------

def normalize_doi(raw) -> str:
    """Bare, lower-cased DOI (``10.x/...``) from any of the usual spellings, or ""."""
    if not raw:
        return ""
    return _DOI_PREFIX.sub("", str(raw).strip()).strip().rstrip("/").lower()


def doi_link(raw) -> str:
    bare = normalize_doi(raw)
    return f"https://doi.org/{bare}" if bare else ""


def find_duplicate_publications(raw_doi) -> list[dict]:
    """Existing publications whose Link/Citation already contains this DOI (soft check).

    Never blocks a submission — a second dataset for the same paper is valid — but both
    the submitter and the curator are alerted.
    """
    bare = normalize_doi(raw_doi)
    if not bare:
        return []
    qs = Publication.objects.filter(Q(Link__icontains=bare) | Q(Citation__icontains=bare))
    return [{"pubid": p.PubID, "title": p.PubTitle or "(untitled)", "link": p.Link} for p in qs[:5]]


# --- value coercion ----------------------------------------------------------

def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return None


def _int(v):
    f = _num(v)
    return int(f) if f is not None else None


def _fit(model, attr: str, value, warnings: list) -> str | None:
    """A string trimmed to the column's ``max_length`` (warning if trimmed), or None."""
    if value in (None, ""):
        return None
    s = str(value)
    ml = model._meta.get_field(attr).max_length
    if ml and len(s) > ml:
        warnings.append(f"{model.__name__}.{attr} trimmed to {ml} chars (was {len(s)}).")
        return s[:ml]
    return s


def _raw_records(records: list, management) -> list:
    """Turn normalized per-date records into RawMeasurementTreatment-ready kwargs."""
    out = []
    for rec in records:
        raw: dict = {}
        for fid in _RAW_FLOAT:
            v = _num(rec.get(fid))
            if v is not None:
                raw[fid] = v
        for fid in _RAW_INT:
            v = _int(rec.get(fid))
            if v is not None:
                raw[fid] = v
        for fid in _RAW_TEXT:
            if rec.get(fid) not in (None, ""):
                raw[fid] = _fit(RawMeasurementTreatment, fid, rec[fid], [])
        out.append({"date": _dt.date.fromisoformat(rec["date"]), "management": management, "raw": raw})
    return out


# --- plan --------------------------------------------------------------------

def build_plan(submission) -> dict:
    """Assemble the exact rows a promote would write, plus any blocking/warning issues.

    Pure and side-effect-free: the shared foundation for both ``dry_run`` (preview) and
    ``promote`` (write).
    """
    from ..views_workbench import (
        _normalized_treatments, _resolved_global_value, _resolved_treatment_value,
    )

    warnings: list[str] = []
    blocking: list[str] = []

    def gv(fid):
        return _resolved_global_value(submission, fid)

    doi = gv("pub_doi")
    pub_kwargs = {
        "PubTitle": _fit(Publication, "PubTitle", gv("pub_title"), warnings),
        "LeadAuthor": _fit(Publication, "LeadAuthor", gv("pub_lead_author"), warnings),
        "Citation": _fit(Publication, "Citation", gv("pub_citation"), warnings),
        "PubYear": _int(gv("pub_year")),
        "Link": _fit(Publication, "Link", gv("pub_link") or doi_link(doi), warnings),
        "Abstract": gv("pub_abstract") or None,
        # Stamp the submitter's persistent id so promoted data stays traceable to them
        # (null if the submission has no owner — e.g. staff-created or a deleted account).
        "Contributor": submission.owner.public_id if submission.owner_id else None,
    }

    lat, lon = _num(gv("site_latitude")), _num(gv("site_longitude"))
    if lat is None or lon is None:
        blocking.append("Site latitude and longitude are required.")
    elif not (-90 <= lat <= 90 and -180 <= lon <= 180):
        blocking.append(
            f"Site coordinates are out of range (lat {lat}, lon {lon}); "
            "latitude must be −90..90 and longitude −180..180."
        )
    # Derive Country/Continent from coordinates (offline) so the leaderboard's
    # continent count and the site's country are correct the instant a submission
    # is promoted — no manual `backfill_geography` re-run needed. A contributor-
    # supplied country wins; the coord-derived value only fills a blank.
    from ..management.commands.backfill_geography import geo_for_coords
    geo_country, geo_continent = geo_for_coords(lat, lon)
    site_kwargs = {
        "Latitude": lat, "Longitude": lon,
        "MAP": _num(gv("map")), "MAT": _num(gv("mat")),
        "SiteName": _fit(Site, "SiteName", gv("site_name"), warnings),
        "Country": _fit(Site, "Country", gv("country"), warnings) or geo_country,
        "Continent": geo_continent,
    }

    experiment_name = _fit(Experiment, "ExperimentName", gv("pub_title"), warnings)

    norm = _normalized_treatments(submission)
    if not norm["treatments"]:
        blocking.append("No treatments to promote.")
    td = submission.treatment_details or {}
    descriptions = td.get("descriptions", {}) or {}
    names = td.get("names", {}) or {}  # identifier -> optional readable TreatmentName

    plan_treatments: list = []
    raw_total = 0
    for t in norm["treatments"]:
        def tv(fid, _t=t):
            return _resolved_treatment_value(submission, _t, fid)

        if not tv("primary_crop"):
            blocking.append(f"Treatment '{t}' has no primary crop.")
        tkwargs = {
            # Identifier `t` keys the rows; the readable name (if any) is the label written.
            "TreatmentName": _fit(Treatment, "TreatmentName", names.get(t) or t, warnings),
            "PrimaryCrop": _fit(Treatment, "PrimaryCrop", tv("primary_crop"), warnings),
            "SecondaryCrop": _fit(Treatment, "SecondaryCrop", tv("secondary_crop"), warnings),
            "CoverCrop": _fit(Treatment, "CoverCrop", tv("cover_crop"), warnings),
            "Management": _fit(Treatment, "Management", tv("management"), warnings),
            "Tillage": _fit(Treatment, "Tillage", tv("tillage"), warnings),
            "FluxInstrument": _fit(Treatment, "FluxInstrument", tv("flux_instrument"), warnings),
            "SandMean": _num(tv("sand")), "SiltMean": _num(tv("silt")), "ClayMean": _num(tv("clay")),
            "pHMean": _num(tv("ph")), "BulkDensityMean": _num(tv("bulk_density")),
            "SOMMean": _num(tv("som")),
            "TreatmentDescription": descriptions.get(t) or None,
        }
        records = _raw_records(norm["perTreatment"][t]["records"], tkwargs["Management"])
        raw_total += len(records)
        plan_treatments.append({"name": t, "kwargs": tkwargs, "records": records})

    return {
        "publication_kwargs": pub_kwargs,
        "site_kwargs": site_kwargs,
        "experiment_name": experiment_name,
        "treatments": plan_treatments,
        "counts": {"treatments": len(norm["treatments"]), "raw_rows": raw_total},
        "blocking": blocking,
        "warnings": warnings,
        "doi_duplicates": find_duplicate_publications(doi),
    }


def dry_run(submission) -> dict:
    """A curator-facing preview: what would be written, and any issues — no DB writes."""
    plan = build_plan(submission)
    return {
        "publication": plan["publication_kwargs"],
        "site": plan["site_kwargs"],
        "experiment_name": plan["experiment_name"],
        "treatment_names": [t["name"] for t in plan["treatments"]],
        "counts": plan["counts"],
        "blocking": plan["blocking"],
        "warnings": plan["warnings"],
        "doi_duplicates": plan["doi_duplicates"],
        "already_promoted": bool(submission.promotion),
        "promotable": submission.status in _PROMOTABLE and not submission.promotion,
    }


# --- write -------------------------------------------------------------------

def promote(submission) -> dict:
    """Write the submission into the live tables and mark it ``promoted``.

    Guards (raise ``PromotionError``): already promoted, wrong status, or a blocking
    data issue. All inserts share one transaction — any error rolls the whole thing back.
    """
    from django.utils import timezone
    from ..models_upload import UploadSubmission

    # Cheap pre-check outside the transaction for a fast, friendly failure; the
    # authoritative check happens under the row lock below.
    if submission.promotion:
        raise PromotionError("This submission has already been promoted.")
    if submission.status not in _PROMOTABLE:
        raise PromotionError(f"Cannot promote a submission with status '{submission.status}'.")

    with transaction.atomic():
        # Row-lock the submission so two concurrent promotes (double-click, two tabs)
        # can't both pass the already-promoted guard and write duplicate trees. The
        # second waits here, then sees promotion set and bails.
        locked = UploadSubmission.objects.select_for_update().get(pk=submission.pk)
        if locked.promotion:
            raise PromotionError("This submission has already been promoted.")
        if locked.status not in _PROMOTABLE:
            raise PromotionError(f"Cannot promote a submission with status '{locked.status}'.")

        plan = build_plan(locked)
        if plan["blocking"]:
            raise PromotionError("Cannot promote — " + "; ".join(plan["blocking"]))

        pub = Publication.objects.create(**plan["publication_kwargs"])
        # Continent/Country are derived from lat/lon in build_plan (site_kwargs) via
        # geo_for_coords, so they're filled at promote time — no backfill_geography re-run.
        site = Site.objects.create(**plan["site_kwargs"])
        exp = Experiment.objects.create(
            ExperimentName=plan["experiment_name"], SiteID=site, PubID=pub
        )
        treatment_ids: list = []
        raw_count = 0
        for t in plan["treatments"]:
            trt = Treatment.objects.create(ExperimentID=exp, **t["kwargs"])
            treatment_ids.append(trt.pk)
            objs = [
                RawMeasurementTreatment(
                    treatmentid=trt, date=r["date"], management=r["management"], **r["raw"]
                )
                for r in t["records"]
            ]
            RawMeasurementTreatment.objects.bulk_create(objs, batch_size=1000)
            raw_count += len(objs)

        result = {
            "pubid": pub.pk, "siteid": site.pk, "experimentid": exp.pk,
            "treatmentids": treatment_ids, "raw_count": raw_count,
        }
        locked.promotion = result
        locked.status = "promoted"
        locked.submitted_at = locked.submitted_at or timezone.now()
        locked.save(update_fields=["promotion", "status", "submitted_at", "updated_at"])

    # Reflect the committed result on the caller's instance.
    submission.promotion = result
    submission.status = "promoted"
    submission.submitted_at = locked.submitted_at
    return result
