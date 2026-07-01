"""
Fill Treatment soil physics/chemistry from ETL/soil_enrichment.csv, reversibly.

Columns: TreatmentID, SandMean, SiltMean, ClayMean, pHMean, BulkDensityMean,
SOMMean, Source. Values come from the Dorich Summary export (authoritative) with
a SoilGrids gridded fallback; the Source column records, per field, which one was
used (e.g. "pHMean:summary;BulkDensityMean:soilgrids"). The Source column is
informational and ignored when applying.

These are *fills*, not corrections: by default only fields that are currently
NULL are written, so an existing measured value is never overwritten and empty
CSV cells never null anything. A backup manifest makes every run reversible.

    manage.py apply_soil ETL/soil_enrichment.csv --dry-run
    manage.py apply_soil ETL/soil_enrichment.csv
    manage.py apply_soil --undo ETL/soil_manifest_<ts>.json
"""

import csv
import json
import os
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from n2odb.models import Treatment

# CSV column -> model attribute
FIELDS = {
    "SandMean": "SandMean", "SiltMean": "SiltMean", "ClayMean": "ClayMean",
    "pHMean": "pHMean", "BulkDensityMean": "BulkDensityMean", "SOMMean": "SOMMean",
}


def _num(v):
    if not (isinstance(v, str) and v.strip()):
        return None
    try:
        return float(v.strip())
    except ValueError:
        return None


class _Rollback(Exception):
    """Internal sentinel to unwind a dry-run transaction."""


class Command(BaseCommand):
    help = "Fill Treatment soil fields (Sand/Silt/Clay/pH/BulkDensity/SOM) from a CSV, reversibly."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", nargs="?", help="ETL/soil_enrichment.csv")
        parser.add_argument("--dry-run", action="store_true",
                            help="Apply, report, then roll back without committing.")
        parser.add_argument("--overwrite", action="store_true",
                            help="Also replace existing non-NULL values (default fills NULL only).")
        parser.add_argument("--manifest", help="Where to write the undo manifest.")
        parser.add_argument("--undo", metavar="MANIFEST",
                            help="Restore prior values from this manifest instead of applying.")
        parser.add_argument("--force", action="store_true",
                            help="With --undo: restore even if a value changed since.")

    def handle(self, *a, **o):
        if o.get("undo"):
            return self._undo(o["undo"], o["force"])
        if not o.get("csv_path"):
            raise CommandError("Provide the CSV path, or use --undo <manifest>.")
        return self._apply(o["csv_path"], o["dry_run"], o["overwrite"], o.get("manifest"))

    def _apply(self, csv_path, dry_run, overwrite, manifest_path):
        if not os.path.exists(csv_path):
            raise CommandError(f"CSV not found: {csv_path}")
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        if not rows or "TreatmentID" not in rows[0]:
            raise CommandError("CSV missing a TreatmentID column.")

        changes = []
        stats = dict(rows=0, set=0, skipped_filled=0, missing=0, noop=0)
        try:
            with transaction.atomic():
                for r in rows:
                    tid = (r.get("TreatmentID") or "").strip()
                    if not tid:
                        continue
                    proposed = {f: _num(r.get(f)) for f in FIELDS}
                    if not any(v is not None for v in proposed.values()):
                        stats["noop"] += 1
                        continue
                    stats["rows"] += 1
                    try:
                        trt = Treatment.objects.get(pk=int(tid))
                    except Treatment.DoesNotExist:
                        stats["missing"] += 1
                        self.stderr.write(self.style.WARNING(f"  TreatmentID {tid}: not found, skipped"))
                        continue
                    touched = []
                    for col, attr in FIELDS.items():
                        new = proposed[col]
                        if new is None:
                            continue
                        old = getattr(trt, attr)
                        if old is not None and not overwrite:
                            stats["skipped_filled"] += 1
                            continue
                        if old == new:
                            continue
                        setattr(trt, attr, new)
                        changes.append({"TreatmentID": int(tid), "field": attr, "old": old, "new": new})
                        touched.append(attr)
                        stats["set"] += 1
                    if touched:
                        trt.save(update_fields=touched)
                if dry_run:
                    raise _Rollback()
        except _Rollback:
            self._summary(stats, True)
            return

        manifest = {"created": datetime.now().isoformat(timespec="seconds"),
                    "csv": os.path.abspath(csv_path), "overwrite": overwrite, "changes": changes}
        if not manifest_path:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            manifest_path = os.path.join(os.path.dirname(os.path.abspath(csv_path)),
                                         f"soil_manifest_{ts}.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        self._summary(stats, False)
        self.stdout.write(self.style.SUCCESS(f"Committed. Undo manifest: {manifest_path}"))
        self.stdout.write(f"  Reverse with:  manage.py apply_soil --undo {manifest_path}")

    def _undo(self, manifest_path, force):
        if not os.path.exists(manifest_path):
            raise CommandError(f"Manifest not found: {manifest_path}")
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        changes = manifest.get("changes", [])
        if not changes:
            self.stdout.write("Manifest records no changes; nothing to undo.")
            return
        restored = drift = 0
        with transaction.atomic():
            for c in changes:
                try:
                    trt = Treatment.objects.get(pk=c["TreatmentID"])
                except Treatment.DoesNotExist:
                    continue
                if getattr(trt, c["field"]) != c["new"] and not force:
                    drift += 1
                    self.stderr.write(self.style.WARNING(
                        f"  TreatmentID {c['TreatmentID']}.{c['field']} changed since — skipping (--force to override)."))
                    continue
                setattr(trt, c["field"], c["old"])
                trt.save(update_fields=[c["field"]])
                restored += 1
        msg = f"Undo complete: restored {restored} field(s)."
        if drift:
            msg += f" Skipped {drift} changed since (use --force)."
        self.stdout.write(self.style.SUCCESS(msg))

    def _summary(self, stats, dry_run):
        tag = "[DRY RUN] " if dry_run else ""
        style = self.style.WARNING if dry_run else self.style.SUCCESS
        self.stdout.write(style(
            f"{tag}Fields set={stats['set']}; rows considered={stats['rows']}; "
            f"skipped (already filled, no --overwrite)={stats['skipped_filled']}; "
            f"TreatmentID not found={stats['missing']}; empty rows={stats['noop']}."))
        if dry_run:
            self.stdout.write(self.style.WARNING("Rolled back — nothing committed."))
