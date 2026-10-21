import pandas as pd
import numpy as np
from django.core.management.base import BaseCommand
from n2odb.models import RawMeasurementTreatment

# Standard mineral-soil particle density (g/cm3). Used to derive total
# porosity from bulk density:   porosity = 1 - (BulkDensity / PARTICLE_DENSITY)
PARTICLE_DENSITY = 2.65

class Command(BaseCommand):
    help = ('Calculates VWCCalculated and WFPSCalculated from the treatment '
            'bulk density and the alternate measured soil-moisture metric. '
            'VWC and WFPS are both stored as fractions (0-1); bulk density is g/cm3.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Compute values and write soil_moisture_dry_run.csv without touching the database.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        self.stdout.write("Fetching measurements from the database...")

        # Pull the moisture metrics plus the parent treatment's bulk density.
        qs = RawMeasurementTreatment.objects.all().values(
            'rawtreatmentid', 'vwc', 'wfps',
            'treatmentid__BulkDensityMean',
        )

        if not qs.exists():
            self.stdout.write(self.style.WARNING("No data found."))
            return

        df = pd.DataFrame(list(qs))
        df = df.rename(columns={'treatmentid__BulkDensityMean': 'bd'})

        self.stdout.write("Calculating porosity and derived moisture values...")

        # Porosity (fraction). NaN where bulk density is missing or physically
        # impossible (BD >= particle density -> non-positive porosity).
        bd = pd.to_numeric(df['bd'], errors='coerce')
        porosity = 1.0 - (bd / PARTICLE_DENSITY)
        porosity = porosity.where((porosity > 0) & (porosity < 1))

        vwc = pd.to_numeric(df['vwc'], errors='coerce')
        wfps = pd.to_numeric(df['wfps'], errors='coerce')

        # VWCCalculated comes from the *alternate* metric (WFPS):
        #   VWC = WFPS * porosity
        df['vwccalculated'] = (wfps * porosity).round(4)

        # WFPSCalculated comes from the *alternate* metric (VWC):
        #   WFPS = VWC / porosity
        df['wfpscalculated'] = (vwc / porosity).round(4)

        # Normalise inf -> NaN. NaN is converted to None at object-build time
        # below (a float Series can't actually hold None), otherwise MySQL
        # rejects the non-finite value with "nan can not be used with MySQL".
        for col in ('vwccalculated', 'wfpscalculated'):
            df[col] = df[col].replace([np.inf, -np.inf], np.nan)

        n_vwc = df['vwccalculated'].notna().sum()
        n_wfps = df['wfpscalculated'].notna().sum()
        self.stdout.write(
            f"Derived VWCCalculated for {n_vwc} rows and WFPSCalculated for {n_wfps} rows."
        )

        if dry_run:
            out = 'soil_moisture_dry_run.csv'
            df[['rawtreatmentid', 'bd', 'vwc', 'wfps',
                'vwccalculated', 'wfpscalculated']].to_csv(out, index=False)
            self.stdout.write(self.style.SUCCESS(f"Dry run complete. Wrote {out} (no database changes)."))
            return

        self.stdout.write("Packaging data for bulk update...")

        def clean(v):
            # NaN (missing input or missing porosity) -> real SQL NULL.
            return None if pd.isna(v) else float(v)

        objects_to_update = [
            RawMeasurementTreatment(
                rawtreatmentid=row['rawtreatmentid'],
                vwccalculated=clean(row['vwccalculated']),
                wfpscalculated=clean(row['wfpscalculated']),
            )
            for _, row in df.iterrows()
        ]

        self.stdout.write(f"Writing {len(objects_to_update)} records to the database...")

        # These are continuous floats, so the group-by-value trick used in
        # hot_moment_detection doesn't apply. bulk_update with a modest
        # batch_size chunks the work into many small UPDATEs instead of one
        # giant CASE statement, which keeps the mysqld container from OOMing.
        RawMeasurementTreatment.objects.bulk_update(
            objects_to_update,
            ['vwccalculated', 'wfpscalculated'],
            batch_size=500,
        )

        self.stdout.write(self.style.SUCCESS(
            f"Success! Database fully updated with {len(objects_to_update)} records."
        ))
