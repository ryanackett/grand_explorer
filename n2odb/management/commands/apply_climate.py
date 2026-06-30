"""
Fill Site climate (MAP / MAT) from ETL/site_climate.csv, reversibly.

Columns: SiteID, MAP, MAT, Source. Values come from the Dorich Summary export
(MeanPrec_mm / MeanTemp_c, averaged over the site's references) with a NASA POWER
climatology fallback; the Source column records which per field. It is ignored
when applying.

A Site is considered to have NO climate when the field is NULL *or* 0 — the legacy
rows carry 0 as an "unknown" placeholder. So this command fills NULL-or-0 by
default; a real, non-zero existing value is left alone unless --overwrite. A
backup manifest makes every run reversible.

    manage.py apply_climate ETL/site_climate.csv --dry-run
    manage.py apply_climate ETL/site_climate.csv
    manage.py apply_climate --undo ETL/climate_manifest_<ts>.json
"""

import csv
import json
import os
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from n2odb.models import Site

FIELDS = ("MAP", "MAT")


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
    help = "Fill Site MAP/MAT from a CSV (NULL or 0 placeholder), reversibly."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", nargs="?", help="ETL/site_climate.csv")
        parser.add_argument("--dry-run", action="store_true",
                            help="Apply, report, then roll back without committing.")
        parser.add_argument("--overwrite", action="store_true",
                            help="Also replace existing non-zero values (default fills NULL/0 only).")
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
        if not rows or "SiteID" not in rows[0]:
            raise CommandError("CSV missing a SiteID column.")

        changes = []
        stats = dict(rows=0, set=0, skipped_filled=0, missing=0, noop=0)
        try:
            with transaction.atomic():
                for r in rows:
                    sid = (r.get("SiteID") or "").strip()
                    if not sid:
                        continue
                    proposed = {f: _num(r.get(f)) for f in FIELDS}
                    if not any(v is not None for v in proposed.values()):
                        stats["noop"] += 1
                        continue
                    stats["rows"] += 1
                    try:
                        site = Site.objects.get(pk=int(sid))
                    except Site.DoesNotExist:
                        stats["missing"] += 1
                        self.stderr.write(self.style.WARNING(f"  SiteID {sid}: not found, skipped"))
                        continue
                    touched = []
                    for f in FIELDS:
                        new = proposed[f]
                        if new is None:
                            continue
                        old = getattr(site, f)
                        # NULL or 0 placeholder is fillable; a real value needs --overwrite
                        if old not in (None, 0) and not overwrite:
                            stats["skipped_filled"] += 1
                            continue
                        if old == new:
                            continue
                        setattr(site, f, new)
                        changes.append({"SiteID": int(sid), "field": f, "old": old, "new": new})
                        touched.append(f)
                        stats["set"] += 1
                    if touched:
                        site.save(update_fields=touched)
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
                                         f"climate_manifest_{ts}.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        self._summary(stats, False)
        self.stdout.write(self.style.SUCCESS(f"Committed. Undo manifest: {manifest_path}"))
        self.stdout.write(f"  Reverse with:  manage.py apply_climate --undo {manifest_path}")

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
                    site = Site.objects.get(pk=c["SiteID"])
                except Site.DoesNotExist:
                    continue
                if getattr(site, c["field"]) != c["new"] and not force:
                    drift += 1
                    self.stderr.write(self.style.WARNING(
                        f"  SiteID {c['SiteID']}.{c['field']} changed since — skipping (--force to override)."))
                    continue
                setattr(site, c["field"], c["old"])
                site.save(update_fields=[c["field"]])
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
            f"skipped (real value, no --overwrite)={stats['skipped_filled']}; "
            f"SiteID not found={stats['missing']}; empty rows={stats['noop']}."))
        if dry_run:
            self.stdout.write(self.style.WARNING("Rolled back — nothing committed."))
