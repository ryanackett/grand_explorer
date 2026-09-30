"""Tests for Stage 3 — site/study details (Plane B)."""
from __future__ import annotations

from django.test import TestCase
from n2odb.tests_common import AuthedWorkbenchTestCase
from django.urls import reverse

from n2odb.models_upload import UploadSubmission
from n2odb.services import site_fields, suggest


class SiteFieldSuggestTests(TestCase):
    def test_csv_columns_match_site_fields(self):
        cases = {
            "pH": "ph",
            "Bulk Density": "bulk_density",
            "SOC (%)": "som",
            "Latitude": "site_latitude",
            "Longitude": "site_longitude",
            "Clay content": "clay",
            "MAP": "map",
        }
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(
                    suggest.suggest(header, fields=site_fields.SITE_FIELDS).field_id,
                    expected,
                )

    def test_measurement_columns_do_not_match_site_fields(self):
        for header in ["CO2_flux", "CH4_flux", "Notes", "Chamber_ID"]:
            with self.subTest(header=header):
                self.assertIsNone(
                    suggest.suggest(header, fields=site_fields.SITE_FIELDS).field_id
                )


class SiteViewTests(AuthedWorkbenchTestCase):
    def setUp(self):
        super().setUp()
        self.submission = UploadSubmission.objects.create(
            parsed_content={
                "source": "file",
                "source_name": "s.csv",
                "sheets": [
                    {
                        "name": "s.csv",
                        "headers": ["date", "n2o_flux", "pH", "Latitude", "Longitude"],
                        "rows": [
                            ["2018-05-01", "1.2", "6.4", "42.41", "-85.37"],
                            ["2018-05-02", "0.8", "6.4", "42.41", "-85.37"],
                        ],
                        "dtypes": ["date", "float", "float", "float", "float"],
                        "n_rows": 2, "n_rows_stored": 2, "header_row": 0, "truncated": False,
                    }
                ],
            },
            # date & flux mapped in Stage 2; pH/lat/long left over for Stage 3.
            mapping={
                "date": {"canonical_field": "date", "source": "user"},
                "n2o_flux": {"canonical_field": "n2oflux", "unit": "g N/ha/d", "source": "user"},
            },
            # a deterministic fact from the manuscript step
            manuscript_facts={"metadata": {"pub_doi": {
                "value": "10.1/x", "source_quote": "doi:10.1/x", "confidence": 1.0}}},
        )
        self.token = self.submission.public_token

    def test_site_page_renders_with_leftover_columns_and_prefill(self):
        resp = self.client.get(reverse("workbench-site", args=[self.token]))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Site &amp; study details", body)
        self.assertIn('id="in-site_latitude"', body)
        # leftover pH/Latitude/Longitude are offered; mapped date/flux are not
        self.assertIn('"pH"', body)
        self.assertIn('"pub_doi"', body)  # prefilled from manuscript facts

    def test_leftover_columns_exclude_mapped(self):
        from n2odb.views_workbench import _leftover_columns
        names = [c["name"] for c in _leftover_columns(self.submission)]
        self.assertIn("pH", names)
        self.assertNotIn("date", names)
        self.assertNotIn("n2o_flux", names)

    def test_leftover_columns_span_all_sheets_with_unique_keys(self):
        # A static second sheet's columns must be offered too, and same-named columns
        # on different sheets stay distinct via their sheet-qualified key.
        from n2odb.views_workbench import _leftover_columns
        self.submission.parsed_content["sheets"].append({
            "name": "Soil", "headers": ["pH", "Sand_pct"],
            "rows": [["6.6", "40"], ["6.7", "41"]],
            "dtypes": ["float", "float"],
            "n_rows": 2, "n_rows_stored": 2, "header_row": 0, "truncated": False,
        })
        self.submission.save(update_fields=["parsed_content"])
        cols = _leftover_columns(self.submission)
        keys = {c["key"] for c in cols}
        self.assertIn("0::pH", keys)          # leftover on the measurement sheet
        self.assertIn("1::pH", keys)          # same name on the soil sheet, distinct key
        self.assertIn("1::Sand_pct", keys)
        sheets = {c["key"]: c["sheet"] for c in cols}
        self.assertEqual(sheets["1::Sand_pct"], "Soil")

    def test_save_site_persists_values(self):
        resp = self.client.post(
            reverse("workbench-site-save", args=[self.token]),
            data={"values": {
                "site_latitude": {"value": "42.41", "source": "column", "column": "Latitude"},
                "country": {"value": "USA", "source": "user"},
                "primary_crop": {"value": "maize", "source": "user"},  # treatment-level, dropped here
                "bogus": {"value": "x", "source": "user"},
            }},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["filled_count"], 2)  # bogus + primary_crop dropped
        self.assertIn("site_latitude", data["required_filled"])
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.metadata_values["site_latitude"]["column"], "Latitude")
        self.assertEqual(self.submission.metadata_values["country"]["value"], "USA")
        # primary_crop belongs to the Soil & Management stage now.
        self.assertNotIn("primary_crop", self.submission.metadata_values)

    def test_save_site_coerces_spoofed_source_to_user(self):
        # A crafted request can't claim provenance we didn't assign (curator trust signal).
        resp = self.client.post(
            reverse("workbench-site-save", args=[self.token]),
            data={"values": {
                "country": {"value": "USA", "source": "document"},   # legitimate
                "site_name": {"value": "Farm", "source": "paper"},   # spoofed
            }},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.metadata_values["country"]["source"], "document")
        self.assertEqual(self.submission.metadata_values["site_name"]["source"], "user")

    def test_derived_link_prefilled_from_doi(self):
        # A DOI implies a link, so the Site form shows one even when no explicit
        # Link was saved or extracted.
        from n2odb.views_workbench import _initial_site_values
        values = _initial_site_values(self.submission, site_fields.global_fields())
        self.assertEqual(values["pub_link"]["value"], "https://doi.org/10.1/x")
        self.assertEqual(values["pub_link"]["source"], "derived")

    def test_save_site_normalizes_a_doi_link(self):
        resp = self.client.post(
            reverse("workbench-site-save", args=[self.token]),
            data={"values": {"pub_link": {"value": "doi.org/10.1234/abc", "source": "user"}}},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["warnings"], [])
        self.submission.refresh_from_db()
        self.assertEqual(
            self.submission.metadata_values["pub_link"]["value"],
            "https://doi.org/10.1234/abc",
        )

    def test_save_site_warns_on_an_unresolvable_link(self):
        resp = self.client.post(
            reverse("workbench-site-save", args=[self.token]),
            data={"values": {"pub_link": {"value": "not a link", "source": "user"}}},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["warnings"])
        self.submission.refresh_from_db()
        self.assertNotIn("pub_link", self.submission.metadata_values)
