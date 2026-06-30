import csv
from collections import OrderedDict
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

# ---------------------------------------------------------------------------
# Type coercion helpers. The CSV is a denormalized export from a DB View, so
# empty cells arrive as empty strings and the HM flags arrive as "0.0"/"1.0".
# ---------------------------------------------------------------------------

_NULLISH = {"", "na", "nan", "none", "null"}


def to_float(s):
    if s is None or str(s).strip().lower() in _NULLISH:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def to_int(s):
    f = to_float(s)
    return None if f is None else int(f)


def to_bool_flag(s):
    """'0'/'0.0' -> False, anything else numeric -> True, blank -> None."""
    f = to_float(s)
    return None if f is None else (f != 0)


def to_date(s):
    if s is None or not str(s).strip():
        return None
    return datetime.strptime(str(s).strip(), "%Y-%m-%d").date()


def norm(s):
    """Normalize a business-key string for case/whitespace-insensitive matching."""
    return (s or "").strip().lower()


def clip(s, length):
    """Truncate a string to a CharField max_length (None-safe)."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    return s[:length]


class Command(BaseCommand):
    help = (
        "Split the denormalized ETL/RawData.csv export into the normalized n2odb "
        "tables (Publication / Site / Experiment / Treatment / RawMeasurementTreatment). "
        "Only publications not already in the DB (matched on PubTitle OR Citation) are added."
    )

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="Path to RawData.csv")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and build everything, report counts, then roll back without committing.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=5000,
            help="bulk_create batch size for measurement rows (default: 5000).",
        )
        parser.add_argument(
            "--show-skipped",
            action="store_true",
            help="Print the title/citation of each publication skipped as already present.",
        )

    def handle(self, *args, **options):
        csv_path = options["csv_path"]
        dry_run = options["dry_run"]
        batch_size = options["batch_size"]
        show_skipped = options["show_skipped"]

        # 1. Preload dedup sets of existing publications (normalized).
        existing_titles = {
            norm(t) for t in Publication.objects.values_list("PubTitle", flat=True) if norm(t)
        }
        existing_citations = {
            norm(c) for c in Publication.objects.values_list("Citation", flat=True) if norm(c)
        }
        self.stdout.write(
            f"Loaded {len(existing_titles)} existing titles / "
            f"{len(existing_citations)} existing citations for dedup."
        )

        # 2. Read the CSV and group rows by source PubID (defensive: not assuming order).
        self.stdout.write(f"Reading {csv_path} ...")
        pubs = OrderedDict()  # src PubID -> list[row dict]
        try:
            with open(csv_path, newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                total_rows = 0
                for row in reader:
                    total_rows += 1
                    pubs.setdefault(row.get("PubID"), []).append(row)
        except FileNotFoundError:
            raise CommandError(f"CSV not found: {csv_path}")

        self.stdout.write(
            f"Parsed {total_rows} rows across {len(pubs)} source publications."
        )

        # Running totals for the summary.
        stats = dict(pubs_added=0, pubs_skipped=0, sites=0, experiments=0,
                     treatments=0, measurements=0, rows_skipped=0)

        for src_pubid, rows in pubs.items():
            first = rows[0]
            title_n = norm(first.get("PubTitle"))
            cite_n = norm(first.get("Citation"))

            # Skip publications already present in the DB or already imported this run.
            if (title_n and title_n in existing_titles) or (cite_n and cite_n in existing_citations):
                stats["pubs_skipped"] += 1
                if show_skipped:
                    matched_on = "title" if (title_n and title_n in existing_titles) else "citation"
                    self.stdout.write(
                        f"  SKIP src PubID={src_pubid} (matched {matched_on}): "
                        f"{(first.get('PubTitle') or '').strip()[:100]}"
                    )
                continue

            try:
                self._import_publication(first, rows, stats, batch_size, dry_run)
            except Exception as exc:  # per-publication atomic block already rolled back
                self.stderr.write(
                    self.style.ERROR(f"  FAILED src PubID={src_pubid}: {exc}")
                )
                continue

            # Mark as seen so within-file repeats (and reruns) are deduped.
            if title_n:
                existing_titles.add(title_n)
            if cite_n:
                existing_citations.add(cite_n)
            stats["pubs_added"] += 1

        # 5. Summary.
        style = self.style.WARNING if dry_run else self.style.SUCCESS
        self.stdout.write(style(
            ("[DRY RUN] " if dry_run else "")
            + "Done. "
            f"Publications added={stats['pubs_added']} skipped={stats['pubs_skipped']}; "
            f"Sites={stats['sites']} Experiments={stats['experiments']} "
            f"Treatments={stats['treatments']} Measurements={stats['measurements']}; "
            f"Rows skipped (no date)={stats['rows_skipped']}."
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("Nothing was committed (dry run)."))

    def _import_publication(self, first, rows, stats, batch_size, dry_run):
        """Create the full hierarchy for one source publication, atomically."""
        with transaction.atomic():
            sid = transaction.savepoint()

            publication = Publication.objects.create(
                PubTitle=clip(first.get("PubTitle"), 255),
                Citation=clip(first.get("Citation"), 500),
            )

            # One Site per unique (Latitude, Longitude) within this publication.
            site_by_coord = {}

            def get_site(row):
                lat = to_float(row.get("Latitude")) or 0.0  # Latitude is NOT NULL
                lon = to_float(row.get("Longitude")) or 0.0  # Longitude is NOT NULL
                key = (lat, lon)
                site = site_by_coord.get(key)
                if site is None:
                    site = Site.objects.create(
                        Latitude=lat,
                        Longitude=lon,
                        MAP=to_float(row.get("MAP")),
                        MAT=to_float(row.get("MAT")),
                        Gracenet=to_bool_flag(row.get("Gracenet")),
                    )
                    site_by_coord[key] = site
                    stats["sites"] += 1
                return site

            # One Experiment per source ExperimentID (linked to its row's Site + this Publication).
            exp_by_src = {}

            def get_experiment(row):
                src = row.get("ExperimentID")
                exp = exp_by_src.get(src)
                if exp is None:
                    exp = Experiment.objects.create(
                        ExperimentName=clip(
                            f"{first.get('PubTitle') or 'Publication'} - exp {src}", 255
                        ),
                        SiteID=get_site(row),
                        PubID=publication,
                    )
                    exp_by_src[src] = exp
                    stats["experiments"] += 1
                return exp

            # One Treatment per source TreatmentID (linked to its Experiment).
            trt_by_src = {}

            def get_treatment(row):
                src = row.get("TreatmentID")
                trt = trt_by_src.get(src)
                if trt is None:
                    trt = Treatment.objects.create(
                        ExperimentID=get_experiment(row),
                        PrimaryCrop=clip(row.get("PrimaryCrop"), 32) or "Unknown",  # NOT NULL
                        Management=clip(row.get("Management"), 16),
                        SandMean=to_float(row.get("SandMean")),
                        SiltMean=to_float(row.get("SiltMean")),
                        ClayMean=to_float(row.get("ClayMean")),
                    )
                    trt_by_src[src] = trt
                    stats["treatments"] += 1
                return trt

            # Build the measurement rows.
            measurements = []
            for row in rows:
                date = to_date(row.get("Date"))
                if date is None:  # date is NOT NULL on RawMeasurementTreatment
                    stats["rows_skipped"] += 1
                    continue
                measurements.append(RawMeasurementTreatment(
                    treatmentid=get_treatment(row),
                    date=date,
                    doy=to_int(row.get("DOY")),
                    n2oflux=to_float(row.get("N2OFlux")),
                    fluxstandarderror=to_float(row.get("FluxStandardError")),
                    vwc=to_float(row.get("VWC")),
                    vwccalculated=to_float(row.get("VWCcalculated")),
                    wfps=to_float(row.get("WFPS")),
                    wfpscalculated=to_float(row.get("WFPScalculated")),
                    soilt=to_float(row.get("SoilT")),
                    airt=to_float(row.get("AirT")),
                    airtmax=to_float(row.get("AirTMax")),
                    airtmin=to_float(row.get("AirTMin")),
                    precip=to_float(row.get("Precip")),
                    nitrogenapplied=to_float(row.get("NitrogenApplied")),
                    nitrogentype=clip(row.get("NitrogenType"), 45),
                    hmtruth=to_bool_flag(row.get("HMTruth")),
                    hmmcd=to_bool_flag(row.get("HMMCD")),
                    hmmad=to_bool_flag(row.get("HMMAD")),
                    hmknn=to_bool_flag(row.get("HMKNN")),
                    hmiqr=to_bool_flag(row.get("HMIQR")),
                ))

            RawMeasurementTreatment.objects.bulk_create(measurements, batch_size=batch_size)
            stats["measurements"] += len(measurements)

            if dry_run:
                transaction.savepoint_rollback(sid)
