"""
Apply resolved Link / Abstract / Citation values to the Publication table from a
reviewed CSV (ETL/pub_enrichment.csv or ETL/gracenet_candidates.csv), reversibly.

Only columns present in the CSV are used, so the journal sheet (no Citation
column) sets Link/Abstract, while the GRACEnet sheet additionally carries a
proper individual-study Citation that should REPLACE the inadequate umbrella
citation — for that, run with --overwrite (the backup manifest still makes it
fully reversible). Empty CSV cells never overwrite anything.

    manage.py apply_enrichment <csv>                 # fill empty Link/Abstract
    manage.py apply_enrichment <csv> --dry-run       # report, roll back
    manage.py apply_enrichment <csv> --overwrite     # also replace existing values
    manage.py apply_enrichment --undo <manifest>     # restore prior values exactly

The CSV is produced out-of-band by the DOI/abstract resolver and is meant to be
eyeballed first (drop or edit any row you don't trust). Only the PubID, Link and
Abstract columns are used here; everything else (Verdict, MatchScore, ...) is for
your review. A row with empty Link AND Abstract is a no-op.

Reversibility: by default only *empty* fields are filled, so this never destroys
existing data. Every field it changes is recorded — old and new value — in a JSON
manifest; `--undo <manifest>` restores the old values, and refuses if a field has
since changed away from what we wrote (unless --force). No schema/migration touched.
"""

import csv
import json
import os
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from n2odb.models import Publication

FIELDS = ("Link", "Abstract", "Citation", "PubTitle")   # CSV column -> model field
MAXLEN = {"Link": 255, "Citation": 500, "PubTitle": 255}  # CharField caps; Abstract is TextField


def _clean(v, field=None):
    if not (isinstance(v, str) and v.strip()):
        return None
    v = v.strip()
    cap = MAXLEN.get(field)
    return v[:cap] if cap else v


class Command(BaseCommand):
    help = "Fill Publication.Link / Publication.Abstract from a reviewed CSV, reversibly."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", nargs="?", help="ETL/pub_enrichment.csv")
        parser.add_argument("--dry-run", action="store_true",
                            help="Apply, report, then roll back without committing.")
        parser.add_argument("--overwrite", action="store_true",
                            help="Also replace existing non-empty Link/Abstract values.")
        parser.add_argument("--manifest", help="Where to write the undo manifest.")
        parser.add_argument("--undo", metavar="MANIFEST",
                            help="Restore prior values from this manifest instead of applying.")
        parser.add_argument("--force", action="store_true",
                            help="With --undo: restore even if a field changed since.")

    def handle(self, *a, **o):
        if o.get("undo"):
            return self._undo(o["undo"], o["force"])
        if not o.get("csv_path"):
            raise CommandError("Provide the CSV path, or use --undo <manifest>.")
        return self._apply(o["csv_path"], o["dry_run"], o["overwrite"], o.get("manifest"))

    # ----------------------------------------------------------------- apply #

    def _apply(self, csv_path, dry_run, overwrite, manifest_path):
        if not os.path.exists(csv_path):
            raise CommandError(f"CSV not found: {csv_path}")

        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        if not rows or "PubID" not in rows[0]:
            raise CommandError("CSV missing a PubID column.")

        changes = []      # manifest entries: {PubID, field, old, new}
        stats = dict(rows=0, set=0, skipped_filled=0, missing_pub=0, noop=0)

        try:
            with transaction.atomic():
                for r in rows:
                    pid = (r.get("PubID") or "").strip()
                    if not pid:
                        continue
                    proposed = {f: _clean(r.get(f), f) for f in FIELDS}
                    if not any(proposed.values()):
                        stats["noop"] += 1
                        continue
                    stats["rows"] += 1
                    try:
                        pub = Publication.objects.get(pk=int(pid))
                    except Publication.DoesNotExist:
                        stats["missing_pub"] += 1
                        self.stderr.write(self.style.WARNING(f"  PubID {pid}: not found, skipped"))
                        continue

                    touched = []
                    for f in FIELDS:
                        new = proposed[f]
                        if new is None:
                            continue
                        old = getattr(pub, f)
                        if old and not overwrite:
                            stats["skipped_filled"] += 1
                            continue
                        if old == new:
                            continue
                        setattr(pub, f, new)
                        changes.append({"PubID": int(pid), "field": f,
                                        "old": old, "new": new})
                        touched.append(f)
                        stats["set"] += 1
                    if touched:
                        pub.save(update_fields=touched)

                if dry_run:
                    raise _Rollback()
        except _Rollback:
            self._summary(stats, dry_run=True)
            return

        # Persist the manifest so the change can be reversed.
        manifest = {"created": datetime.now().isoformat(timespec="seconds"),
                    "csv": os.path.abspath(csv_path), "overwrite": overwrite,
                    "changes": changes}
        if not manifest_path:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            manifest_path = os.path.join(os.path.dirname(os.path.abspath(csv_path)),
                                         f"enrich_manifest_{ts}.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, ensure_ascii=False)

        self._summary(stats, dry_run=False)
        self.stdout.write(self.style.SUCCESS(f"Committed. Undo manifest: {manifest_path}"))
        self.stdout.write(f"  Reverse with:  manage.py apply_enrichment --undo {manifest_path}")

    # ------------------------------------------------------------------ undo #

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
                    pub = Publication.objects.get(pk=c["PubID"])
                except Publication.DoesNotExist:
                    continue
                cur = getattr(pub, c["field"])
                if cur != c["new"] and not force:
                    drift += 1
                    self.stderr.write(self.style.WARNING(
                        f"  PubID {c['PubID']}.{c['field']} changed since load "
                        f"— skipping (use --force to override)."))
                    continue
                setattr(pub, c["field"], c["old"])
                pub.save(update_fields=[c["field"]])
                restored += 1
        msg = f"Undo complete: restored {restored} field(s)."
        if drift:
            msg += f" Skipped {drift} that changed since (use --force)."
        self.stdout.write(self.style.SUCCESS(msg))

    # --------------------------------------------------------------- summary #

    def _summary(self, stats, dry_run):
        tag = "[DRY RUN] " if dry_run else ""
        style = self.style.WARNING if dry_run else self.style.SUCCESS
        self.stdout.write(style(
            f"{tag}Fields set={stats['set']}; "
            f"rows considered={stats['rows']}; "
            f"skipped (already filled)={stats['skipped_filled']}; "
            f"PubID not found={stats['missing_pub']}; empty rows={stats['noop']}."
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("Rolled back — nothing committed."))


class _Rollback(Exception):
    """Internal sentinel to unwind a dry-run transaction."""
