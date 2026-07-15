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
            # Plot / replicate identifiers now route to the dedicated replication field.
            "PlotID": "replication",
            "plot_id": "replication",
            "replicate": "replication",
            "block": "replication",
        }
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(suggest.suggest(header).field_id, expected)

    def test_unrelated_headers_get_no_suggestion(self):
        for header in ["notes", "xyz123"]:
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

    def test_stale_saved_field_falls_back_not_vanish(self):
        # A saved mapping pointing at a field that's no longer mappable (e.g. from
        # before a registry change) must not mark the column "mapped" with no target
        # to render on — it would vanish. Fall back to the suggestion instead.
        import json, re
        self.submission.mapping = {"date": {"canonical_field": "plot", "source": "user"}}
        self.submission.save(update_fields=["mapping"])
        body = self.client.get(reverse("workbench-mapping", args=[self.token])).content.decode()
        cols = json.loads(re.search(r'id="columns-data"[^>]*>(.*?)</script>', body, re.S).group(1))
        date_col = next(c for c in cols if c["name"] == "date")
        self.assertNotEqual(date_col["assigned"], "plot")   # stale value dropped
        self.assertEqual(date_col["assigned"], "date")       # healed to the suggestion

    def test_collision_two_columns_one_field_does_not_vanish(self):
        # A stale save mapping two columns to the same field ("Block" and "Treatment"
        # both -> treatment) must not hide one behind the other. The rightful owner
        # keeps the field; the other falls back to its own suggestion (or the pool).
        import json, re
        sub = UploadSubmission.objects.create(
            owner=self.submission.owner,
            parsed_content={
                "source": "file", "source_name": "s.csv",
                "sheets": [{
                    "name": "s.csv",
                    "headers": ["Treatment", "Block"],
                    "rows": [["CT-U", "1"], ["NT-U", "2"]],
                    "dtypes": ["string", "int"],
                    "n_rows": 2, "n_rows_stored": 2, "header_row": 0, "truncated": False,
                }],
            },
            mapping={
                "Treatment": {"canonical_field": "treatment", "source": "user"},
                "Block": {"canonical_field": "treatment", "source": "user"},
            },
        )
        body = self.client.get(reverse("workbench-mapping", args=[sub.public_token])).content.decode()
        cols = json.loads(re.search(r'id="columns-data"[^>]*>(.*?)</script>', body, re.S).group(1))
        by = {c["name"]: c["assigned"] for c in cols}
        self.assertEqual(by["Treatment"], "treatment")     # rightful owner keeps it
        self.assertEqual(by["Block"], "replication")        # bumped to its own suggestion
        self.assertNotEqual(by["Block"], by["Treatment"])   # never two columns on one field

    def test_derived_fields_not_mappable(self):
        # DOY, nitrogen type and management are derived by us — never mapping targets.
        from n2odb.services import canonical
        ids = {f.id for f in canonical.mappable_fields()}
        for fid in ("doy", "nitrogentype", "management"):
            self.assertNotIn(fid, ids)
            self.assertIsNotNone(canonical.get(fid))  # still in the registry for derivation

    def test_mapping_page_hides_derived_fields(self):
        body = self.client.get(reverse("workbench-mapping", args=[self.token])).content.decode()
        self.assertIn('data-field="n2oflux"', body)          # a real target is present
        for fid in ("doy", "nitrogentype", "management"):
            self.assertNotIn(f'data-field="{fid}"', body)

    def test_events_group_marked_optional(self):
        # Event fields are optional to map here — the UI must say they can come from the
        # paper or the Timeline step.
        body = self.client.get(reverse("workbench-mapping", args=[self.token])).content.decode()
        self.assertIn("Timeline", body)
        self.assertIn("field-group--optional", body)

    def test_save_mapping_rejects_derived_fields(self):
        resp = self.client.post(
            reverse("workbench-mapping-save", args=[self.token]),
            data={"mapping": {"date": "date", "n2o_flux": "n2oflux", "DOY": "doy"}},
            content_type="application/json",
        )
        self.assertEqual(resp.json()["mapped_count"], 2)  # doy rejected
        self.submission.refresh_from_db()
        self.assertNotIn("DOY", self.submission.mapping)

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


class MeasurementSheetTests(AuthedWorkbenchTestCase):
    def _two_sheet_sub(self, meas_first=True):
        temporal = {
            "name": "Temporal",
            "headers": ["DateTime", "Treatment", "N2O_flux"],
            "rows": [["2023-04-17", "CT-U", "1.2"], ["2023-04-18", "NT-U", "0.8"]],
            "dtypes": ["string", "string", "float"],
            "n_rows": 2, "n_rows_stored": 2, "header_row": 0, "truncated": False,
        }
        soil = {
            "name": "Soil",
            "headers": ["Treatment", "Sand_pct", "pH"],
            "rows": [["CT-U", "40", "6.4"], ["NT-U", "41", "6.5"]],
            "dtypes": ["string", "float", "float"],
            "n_rows": 2, "n_rows_stored": 2, "header_row": 0, "truncated": False,
        }
        sheets = [temporal, soil] if meas_first else [soil, temporal]
        return UploadSubmission.objects.create(
            parsed_content={"source": "file", "source_name": "x.xlsx", "sheets": sheets})

    def test_autodetect_picks_temporal_sheet_regardless_of_order(self):
        from n2odb.views_workbench import _measurement_sheet_index
        self.assertEqual(_measurement_sheet_index(self._two_sheet_sub(True)), 0)
        self.assertEqual(_measurement_sheet_index(self._two_sheet_sub(False)), 1)

    def test_switch_measurement_sheet_persists_and_clears_mapping(self):
        sub = self._two_sheet_sub(True)
        sub.mapping = {"DateTime": {"canonical_field": "date", "source": "user"}}
        sub.save(update_fields=["mapping"])
        resp = self.client.post(
            reverse("workbench-measurement-sheet", args=[sub.public_token]),
            data={"index": 1}, content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        sub.refresh_from_db()
        self.assertEqual(sub.structure["measurement_sheet"], 1)
        self.assertEqual(sub.mapping, {})   # old sheet's mapping doesn't carry over

    def test_switch_rejects_out_of_range(self):
        sub = self._two_sheet_sub(True)
        resp = self.client.post(
            reverse("workbench-measurement-sheet", args=[sub.public_token]),
            data={"index": 9}, content_type="application/json")
        self.assertEqual(resp.status_code, 400)


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
