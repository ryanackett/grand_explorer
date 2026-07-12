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
            "n2o_flux": {"canonical_field": "n2oflux", "unit": "g N/ha/d", "source": "user"},
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

    def test_soil_leftover_per_treatment_from_static_sheet(self):
        # A separate soil sheet's columns are offered, with per-treatment values keyed by
        # that sheet's own treatment column (first plot value per treatment).
        from n2odb.views_workbench import _soil_leftover_columns
        sub = _submission()
        sub.parsed_content["sheets"].append({
            "name": "Soil",
            "headers": ["PlotID", "Sand_pct"],
            "rows": [["NT", "40"], ["NT", "41"], ["CT", "55"], ["CT", "56"]],
            "dtypes": ["string", "float"],
            "n_rows": 4, "n_rows_stored": 4, "header_row": 0, "truncated": False,
        })
        sub.save(update_fields=["parsed_content"])
        cols = {c["key"]: c for c in _soil_leftover_columns(sub)}
        sand = cols["1::Sand_pct"]
        self.assertEqual(sand["sheet"], "Soil")
        self.assertEqual(sand["value"], "40")                       # global first value
        self.assertEqual(sand["treatment_values"], {"NT": "40", "CT": "55"})


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

    def test_readable_names_saved_and_keyed_by_identifier(self):
        # A readable name can be attached to each identifier; orphan names are dropped.
        self.client.post(
            reverse("workbench-soil-save", args=[self.token]),
            data={
                "names": {"NT": "No-till + urea", "CT": "", "Ghost": "ignored"},
                "descriptions": {},
            },
            content_type="application/json",
        )
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.treatment_details["names"], {"NT": "No-till + urea"})

    def test_treatments_locked_to_detected(self):
        # The client cannot rename/add treatments — identity is owned by the data column,
        # or annotations would orphan from the raw rows. Submitted names are ignored.
        resp = self.client.post(
            reverse("workbench-soil-save", args=[self.token]),
            data={
                "treatments": ["Renamed-NT", "CT", "Invented"],   # all ignored
                "descriptions": {"Renamed-NT": "x"},              # orphan → dropped
                "per_treatment": {
                    "NT": {"tillage": {"value": "no-till", "source": "user"}},   # kept (detected)
                    "Invented": {"tillage": {"value": "x", "source": "user"}},   # dropped
                },
            },
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.sub.refresh_from_db()
        td = self.sub.treatment_details
        self.assertEqual(td["treatments"], ["NT", "CT"])       # detected, not the submitted names
        self.assertNotIn("Renamed-NT", td["descriptions"])
        self.assertIn("NT", td["per_treatment"])
        self.assertNotIn("Invented", td["per_treatment"])
