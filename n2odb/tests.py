import csv
import io
import zipfile
from datetime import date

from django.test import TestCase
from django.urls import reverse

from n2odb.models import Experiment, Publication, RawMeasurementTreatment, Site, Treatment


class ExportCalculatedMoistureTests(TestCase):
    """Calculated VWC/WFPS ship in their own columns, next to (not merged into) the reported ones."""

    def test_calculated_columns_exported_alongside_reported(self):
        site = Site.objects.create(Latitude=1, Longitude=2)
        exp = Experiment.objects.create(SiteID=site, PubID=Publication.objects.create())
        trt = Treatment.objects.create(ExperimentID=exp, PrimaryCrop='Zea mays (Corn)', BulkDensityMean=1.325)
        RawMeasurementTreatment.objects.create(treatmentid=trt, date=date(2020, 1, 1), n2oflux=1.0,
                                               wfps=0.6, vwccalculated=0.3)

        resp = self.client.post(reverse('download_zip'), {'ids': [trt.pk]})
        zbytes = b''.join(resp.streaming_content)
        rows = list(csv.DictReader(io.StringIO(
            zipfile.ZipFile(io.BytesIO(zbytes)).read('n2odb_data_export.csv').decode('utf-8'))))

        self.assertEqual(rows[0]['VWC (%)'], '')  # reported VWC stays blank
        self.assertEqual(rows[0]['VWC Calculated (from WFPS + BD)'], '0.3')
        self.assertEqual(rows[0]['WFPS (%)'], '0.6')
        self.assertEqual(rows[0]['WFPS Calculated (from VWC + BD)'], '')
