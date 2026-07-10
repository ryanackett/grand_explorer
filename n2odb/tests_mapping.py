"""Tests for the column-mapping suggestion engine and mapping save endpoint."""
from __future__ import annotations

from django.test import TestCase
from n2odb.tests_common import AuthedWorkbenchTestCase
from django.urls import reverse

from n2odb.models_upload import UploadSubmission
from n2odb.services import suggest, units


class SuggestTests(TestCase):
    def test_alias_and_fuzzy_variants(self):
        cases = {
            "N2O_Flux": "n2oflux",
            "n2o flux": "n2oflux",
            "Nitrous oxide flux": "n2oflux",
            "date": "date",
            "Sampling Date": "date",
            "Soil Temp (C)": "soilt",
            "soilTemperature": "soilt",
            "WFPS%": "wfps",
            "VWC": "vwc",
            "soil moisture": "vwc",
            "NH4-N": "soilnh4",
            "NO3": "soilno3",
            "rainfall": "precip",
            "N applied (kg N/ha)": "nitrogenapplied",
            "AirTemp": "airt",
            "crop": "plantedcrop",
            "Treatment": "treatment",
            "PlotID": "treatment",
            "plot_id": "treatment",
        }
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(suggest.suggest(header).field_id, expected)

    def test_unrelated_headers_get_no_suggestion(self):
        for header in ["replicate", "notes", "xyz123"]:
            with self.subTest(header=header):
                self.assertIsNone(suggest.suggest(header).field_id)

    def test_collision_resolves_to_highest_confidence(self):
        # Two columns both look like flux; only the stronger match keeps it.
        out = suggest.suggest_columns(["n2oflux", "n2o-ish flux thing"])
        assigned = [o["canonical_field"] for o in out]
        self.assertEqual(assigned.count("n2oflux"), 1)

    def test_dtype_mismatch_lowers_confidence(self):
        # A fuzzy (non-exact) match to a numeric field: a text column should score
        # lower than a numeric one. (Exact alias hits are trusted regardless.)
        numeric = suggest.suggest("nitrogen application", "float")
        texty = suggest.suggest("nitrogen application", "string")
        self.assertEqual(numeric.source, "fuzzy")
        self.assertEqual(numeric.field_id, "nitrogenapplied")
        self.assertLess(texty.confidence, numeric.confidence)


class MappingEndpointTests(AuthedWorkbenchTestCase):
    def setUp(self):
        super().setUp()
        self.submission = UploadSubmission.objects.create(
            parsed_content={
                "source": "file",
                "source_name": "study.csv",
                "sheets": [
                    {
                        "name": "study.csv",
                        "headers": ["site", "date", "n2o_flux"],
                        "rows": [["KBS", "2018-05-01", "1.2"], ["KBS", "2018-05-02", "0.8"]],
                        "dtypes": ["string", "date", "float"],
                        "n_rows": 2,
                        "n_rows_stored": 2,
                        "header_row": 0,
                        "truncated": False,
                    }
                ],
            }
        )
        self.token = self.submission.public_token

    def test_mapping_page_renders_with_suggestions(self):
        resp = self.client.get(reverse("workbench-mapping", args=[self.token]))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('id="columns-data"', body)
        self.assertIn("time-series", body)
        self.assertIn('data-field="n2oflux"', body)

    def test_mapping_page_without_data_redirects(self):
        empty = UploadSubmission.objects.create()
        resp = self.client.get(reverse("workbench-mapping", args=[empty.public_token]))
        self.assertEqual(resp.status_code, 302)

    def test_save_mapping_persists_valid_fields(self):
        resp = self.client.post(
            reverse("workbench-mapping-save", args=[self.token]),
            data={"mapping": {"date": "date", "n2o_flux": "n2oflux", "site": "bogus_field"}},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["mapped_count"], 2)  # bogus_field dropped
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.mapping["date"]["canonical_field"], "date")
        self.assertNotIn("site", self.submission.mapping)

    def test_save_mapping_stores_unit(self):
        resp = self.client.post(
            reverse("workbench-mapping-save", args=[self.token]),
            data={"mapping": {
                "N2O": {"field": "n2oflux", "unit": "kg N/ha/d"},
                "T": {"field": "soilt", "unit": "not-a-real-unit"},  # dropped
            }},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.mapping["N2O"]["unit"], "kg N/ha/d")
        # An invalid unit is not stored, but the mapping still is.
        self.assertNotIn("unit", self.submission.mapping["T"])


class UnitsTests(TestCase):
    def test_extract_unit_hint(self):
        cases = {
            "N2O_flux (g N/ha/d)": "g N/ha/d",
            "NH4+ (mg/kg)": "mg/kg",
            "WFPS (%)": "%",
            "Soil Temp °C": "°C",
            "Rain_mm": "mm",
            "plot_id": "",
        }
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(units.extract_unit_hint(header), expected)

    def test_options_canonical_first(self):
        self.assertEqual(units.options_for("soilt")[0], "°C")
        self.assertEqual(units.options_for("plantedcrop"), [])  # no unit concept
