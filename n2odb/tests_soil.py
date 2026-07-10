"""Tests for Stage 4 — soil & management (global or per-treatment)."""
from __future__ import annotations

from django.test import TestCase
from n2odb.tests_common import AuthedWorkbenchTestCase
from django.urls import reverse

from n2odb.models_upload import UploadSubmission
from n2odb.views_workbench import _detected_treatments, _treatment_column


def _submission(**overrides):
    data = dict(
        parsed_content={
            "source": "file",
            "source_name": "s.csv",
            "sheets": [
                {
                    "name": "s.csv",
                    "headers": ["date", "n2o_flux", "PlotID", "pH", "Tillage"],
                    "rows": [
                        ["2018-05-01", "1.2", "NT", "6.4", "no-till"],
                        ["2018-05-02", "0.8", "NT", "6.4", "no-till"],
                        ["2018-05-01", "2.1", "CT", "5.9", "conventional"],
                        ["2018-05-02", "1.7", "CT", "5.9", "conventional"],
                    ],
                    "dtypes": ["date", "float", "string", "float", "string"],
                    "n_rows": 4, "n_rows_stored": 4, "header_row": 0, "truncated": False,
                }
            ],
        },
        mapping={
            "date": {"canonical_field": "date", "source": "user"},
            "n2o_flux": {"canonical_field": "n2oflux", "source": "user"},
            "PlotID": {"canonical_field": "treatment", "source": "user"},
        },
    )
    data.update(overrides)
    return UploadSubmission.objects.create(**data)


class TreatmentDetectionTests(TestCase):
    def test_treatment_column_and_distinct_values(self):
        sub = _submission()
        self.assertEqual(_treatment_column(sub), "PlotID")
        self.assertEqual(_detected_treatments(sub, "PlotID"), ["NT", "CT"])

    def test_no_treatment_column(self):
        sub = _submission(mapping={"date": {"canonical_field": "date", "source": "user"}})
        self.assertIsNone(_treatment_column(sub))
        self.assertEqual(_detected_treatments(sub, None), [])


class SoilViewTests(AuthedWorkbenchTestCase):
    def setUp(self):
        super().setUp()
        self.sub = _submission()
        self.token = self.sub.public_token

    def test_page_renders_treatment_stage(self):
        resp = self.client.get(reverse("workbench-soil", args=[self.token]))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Soil &amp; management", body)
        self.assertIn('id="detected-data"', body)
        # Soil/Crop fields present; a global-only field (country) is not on this stage.
        self.assertIn('"ph"', body)
        self.assertIn('"primary_crop"', body)
        self.assertNotIn('"country"', body)

    def test_page_without_data_redirects(self):
        empty = UploadSubmission.objects.create()
        resp = self.client.get(reverse("workbench-soil", args=[empty.public_token]))
        self.assertEqual(resp.status_code, 302)

    def test_save_global_and_per_treatment(self):
        resp = self.client.post(
            reverse("workbench-soil-save", args=[self.token]),
            data={
                "treatments": ["NT", "CT"],
                "descriptions": {"NT": "no-till continuous corn", "CT": "", "Ghost": "x"},
                "scopes": {"Soil": "global", "Crop & management": "per_treatment", "Bogus": "x"},
                "global": {
                    "ph": {"value": "6.2", "source": "user"},
                    "not_a_field": {"value": "x", "source": "user"},  # dropped
                },
                "per_treatment": {
                    "NT": {"tillage": {"value": "no-till", "source": "user"}},
                    "CT": {"tillage": {"value": "conventional", "source": "user"}},
                    "Ghost": {"tillage": {"value": "x"}},  # not in treatments -> dropped
                },
            },
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["treatments"], 2)
        self.assertEqual(data["global_filled"], 1)         # not_a_field dropped
        self.assertEqual(data["per_treatment_filled"], 2)  # Ghost dropped

        self.sub.refresh_from_db()
        td = self.sub.treatment_details
        self.assertEqual(td["scopes"], {"Soil": "global", "Crop & management": "per_treatment"})
        self.assertEqual(td["treatment_column"], "PlotID")
        self.assertEqual(td["global"]["ph"]["value"], "6.2")
        self.assertNotIn("not_a_field", td["global"])
        self.assertEqual(td["per_treatment"]["NT"]["tillage"]["value"], "no-till")
        self.assertNotIn("Ghost", td["per_treatment"])
        # descriptions: kept for known treatments, empty/unknown dropped.
        self.assertEqual(td["descriptions"], {"NT": "no-till continuous corn"})
