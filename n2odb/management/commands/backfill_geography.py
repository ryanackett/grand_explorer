"""
Derive Site.Country and Site.Continent from Latitude/Longitude, offline.

Coordinates are reverse-geocoded to an ISO country code with `reverse_geocoder`
(a local k-d tree over GeoNames places — no network, no API), then mapped to a
country name and continent with `pycountry_convert`. A site's coordinates never
change, so this is computed once and stored; RE-RUN THIS COMMAND after adding
new sites to fill them in.

By default only NULL fields are filled (idempotent — safe to re-run). Sites with
missing or (0, 0) placeholder coordinates are skipped and left blank.

    manage.py backfill_geography --dry-run     # preview, commit nothing
    manage.py backfill_geography               # fill NULL Country/Continent
    manage.py backfill_geography --overwrite   # recompute all sites
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from n2odb.models import Site

FIELDS = ("Country", "Continent")


def _valid_coord(lat, lon):
    """Reject missing coords and the (0, 0) placeholder (Gulf of Guinea)."""
    if lat is None or lon is None:
        return False
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return False
    if lat == 0 and lon == 0:
        return False
    return True


def _geo_for_code(cc):
    """(country_name, continent_name) for an ISO alpha-2 code; either may be None."""
    from pycountry_convert import (
        country_alpha2_to_country_name,
        country_alpha2_to_continent_code,
        convert_continent_code_to_continent_name,
    )
    country = continent = None
    try:
        country = country_alpha2_to_country_name(cc)
    except (KeyError, ValueError):
        pass
    try:
        continent = convert_continent_code_to_continent_name(
            country_alpha2_to_continent_code(cc)
        )
    except (KeyError, ValueError):
        # e.g. Antarctica (AQ) has no continent code in pycountry_convert.
        pass
    return country, continent


def geo_for_coords(lat, lon):
    """(country, continent) for a single lat/lon, offline; (None, None) if uncodable.

    Same derivation the batch command uses, exposed for one-off calls (e.g. at
    promotion). Importing reverse_geocoder loads its k-d tree into the process on
    first use — a one-time cost per worker; skipped entirely for invalid coords.
    """
    if not _valid_coord(lat, lon):
        return None, None
    import reverse_geocoder as rg
    res = rg.search([(lat, lon)], mode=1)
    cc = (res[0].get("cc") or "").strip().upper() if res else ""
    return _geo_for_code(cc) if cc else (None, None)


class Command(BaseCommand):
    help = "Derive Site Country/Continent from coordinates (offline reverse-geocode)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would change, then roll back.")
        parser.add_argument("--overwrite", action="store_true",
                            help="Recompute all sites (default fills NULL fields only).")

    def handle(self, *args, **opts):
        import reverse_geocoder as rg

        dry_run = opts["dry_run"]
        overwrite = opts["overwrite"]

        sites = list(Site.objects.all())

        # Decide which sites still need work (NULL fields, unless --overwrite).
        pending = []
        for s in sites:
            if overwrite or s.Country is None or s.Continent is None:
                pending.append(s)

        # Split into geocodable (valid coords) and skipped.
        to_geocode = [s for s in pending if _valid_coord(s.Latitude, s.Longitude)]
        no_coord = [s for s in pending if not _valid_coord(s.Latitude, s.Longitude)]

        stats = dict(considered=len(pending), set=0, unchanged=0,
                     no_coord=len(no_coord), already=len(sites) - len(pending))

        if not to_geocode:
            self._summary(stats, dry_run)
            return

        # One batched reverse-geocode (builds the tree once). mode=1 = single
        # process, which is the safe choice inside a container.
        coords = [(s.Latitude, s.Longitude) for s in to_geocode]
        results = rg.search(coords, mode=1)

        try:
            with transaction.atomic():
                for site, res in zip(to_geocode, results):
                    cc = (res.get("cc") or "").strip().upper()
                    country, continent = _geo_for_code(cc) if cc else (None, None)
                    proposed = {"Country": country, "Continent": continent}

                    touched = []
                    for f in FIELDS:
                        new = proposed[f]
                        if new is None:
                            continue
                        old = getattr(site, f)
                        if old is not None and not overwrite:
                            continue
                        if old == new:
                            continue
                        setattr(site, f, new)
                        touched.append(f)

                    if touched:
                        site.save(update_fields=touched)
                        stats["set"] += 1
                        self.stdout.write(
                            f"  Site {site.pk} ({site.SiteName or 'unnamed'}): "
                            f"{site.Country} / {site.Continent}"
                        )
                    else:
                        stats["unchanged"] += 1

                if dry_run:
                    transaction.set_rollback(True)
        finally:
            pass

        self._summary(stats, dry_run)

    def _summary(self, stats, dry_run):
        tag = "[DRY RUN] " if dry_run else ""
        style = self.style.WARNING if dry_run else self.style.SUCCESS
        self.stdout.write(style(
            f"{tag}Sites updated={stats['set']}; considered={stats['considered']}; "
            f"unchanged={stats['unchanged']}; skipped (no/invalid coords)={stats['no_coord']}; "
            f"already filled={stats['already']}."
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("Rolled back — nothing committed."))
