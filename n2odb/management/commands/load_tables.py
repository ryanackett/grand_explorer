"""
Load the normalized per-table CSVs produced by DorichScraper's build_tables.py
into the n2odb schema, in foreign-key order, as a single atomic transaction.

    manage.py load_tables <dir>                 # load + write an undo manifest
    manage.py load_tables <dir> --dry-run       # parse, report, roll back
    manage.py load_tables --undo <manifest>     # reverse a previous load exactly

Design notes
------------
* The CSVs carry EXPLICIT surrogate primary keys and foreign keys, offset above
  the live DB's MAX(pk) (see etl.sh). Nothing is auto-assigned, so every loaded
  row occupies a known, contiguous id range that does not overlap existing data.

* Reversibility is the whole point: a load writes a JSON *manifest* recording,
  per table, the pk range and row count it inserted. `--undo <manifest>` deletes
  exactly those pk ranges, in reverse FK order, and refuses to run if the rows it
  finds don't match what the manifest recorded (so it can never clobber rows it
  did not create). It touches no schema and runs no migration, so model and
  migration integrity are untouched.

* Type/NULL safety: empty cells become NULL (not 0 / "" ), via the coercion
  helpers below; this matters because the columns are mostly nullable floats.

This is deliberately a plain-ORM loader rather than a django-import-export
ModelResource: the CSVs are already keyed and FK-resolved, so explicit
bulk_create with pre-set pks is simpler, faster, and — critically — makes the
undo an exact, dependency-free pk-range delete.
"""

import csv
import json
import os
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from n2odb.models import (
    Publication,
    Site,
    Experiment,
    Treatment,
    RawMeasurementTreatment,
)

# --------------------------------------------------------------------------- #
# Type coercion. Empty cells -> None (NULL), never 0 or "".
# --------------------------------------------------------------------------- #

_NULLISH = {"", "na", "nan", "none", "null"}


def to_float(v):
    if v is None or str(v).strip().lower() in _NULLISH:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def to_int(v):
    f = to_float(v)
    return None if f is None else int(f)


def to_bool(v):
    """'0'/'0.0' -> False, other numeric -> True, blank -> None."""
    f = to_float(v)
    return None if f is None else (f != 0)


def to_date(v):
    if v is None or not str(v).strip():
        return None
    return datetime.strptime(str(v).strip(), "%Y-%m-%d").date()


def clip(v, length):
    """Trim a string to a CharField max_length; blank -> None."""
    if v is None:
        return None
    v = str(v).strip()
    return v[:length] if v else None


def required_str(v, length, default):
    """Like clip(), but never None (for NOT NULL CharField columns)."""
    return clip(v, length) or default


# --------------------------------------------------------------------------- #
# Per-table row builders. Each maps a CSV row dict -> an unsaved model instance,
# assigning the explicit pk and FK *_id values straight from the CSV.
# --------------------------------------------------------------------------- #

def build_publication(r):
    return Publication(
        PubID=int(r["PubID"]),
        PubTitle=clip(r.get("PubTitle"), 255),
        LeadAuthor=clip(r.get("LeadAuthor"), 32),
        Citation=clip(r.get("Citation"), 500),
        PubYear=to_int(r.get("PubYear")),
        Link=clip(r.get("Link"), 255),
    )


def build_site(r):
    return Site(
        SiteID=int(r["SiteID"]),
        Latitude=to_float(r.get("Latitude")),    # NOT NULL in the model
        Longitude=to_float(r.get("Longitude")),  # NOT NULL in the model
        MAP=to_float(r.get("MAP")),
        MAT=to_float(r.get("MAT")),
        Gracenet=to_bool(r.get("Gracenet")),
        SiteName=clip(r.get("SiteName"), 255),
    )


def build_experiment(r):
    return Experiment(
        ExperimentID=int(r["ExperimentID"]),
        ExperimentName=required_str(r.get("ExperimentName"), 255, "Experiment"),
        SiteID_id=int(r["SiteID"]),
        PubID_id=int(r["PubID"]),
    )


def build_treatment(r):
    return Treatment(
        TreatmentID=int(r["TreatmentID"]),
        ExperimentID_id=int(r["ExperimentID"]),
        PrimaryCrop=required_str(r.get("PrimaryCrop"), 32, "Unknown"),
        Management=clip(r.get("Management"), 16),
        Tillage=clip(r.get("Tillage"), 32),
        FluxInstrument=clip(r.get("FluxInstrument"), 45),
        TreatmentName=clip(r.get("TreatmentName"), 45),
        SandMean=to_float(r.get("SandMean")),
        SiltMean=to_float(r.get("SiltMean")),
        ClayMean=to_float(r.get("ClayMean")),
        TreatmentDescription=clip(r.get("TreatmentDescription"), 65535),
        pHMean=to_float(r.get("pH")),                # db_column 'pH'
        BulkDensityMean=to_float(r.get("BulkDensity")),  # db_column 'BulkDensity'
        SOMMean=to_float(r.get("SOMMean")),
    )


def build_raw(r):
    return RawMeasurementTreatment(
        rawtreatmentid=int(r["RawTreatmentID"]),
        treatmentid_id=int(r["TreatmentID"]),
        date=to_date(r.get("Date")),               # NOT NULL in the model
        doy=to_int(r.get("DOY")),
        n2oflux=to_float(r.get("N2OFlux")),
        fluxstandarderror=to_float(r.get("FluxStandardError")),
        vwc=to_float(r.get("VWC")),
        vwccalculated=to_float(r.get("VWCCalculated")),
        wfps=to_float(r.get("WFPS")),
        wfpscalculated=to_float(r.get("WFPSCalculated")),
        soilt=to_float(r.get("SoilT")),
        airt=to_float(r.get("AirT")),
        airtmax=to_float(r.get("AirTMax")),
        airtmin=to_float(r.get("AirTMin")),
        precip=to_float(r.get("Precip")),
        irrigationapplied=to_float(r.get("IrrigationApplied")),
        nitrogenapplied=to_float(r.get("NitrogenApplied")),
        nitrogenform=clip(r.get("NitrogenForm"), 64),
        nitrogentype=clip(r.get("NitrogenType"), 45),
        soilnh4=to_float(r.get("SoilNH4")),
        soilno3=to_float(r.get("SoilNO3")),
        plantedcrop=clip(r.get("PlantedCrop"), 32),
        tillage=to_int(r.get("Tillage")),
        harvest=to_int(r.get("Harvest")),
        management=clip(r.get("Management"), 45),
    )


# FK load order. (filename, model, pk attribute, row builder).
TABLES = [
    ("Publication.csv", Publication, "PubID", build_publication),
    ("Site.csv", Site, "SiteID", build_site),
    ("Experiment.csv", Experiment, "ExperimentID", build_experiment),
    ("Treatment.csv", Treatment, "TreatmentID", build_treatment),
    ("RawMeasurementTreatment.csv", RawMeasurementTreatment, "rawtreatmentid", build_raw),
]


class _Rollback(Exception):
    """Internal sentinel to unwind a dry-run transaction."""


class Command(BaseCommand):
    help = (
        "Load DorichScraper's per-table CSVs (Publication, Site, Experiment, "
        "Treatment, RawMeasurementTreatment) into n2odb in FK order, atomically, "
        "writing an undo manifest. Use --undo <manifest> to reverse a load exactly."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "directory",
            nargs="?",
            help="Directory containing the five per-table CSVs.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and insert, report counts, then roll back without committing.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=5000,
            help="bulk_create / delete batch size (default: 5000).",
        )
        parser.add_argument(
            "--manifest",
            help="Path to write the undo manifest (default: <dir>/load_manifest_<ts>.json).",
        )
        parser.add_argument(
            "--undo",
            metavar="MANIFEST",
            help="Reverse the load described by this manifest instead of loading.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="With --undo: delete the recorded pk ranges even if the current "
                 "row counts don't match the manifest (use with care).",
        )

    def handle(self, *args, **opts):
        if opts.get("undo"):
            return self._undo(opts["undo"], opts["batch_size"], opts["force"])

        directory = opts.get("directory")
        if not directory:
            raise CommandError("Provide a directory of CSVs, or use --undo <manifest>.")
        return self._load(directory, opts["dry_run"], opts["batch_size"], opts.get("manifest"))

    # ------------------------------------------------------------------ load #

    def _load(self, directory, dry_run, batch_size, manifest_path):
        # Verify every expected CSV is present before touching the DB.
        paths = {}
        for fname, *_ in TABLES:
            p = os.path.join(directory, fname)
            if not os.path.exists(p):
                raise CommandError(f"Missing CSV: {p}")
            paths[fname] = p

        manifest = {
            "created": datetime.now().isoformat(timespec="seconds"),
            "source_dir": os.path.abspath(directory),
            "dry_run": dry_run,
            "tables": [],   # in load order
        }

        try:
            with transaction.atomic():
                for fname, model, pk_attr, builder in TABLES:
                    inserted = self._load_one(paths[fname], model, pk_attr, builder, batch_size)
                    manifest["tables"].append(inserted)

                if dry_run:
                    raise _Rollback()
        except _Rollback:
            self.stdout.write(self.style.WARNING("[DRY RUN] rolled back — nothing committed."))
            self._print_summary(manifest)
            return

        # Committed: persist the manifest so the load can be reversed.
        if not manifest_path:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            manifest_path = os.path.join(directory, f"load_manifest_{ts}.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)

        self._print_summary(manifest)
        self.stdout.write(self.style.SUCCESS(f"Committed. Undo manifest: {manifest_path}"))
        self.stdout.write(f"  Reverse with:  manage.py load_tables --undo {manifest_path}")

    def _load_one(self, path, model, pk_attr, builder, batch_size):
        db_table = model._meta.db_table
        objs, pks = [], []
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for lineno, row in enumerate(csv.DictReader(fh), start=2):
                try:
                    obj = builder(row)
                except (ValueError, KeyError) as exc:
                    raise CommandError(f"{os.path.basename(path)} line {lineno}: {exc}")
                objs.append(obj)
                pks.append(getattr(obj, pk_attr))

        if not pks:
            raise CommandError(f"{os.path.basename(path)} has no data rows.")

        pk_min, pk_max = min(pks), max(pks)
        # Guard: refuse to load onto ids that already exist (would collide /
        # silently merge). The offsets in build_tables.py exist to prevent this.
        clash = model.objects.filter(pk__range=(pk_min, pk_max)).count()
        if clash:
            raise CommandError(
                f"{db_table}: {clash} existing row(s) already occupy pk range "
                f"[{pk_min}, {pk_max}]. Re-run build_tables.py with offsets above "
                f"the live MAX(pk) (see etl.sh) before loading."
            )

        model.objects.bulk_create(objs, batch_size=batch_size)
        self.stdout.write(
            f"  {db_table:<26} +{len(objs):>6} rows  pk [{pk_min}..{pk_max}]"
        )
        return {
            "model": model.__name__,
            "db_table": db_table,
            "pk_field": model._meta.pk.name,
            "pk_column": model._meta.pk.column,
            "count": len(pks),
            "pk_min": pk_min,
            "pk_max": pk_max,
        }

    # ------------------------------------------------------------------ undo #

    def _undo(self, manifest_path, batch_size, force):
        if not os.path.exists(manifest_path):
            raise CommandError(f"Manifest not found: {manifest_path}")
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        if manifest.get("dry_run"):
            raise CommandError("This manifest was a --dry-run; nothing was committed to undo.")

        by_name = {model.__name__: model for _, model, _, _ in TABLES}

        # Reverse FK order = reverse of the recorded load order.
        tables = list(reversed(manifest["tables"]))

        # 1. Verify first: every table's live row count in its recorded range
        #    must equal what the manifest says, or we abort (unless --force).
        problems = []
        for t in tables:
            model = by_name[t["model"]]
            present = model.objects.filter(pk__range=(t["pk_min"], t["pk_max"])).count()
            status = "ok" if present == t["count"] else "MISMATCH"
            if present != t["count"]:
                problems.append(
                    f"  {t['db_table']:<26} manifest={t['count']} present={present}  [{status}]"
                )
            else:
                self.stdout.write(
                    f"  {t['db_table']:<26} manifest={t['count']} present={present}  [{status}]"
                )
        if problems:
            self.stdout.write(self.style.ERROR("\n".join(problems)))
            if not force:
                raise CommandError(
                    "Row counts don't match the manifest — refusing to delete. The data "
                    "may have changed since the load. Re-check, or pass --force to delete "
                    "exactly the recorded pk ranges anyway."
                )
            self.stdout.write(self.style.WARNING("--force: deleting recorded pk ranges despite mismatch."))

        # 2. Delete, in reverse FK order, exactly the recorded pk ranges.
        with transaction.atomic():
            for t in tables:
                model = by_name[t["model"]]
                deleted = self._delete_range(model, t["pk_min"], t["pk_max"], batch_size)
                self.stdout.write(
                    f"  {t['db_table']:<26} -{deleted:>6} rows  pk [{t['pk_min']}..{t['pk_max']}]"
                )

        self.stdout.write(self.style.SUCCESS("Undo complete — loaded rows removed; existing data untouched."))

    def _delete_range(self, model, pk_min, pk_max, batch_size):
        """Delete pk in [pk_min, pk_max] in batches; returns total deleted."""
        total = 0
        lo = pk_min
        while lo <= pk_max:
            hi = min(lo + batch_size - 1, pk_max)
            n, _ = model.objects.filter(pk__range=(lo, hi)).delete()
            total += n
            lo = hi + 1
        return total

    # --------------------------------------------------------------- summary #

    def _print_summary(self, manifest):
        total = sum(t["count"] for t in manifest["tables"])
        self.stdout.write(self.style.SUCCESS(f"Total rows: {total}"))
