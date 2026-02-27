import pandas as pd
import numpy as np
from django.core.management.base import BaseCommand
from n2odb.models import RawMeasurementTreatment
from pyod.models.mad import MAD
from pyod.models.mcd import MCD

class Command(BaseCommand):
    help = 'Detects flux outliers (Hot Moments) and updates the HMMAD column.'

    def handle(self, *args, **options):
        self.stdout.write("Fetching measurements from the database...")
        
        # 1. Fetch only the columns you need to save memory
        qs = RawMeasurementTreatment.objects.all().values('rawtreatmentid','date', 'treatmentid', 'n2oflux', 'hmmad', 'hmmcd')

        if not qs.exists():
            self.stdout.write(self.style.WARNING("No data found."))
            return

        # 2. Load into Pandas
        df = pd.DataFrame(list(qs))
        df = df.dropna(subset=['n2oflux'])

        print(df.head())

        self.stdout.write("Calculating outliers using IQR grouped by Treatment...")

        # Create a list to hold our fully processed chunks of data
        processed_groups = []

        # 3. IQR Outlier function
        for treatment_id, group in df.groupby('treatmentid'):
            mad_threshold =  2.025
            mcd_treshold = 17

            # Sort by date to maintain temporal order
            id_data = group.sort_values('date')
            flux = id_data[['n2oflux']]
            flux['index'] = np.arange(len(flux))

            mad = MAD(threshold=1.2)
            mad.fit(id_data[['n2oflux']])
            mad_scores = mad.decision_scores_
            mad_pred = [1 if score > mad_threshold else 0 for score in mad_scores]

            mcd = MCD(contamination=0.5)
            mcd.fit(flux)
            mcd_scores = mcd.decision_scores_
            mcd_pred = [1 if score > mcd_treshold else 0 for score in mcd_scores]

            median = np.median(flux['n2oflux'])
            # If flux is less than median but flagged as outlier, set to non-outlier (to avoid false positives on low flux values)
            mad_pred = [0 if (flux.iloc[i]['n2oflux'] < median and mad_pred[i] == 1) else mad_pred[i] for i in range(len(flux))]
            mcd_pred = [0 if (flux.iloc[i]['n2oflux'] < median and mcd_pred[i] == 1) else mcd_pred[i] for i in range(len(flux))]

            # If flux is less than 1, set to non-outlier (to avoid false positives on very low flux values)
            mad_pred = [0 if (flux.iloc[i]['n2oflux'] < 1 and mad_pred[i] == 1) else mad_pred[i] for i in range(len(flux))]
            mcd_pred = [0 if (flux.iloc[i]['n2oflux'] < 1 and mcd_pred[i] == 1) else mcd_pred[i] for i in range(len(flux))]

            id_data['hmmad'] = mad_pred
            id_data['hmmcd'] = mcd_pred

            # Add the finished chunk to our list
            processed_groups.append(id_data)
        
        
        # Apply the math grouped by treatment
        df = pd.concat(processed_groups, ignore_index=True)

        self.stdout.write("Packaging data for bulk update...")

        objects_to_update = []
        for index, row in df.iterrows():
            # Instantiate a shell of the Django model containing ONLY the primary key and the updated fields
            obj = RawMeasurementTreatment(
                rawtreatmentid=row['rawtreatmentid'], 
                hmmad=row['hmmad'],
                hmmcd=row['hmmcd']
            )
            objects_to_update.append(obj)

        self.stdout.write(f"Writing {len(objects_to_update)} records to the database...")

        # 5. Execute Bulk Update 
        batch_size = 5000 
        RawMeasurementTreatment.objects.bulk_update(
            objects_to_update, 
            ['hmmad', 'hmmcd'],  # Tells Django to strictly ONLY touch these two columns
            batch_size=batch_size
        )

        self.stdout.write(self.style.SUCCESS(f"Success! Database fully updated with {len(objects_to_update)} records."))

                