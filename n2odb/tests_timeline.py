"""Tests for Stage 5 — timeline of dated events (global or per-treatment)."""
from __future__ import annotations

from django.test import TestCase
from n2odb.tests_common import AuthedWorkbenchTestCase
from django.urls import reverse

from n2odb.models_upload import UploadSubmission
from n2odb.services import event_fields
from n2odb.views_workbench import _paper_events, _timeline_treatments


_EVENTS = [
    {  # fertilisation → input_application
        "type": "input_application", "date_iso": "2018-05-01", "date_text": "1 May 2018",
        "fields": [
            {"key": "amount", "value": 135, "unit": "kg N/ha"},
            {"key": "input_form", "value": "urea"},
            {"key": "input_source", "value": "synthetic"},   # resolves to canonical casing
        ],
        "source_quote": "135 kg N ha-1 as urea", "confidence": 0.9,
    },
    {  # second fertilisation with an off-list type → type left blank
        "type": "input_application", "date_text": "mid-June",
        "fields": [{"key": "input_source", "value": "zzz not a category"}],
    },
    {"type": "planting", "date_iso": "2018-04-20",
     "fields": [{"key": "crop", "value": "maize"}]},
    {"type": "harvest", "date_iso": "2018-10-15", "fields": []},
    {"type": "tillage", "date_text": "spring",
     "fields": [{"key": "tillage_instrument", "value": "moldboard plough"}]},
    {"type": "irrigation", "fields": []},  # no timeline section -> ignored
]


def _submission(**overrides):
    data = dict(
        parsed_content={
            "source": "file", "source_name": "s.csv",
            "sheets": [{
                "name": "s.csv",
                "headers": ["date", "n2o_flux", "PlotID"],
                "rows": [["2018-05-01", "1.2", "NT"], ["2018-05-01", "2.1", "CT"]],
                "dtypes": ["date", "float", "string"],
                "n_rows": 2, "n_rows_stored": 2, "header_row": 0, "truncated": False,
            }],
        },
        mapping={
            "date": {"canonical_field": "date", "source": "user"},
            "PlotID": {"canonical_field": "treatment", "source": "user"},
        },
    )
    data.update(overrides)
    return UploadSubmission.objects.create(**data)


class EventRegistryTests(TestCase):
    def test_allowed_keys_include_paired_unit(self):
        keys = event_fields.allowed_keys("fertilisation")
        self.assertEqual(keys, {"date", "n_amount", "form", "type", "n_unit"})
        self.assertEqual(event_fields.allowed_keys("tillage"), {"date", "instrument"})

    def test_for_template_resolves_dropdown_options(self):
        tmpl = event_fields.for_template({"nitrogen_type": ["Synthetic", "Organic"]})
        fert = next(s for s in tmpl if s["id"] == "fertilisation")
        self.assertEqual(fert["contract"], "input_application")
        type_field = next(f for f in fert["fields"] if f["key"] == "type")
        self.assertEqual(type_field["options"], ["Synthetic", "Organic"])
        # A non-dropdown field carries an empty option list.
        date_field = next(f for f in fert["fields"] if f["key"] == "date")
        self.assertEqual(date_field["options"], [])


class PaperEventsTests(TestCase):
    def test_group_and_resolve(self):
        sub = _submission(manuscript_facts={"events": _EVENTS})
        opts = {"nitrogen_type": ["Synthetic", "Organic", "Mixed"]}
        grouped = _paper_events(sub, opts)

        self.assertEqual(len(grouped["fertilisation"]), 2)
        f0 = grouped["fertilisation"][0]
        self.assertEqual(f0["fields"]["date"], "2018-05-01")  # true date from date_iso
        self.assertEqual(f0["fields"]["n_amount"], 135)
        self.assertEqual(f0["fields"]["n_unit"], "kg N/ha")
        self.assertEqual(f0["fields"]["form"], "urea")
        self.assertEqual(f0["fields"]["type"], "Synthetic")   # 'synthetic' -> canonical
        self.assertEqual(f0["source"], "document")
        self.assertEqual(f0["date_hint"], "1 May 2018")       # as-stated, kept as a hint

        # No real date_iso: nothing goes in the date field; the vague timing is a hint.
        f1 = grouped["fertilisation"][1]
        self.assertNotIn("type", f1["fields"])
        self.assertNotIn("date", f1["fields"])
        self.assertEqual(f1["date_hint"], "mid-June")

        self.assertEqual(grouped["planting"][0]["fields"], {"date": "2018-04-20", "crop": "maize"})
        self.assertEqual(grouped["harvest"][0]["fields"], {"date": "2018-10-15"})
        # "spring" is not a date -> instrument only in fields, "spring" as the hint.
        self.assertEqual(grouped["tillage"][0]["fields"], {"instrument": "moldboard plough"})
        self.assertEqual(grouped["tillage"][0]["date_hint"], "spring")
        # irrigation has no section, so it never appears.
        self.assertNotIn("irrigation", grouped)


class TimelineTreatmentsTests(TestCase):
    def test_reuses_soil_treatments(self):
        sub = _submission(treatment_details={"treatments": ["A", "B", "C"]})
        self.assertEqual(_timeline_treatments(sub), ["A", "B", "C"])

    def test_falls_back_to_detected_column(self):
        sub = _submission()
        self.assertEqual(_timeline_treatments(sub), ["NT", "CT"])


class TimelineViewTests(AuthedWorkbenchTestCase):
    def setUp(self):
        super().setUp()
        self.sub = _submission(
            manuscript_facts={"events": _EVENTS},
            treatment_details={"treatments": ["NT", "CT"]},
        )
        self.token = self.sub.public_token

    def test_page_renders(self):
        resp = self.client.get(reverse("workbench-timeline", args=[self.token]))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Timeline of events", body)
        self.assertIn('id="types-data"', body)
        self.assertIn("moldboard plough", body)  # paper prefill reached the page
        # Progress rail: reached steps are links, later (unbuilt) steps are not.
        self.assertIn(reverse("workbench-site", args=[self.token]), body)
        self.assertIn("wizard-step__link", body)

    def test_page_without_data_redirects(self):
        empty = UploadSubmission.objects.create()
        resp = self.client.get(reverse("workbench-timeline", args=[empty.public_token]))
        self.assertEqual(resp.status_code, 302)

    def test_save_round_trip(self):
        resp = self.client.post(
            reverse("workbench-timeline-save", args=[self.token]),
            data={
                "scopes": {"fertilisation": "per_treatment", "planting": "global", "bogus": "x"},
                "global": {
                    "planting": [{"fields": {"date": "2018-04-20", "crop": "maize"}, "source": "document",
                                  "source_quote": "sown 20 Apr"}],
                    "harvest": [{"fields": {"date": "late July"}}],   # vague date, no hint -> dropped
                    "tillage": [{"fields": {"date": "spring"}, "date_hint": "spring"}],  # hint-only kept
                    "not_a_section": [{"fields": {"date": "2020-01-01"}}],  # dropped
                },
                "per_treatment": {
                    "NT": {"fertilisation": [{"fields": {"date": "2018-05-01", "n_amount": "100",
                                                         "junk": "drop"}, "source": "user",
                                              "date_hint": "early May"}]},
                    "Ghost": {"fertilisation": [{"fields": {"date": "2020-01-01"}}]},  # unknown treatment
                },
            },
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["event_count"], 3)  # planting + tillage(hint) + NT

        self.sub.refresh_from_db()
        ev = self.sub.events
        self.assertEqual(ev["scopes"], {"fertilisation": "per_treatment", "planting": "global"})
        # harvest's vague date is not a real date and has no hint -> the event vanishes.
        self.assertEqual(set(ev["global"].keys()), {"planting", "tillage"})
        self.assertEqual(ev["global"]["planting"][0]["source_quote"], "sown 20 Apr")
        # A hint-only event survives with empty fields but carries the manual-entry aid.
        till = ev["global"]["tillage"][0]
        self.assertEqual(till["fields"], {})
        self.assertEqual(till["date_hint"], "spring")
        # per-treatment: unknown key stripped, true date kept, hint passed through.
        nt = ev["per_treatment"]["NT"]["fertilisation"][0]
        self.assertEqual(nt["fields"], {"date": "2018-05-01", "n_amount": "100"})
        self.assertEqual(nt["date_hint"], "early May")
        self.assertNotIn("Ghost", ev["per_treatment"])


class ReviewTests(AuthedWorkbenchTestCase):
    def _review_submission(self, **overrides):
        data = dict(
            parsed_content={
                "source": "file", "source_name": "s.csv",
                "sheets": [{
                    "name": "s.csv",
                    "headers": ["date", "n2o_flux", "vwc", "PlotID"],
                    "rows": [
                        ["2018-05-01", "1.2", "40", "NT"],
                        ["2018-05-10", "2.1", "45", "NT"],
                        ["2018-05-01", "0.8", "38", "CT"],
                        ["late July", "9.9", "50", "NT"],  # unparseable date -> table only
                    ],
                    "dtypes": ["date", "float", "float", "string"],
                    "n_rows": 4, "n_rows_stored": 4, "header_row": 0, "truncated": False,
                }],
            },
            mapping={
                "date": {"canonical_field": "date", "source": "user"},
                "n2o_flux": {"canonical_field": "n2oflux", "unit": "g N/ha/d", "source": "user"},
                "vwc": {"canonical_field": "vwc", "source": "user"},
                "PlotID": {"canonical_field": "treatment", "source": "user"},
            },
            metadata_values={
                "pub_title": {"value": "A study", "source": "document"},
                "country": {"value": "USA", "source": "user"},
            },
            treatment_details={"treatments": ["NT", "CT"], "descriptions": {"NT": "No-till"}},
            events={
                "scopes": {"fertilisation": "global"},
                "global": {"fertilisation": [
                    {"fields": {"date": "2018-05-05", "n_amount": 135, "n_unit": "kg N/ha"},
                     "source": "document"}]},
                "per_treatment": {},
            },
        )
        data.update(overrides)
        return UploadSubmission.objects.create(**data)

    def test_build_review_daily_fill_and_grouping(self):
        from n2odb.views_workbench import _build_review
        sub = self._review_submission()
        data = _build_review(sub)

        self.assertEqual(data["treatments"], ["NT", "CT"])
        nt = data["perTreatment"]["NT"]
        # Scatter points come only from real measurements; "late July" (unparseable) drops.
        self.assertEqual(nt["series"]["n2oflux"], [[_ms("2018-05-01"), 1.2], [_ms("2018-05-10"), 2.1]])
        self.assertEqual(nt["series"]["vwc"], [[_ms("2018-05-01"), 40.0], [_ms("2018-05-10"), 45.0]])
        # Dates are daily-continuous: NT spans 05-01..05-10 = 10 rows; CT spans 05-01..05-05
        # (extended by the global fertilisation event on 05-05) = 5 rows.
        self.assertEqual(len(nt["rows"]), 10)
        self.assertEqual(len(data["perTreatment"]["CT"]["rows"]), 5)
        # Column set gains a derived DOY plus nitrogenapplied (merged event), canonical order.
        col_ids = [c["id"] for c in data["columns"]]
        self.assertEqual(col_ids, ["date", "n2oflux", "vwc", "doy", "nitrogenapplied"])

    def test_event_on_bare_date_becomes_a_row(self):
        from n2odb.views_workbench import _build_review
        data = _build_review(self._review_submission())
        cols = [c["id"] for c in data["columns"]]
        napp = cols.index("nitrogenapplied")
        nt_rows = data["perTreatment"]["NT"]["rows"]
        row = next(r for r in nt_rows if r[0] == "2018-05-05")
        self.assertEqual(row[napp], "135")  # a fertiliser on a measurement-less day is kept

    def test_doy_derived_and_nitrogen_zero_filled(self):
        from n2odb.views_workbench import _build_review
        data = _build_review(self._review_submission())
        cols = [c["id"] for c in data["columns"]]
        doy, napp = cols.index("doy"), cols.index("nitrogenapplied")
        nt_rows = data["perTreatment"]["NT"]["rows"]
        may1 = next(r for r in nt_rows if r[0] == "2018-05-01")
        self.assertEqual(may1[doy], "121")   # 2018-05-01 is day 121
        self.assertEqual(may1[napp], "0")    # no fertiliser that day ⇒ 0, not blank
        may5 = next(r for r in nt_rows if r[0] == "2018-05-05")
        self.assertEqual(may5[doy], "125")

    def test_units_converted_to_canonical(self):
        from n2odb.views_workbench import _build_review
        # Same data, but the flux column is declared in kg N/ha/d (×1000 to canonical).
        sub = self._review_submission(mapping={
            "date": {"canonical_field": "date", "source": "user"},
            "n2o_flux": {"canonical_field": "n2oflux", "unit": "kg N/ha/d", "source": "user"},
            "vwc": {"canonical_field": "vwc", "source": "user"},
            "PlotID": {"canonical_field": "treatment", "source": "user"},
        })
        nt = _build_review(sub)["perTreatment"]["NT"]
        self.assertEqual(nt["series"]["n2oflux"], [[_ms("2018-05-01"), 1200.0], [_ms("2018-05-10"), 2100.0]])

    def test_review_events_and_details(self):
        from n2odb.views_workbench import _build_review
        data = _build_review(self._review_submission())
        nt = data["perTreatment"]["NT"]
        self.assertEqual(nt["details"]["description"], "No-till")
        # Global fertilisation event reaches every treatment as a dated marker.
        ev = nt["events"][0]
        self.assertEqual(ev["kind"], "fert")
        self.assertEqual(ev["ms"], _ms("2018-05-05"))
        self.assertIn("135", ev["label"])

    def test_fert_marker_drops_per_year_unit(self):
        from n2odb.views_workbench import _event_marker
        label, kind = _event_marker(
            "fertilisation", {"n_amount": 137, "n_unit": "kg N ha⁻¹ year⁻¹"})
        self.assertEqual(kind, "fert")
        self.assertEqual(label, "Fertiliser 137 kg N ha⁻¹")  # year part stripped

    def test_treatment_fields_fully_represented(self):
        from n2odb.views_workbench import _build_review
        from n2odb.services import site_fields
        # Soil is study-wide (global); Crop & management varies by treatment.
        sub = self._review_submission(treatment_details={
            "treatments": ["NT", "CT"],
            "scopes": {"Soil": "global", "Crop & management": "per_treatment"},
            "global": {"ph": {"value": 6.5, "source": "user"}},
            "per_treatment": {"NT": {"primary_crop": {"value": "maize", "source": "user"}}},
        })
        data = _build_review(sub)
        # A global group is folded into the study-wide panel once, tagged "all treatments",
        # with EVERY soil field.
        soil = next(g for g in data["global"] if g["name"] == "Soil — all treatments")
        self.assertEqual(
            {i["label"] for i in soil["items"]},
            {f.label for f in site_fields.treatment_fields() if f.group == "Soil"})
        self.assertEqual(next(i for i in soil["items"] if i["label"] == "Soil pH")["value"], 6.5)
        # A per-treatment group shows under each treatment (not in the study panel).
        self.assertNotIn("Crop & management", [g["name"] for g in data["global"]])
        crop = next(g for g in data["perTreatment"]["NT"]["details"]["groups"]
                    if g["name"] == "Crop & management")
        self.assertEqual(next(i for i in crop["items"] if i["label"] == "Primary crop")["value"], "maize")
        # Unfilled fields are present but flagged empty (so gaps are visible).
        self.assertTrue(any(i["empty"] for i in crop["items"]))

    def test_global_panel(self):
        from n2odb.views_workbench import _build_review
        data = _build_review(self._review_submission())
        pub = next(g for g in data["global"] if g["name"] == "Publication")
        title = next(i for i in pub["items"] if i["label"] == "Title")
        self.assertEqual(title["value"], "A study")
        self.assertEqual(title["source"], "document")

    def test_global_panel_falls_back_to_document_facts(self):
        # Publication fields extracted from the paper but never re-saved on the Site
        # step must still show on review (merged view), not appear blank.
        from n2odb.views_workbench import _build_review
        sub = self._review_submission(
            metadata_values={"site_name": {"value": "KBS", "source": "user"}},
            manuscript_facts={"metadata": {
                "pub_title": {"value": "N2O from maize", "source_quote": "..."},
            }},
        )
        data = _build_review(sub)
        pub = next(g for g in data["global"] if g["name"] == "Publication")
        title = next(i for i in pub["items"] if i["label"] == "Title")
        self.assertEqual(title["value"], "N2O from maize")
        self.assertFalse(title["empty"])
        # A field neither saved nor extracted stays flagged empty.
        doi = next(i for i in pub["items"] if i["label"] == "DOI")
        self.assertTrue(doi["empty"])

    def test_global_panel_derives_link_from_doi(self):
        # A DOI implies a link, so the review panel never shows a bare "—" for Link
        # while a DOI is known — even when no explicit Link was captured.
        from n2odb.views_workbench import _build_review
        sub = self._review_submission(manuscript_facts={"metadata": {
            "pub_doi": {"value": "10.1029/2019JG005032", "source": "regex"}}})
        data = _build_review(sub)
        pub = next(g for g in data["global"] if g["name"] == "Publication")
        link = next(i for i in pub["items"] if i["label"] == "Link")
        self.assertFalse(link["empty"])
        self.assertEqual(link["value"], "https://doi.org/10.1029/2019jg005032")

    def test_page_renders(self):
        sub = self._review_submission()
        resp = self.client.get(reverse("workbench-review", args=[sub.public_token]))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('id="review-data"', body)
        self.assertIn("Review your submission", body)
        self.assertIn("No-till", body)

    def test_page_without_data_redirects(self):
        empty = UploadSubmission.objects.create()
        resp = self.client.get(reverse("workbench-review", args=[empty.public_token]))
        self.assertEqual(resp.status_code, 302)


class SubmitTests(AuthedWorkbenchTestCase):
    def _mk(self, **overrides):
        data = dict(
            parsed_content={
                "source": "file", "source_name": "s.csv",
                "sheets": [{
                    "name": "s.csv",
                    "headers": ["date", "n2o_flux", "PlotID"],
                    "rows": [["2018-05-01", "1.2", "NT"], ["2018-05-02", "2.1", "CT"]],
                    "dtypes": ["date", "float", "string"],
                    "n_rows": 2, "n_rows_stored": 2, "header_row": 0, "truncated": False,
                }],
            },
            mapping={
                "date": {"canonical_field": "date", "source": "user"},
                "n2o_flux": {"canonical_field": "n2oflux", "unit": "g N/ha/d", "source": "user"},
                "PlotID": {"canonical_field": "treatment", "source": "user"},
            },
            metadata_values={
                "site_latitude": {"value": 42.4, "source": "user"},
                "site_longitude": {"value": -85.4, "source": "user"},
            },
            treatment_details={
                "treatments": ["NT", "CT"],
                "global": {"primary_crop": {"value": "maize", "source": "user"}},
            },
        )
        data.setdefault("owner", self.user)  # submissions are owned; email binds to it
        data.update(overrides)
        return UploadSubmission.objects.create(**data)

    def test_readiness_and_summary(self):
        from n2odb.views_workbench import _submit_readiness, _submit_summary
        sub = self._mk()
        self.assertTrue(all(c["ok"] for c in _submit_readiness(sub)))
        summary = _submit_summary(sub)
        self.assertEqual(summary["treatments"], 2)
        self.assertGreater(summary["rows"], 0)

    def test_readiness_flags_missing_required_mapping(self):
        from n2odb.views_workbench import _submit_readiness
        # Drop the flux mapping → the required-columns check fails.
        sub = self._mk(mapping={
            "date": {"canonical_field": "date", "source": "user"},
            "PlotID": {"canonical_field": "treatment", "source": "user"},
        })
        req = next(c for c in _submit_readiness(sub) if "Required columns" in c["label"])
        self.assertFalse(req["ok"])

    def test_missing_metadata_warns_but_does_not_block(self):
        # Citation/Link/Abstract gaps are surfaced as warnings, never as blocking checks.
        from n2odb.views_workbench import _metadata_warnings, _submit_readiness
        sub = self._mk()
        self.assertEqual(sorted(_metadata_warnings(sub)), ["Abstract", "Citation", "Link"])
        self.assertTrue(all(c["ok"] for c in _submit_readiness(sub)))

    def test_submit_page_shows_the_missing_details_notice(self):
        sub = self._mk()
        resp = self.client.get(reverse("workbench-submit", args=[sub.public_token]))
        self.assertContains(resp, "Missing study details")
        # The citation gap carries the licence rationale: reuse requires citing it.
        self.assertContains(resp, "CC-BY")

    def test_submit_warning_lists_each_missing_fact_once(self):
        # One consolidated warning, and no missing fact named twice.
        sub = self._mk()
        resp = self.client.get(reverse("workbench-submit", args=[sub.public_token]))
        self.assertContains(resp, "Missing study details", count=1)
        self.assertContains(resp, "Citation, Link, Abstract", count=1)

    def test_review_page_uses_per_field_markers_not_a_banner(self):
        # The review page flags each gap in its own row; it must not also show the
        # submit-page banner, which would double up the same warnings.
        sub = self._mk()
        resp = self.client.get(reverse("workbench-review", args=[sub.public_token]))
        self.assertNotContains(resp, "Missing study details")

    def test_readiness_flags_missing_coordinates(self):
        from n2odb.views_workbench import _submit_readiness
        sub = self._mk(metadata_values={})  # no lat/long
        coord = next(c for c in _submit_readiness(sub) if "coordinates" in c["label"])
        self.assertFalse(coord["ok"])

    def test_readiness_flags_blank_treatment_rows(self):
        from n2odb.views_workbench import _submit_readiness
        sub = self._mk(parsed_content={
            "source": "file", "source_name": "s.csv",
            "sheets": [{
                "name": "s.csv",
                "headers": ["date", "n2o_flux", "PlotID"],
                "rows": [["2018-05-01", "1.2", "NT"], ["2018-05-02", "2.1", ""]],  # blank trt
                "dtypes": ["date", "float", "string"],
                "n_rows": 2, "n_rows_stored": 2, "header_row": 0, "truncated": False,
            }],
        })
        chk = next(c for c in _submit_readiness(sub) if "every row has a treatment" in c["label"].lower())
        self.assertFalse(chk["ok"])
        self.assertIn("1 row", chk["detail"])

    def test_readiness_flags_orphaned_treatment_details(self):
        from n2odb.views_workbench import _submit_readiness
        # Details keyed to a treatment not present in the data (e.g. after remapping).
        sub = self._mk(treatment_details={
            "treatments": ["NT", "CT"],
            "global": {"primary_crop": {"value": "maize", "source": "user"}},
            "per_treatment": {"OLD-NAME": {"tillage": {"value": "x", "source": "user"}}},
        })
        chk = next(c for c in _submit_readiness(sub) if "match the data" in c["label"].lower())
        self.assertFalse(chk["ok"])
        self.assertIn("OLD-NAME", chk["detail"])

    def test_readiness_flags_out_of_range_coordinates(self):
        from n2odb.views_workbench import _submit_readiness
        sub = self._mk(metadata_values={
            "site_latitude": {"value": 999, "source": "user"},     # impossible
            "site_longitude": {"value": -85.4, "source": "user"},
        })
        chk = next(c for c in _submit_readiness(sub) if "coordinates valid" in c["label"].lower())
        self.assertFalse(chk["ok"])

    def test_readiness_flags_missing_unit(self):
        from n2odb.views_workbench import _submit_readiness
        sub = self._mk(mapping={
            "date": {"canonical_field": "date", "source": "user"},
            "n2o_flux": {"canonical_field": "n2oflux", "source": "user"},  # no unit
            "PlotID": {"canonical_field": "treatment", "source": "user"},
        })
        chk = next(c for c in _submit_readiness(sub) if "has a unit" in c["label"].lower())
        self.assertFalse(chk["ok"])
        self.assertIn("n2o_flux", chk["detail"])

    def test_per_treatment_field_ignores_global_paper_value(self):
        from n2odb.views_workbench import _resolved_treatment_value
        # Crop is per-treatment; the paper's single crop must NOT be stamped on each.
        sub = self._mk(
            treatment_details={"treatments": ["NT", "CT"],
                               "scopes": {"Crop & management": "per_treatment"}},
            manuscript_facts={"metadata": {"primary_crop": {"value": "maize"}}},
        )
        self.assertIsNone(_resolved_treatment_value(sub, "NT", "primary_crop"))
        # But a global-scope field still inherits the paper value.
        sub2 = self._mk(
            treatment_details={"treatments": ["NT", "CT"]},  # default global
            manuscript_facts={"metadata": {"primary_crop": {"value": "maize"}}},
        )
        self.assertEqual(_resolved_treatment_value(sub2, "NT", "primary_crop"), "maize")

    def test_treatment_identifier_is_mandatory(self):
        from n2odb.views_workbench import _submit_readiness
        # Drop the treatment mapping → the required-columns check fails.
        sub = self._mk(mapping={
            "date": {"canonical_field": "date", "source": "user"},
            "n2o_flux": {"canonical_field": "n2oflux", "unit": "g N/ha/d", "source": "user"},
        })
        req = next(c for c in _submit_readiness(sub) if "Required columns" in c["label"])
        self.assertFalse(req["ok"])
        self.assertIn("Treatment identifier", req["detail"])

    def test_replication_column_is_ignored_by_pipeline(self):
        # A mapped 'replication' column is captured but not yet wired in — it must not
        # appear as a data column in the normalized series (see workbench-replication-todo).
        from n2odb.views_workbench import _normalized_treatments
        sub = self._mk(
            parsed_content={
                "source": "file", "source_name": "s.csv",
                "sheets": [{
                    "name": "s.csv",
                    "headers": ["date", "n2o_flux", "PlotID", "rep"],
                    "rows": [["2018-05-01", "1.2", "NT", "1"], ["2018-05-02", "2.1", "CT", "2"]],
                    "dtypes": ["date", "float", "string", "string"],
                    "n_rows": 2, "n_rows_stored": 2, "header_row": 0, "truncated": False,
                }],
            },
            mapping={
                "date": {"canonical_field": "date", "source": "user"},
                "n2o_flux": {"canonical_field": "n2oflux", "unit": "g N/ha/d", "source": "user"},
                "PlotID": {"canonical_field": "treatment", "source": "user"},
                "rep": {"canonical_field": "replication", "source": "user"},
            },
        )
        col_ids = [c["id"] for c in _normalized_treatments(sub)["columns"]]
        self.assertNotIn("replication", col_ids)

    def test_readiness_flags_missing_primary_crop(self):
        from n2odb.views_workbench import _submit_readiness
        sub = self._mk(treatment_details={"treatments": ["NT", "CT"]})  # no primary_crop
        crop = next(c for c in _submit_readiness(sub) if "Primary crop" in c["label"])
        self.assertFalse(crop["ok"])

    def test_readiness_accepts_primary_crop_from_document(self):
        # Prefilled from the paper and accepted as-is (never re-saved to treatment_details)
        # still counts — the resolver falls back to manuscript_facts.
        from n2odb.views_workbench import _submit_readiness
        sub = self._mk(
            treatment_details={"treatments": ["NT", "CT"]},
            manuscript_facts={"metadata": {"primary_crop": {"value": "maize"}}},
        )
        crop = next(c for c in _submit_readiness(sub) if "Primary crop" in c["label"])
        self.assertTrue(crop["ok"])

    def test_page_renders_form(self):
        sub = self._mk()
        resp = self.client.get(reverse("workbench-submit", args=[sub.public_token]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Submit for review", resp.content.decode())

    def test_finalize_success(self):
        sub = self._mk()
        resp = self.client.post(
            reverse("workbench-submit-finalize", args=[sub.public_token]),
            data={"name": "Dr Ada", "email": "spoofed@evil.example", "message": "thanks"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        sub.refresh_from_db()
        self.assertEqual(sub.status, "submitted")
        self.assertIsNotNone(sub.submitted_at)
        self.assertEqual(sub.contributor_name, "Dr Ada")
        # Email is bound to the verified account, never the (spoofable) request body.
        self.assertEqual(sub.contributor_email, self.user.email)
        self.assertEqual(sub.review_notes[0]["note"], "thanks")

    def test_finalize_requires_name(self):
        # Name is still contributor-supplied and required; email comes from the account.
        sub = self._mk()
        resp = self.client.post(
            reverse("workbench-submit-finalize", args=[sub.public_token]),
            data={"name": ""},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("name", resp.json()["fields"])
        sub.refresh_from_db()
        self.assertEqual(sub.status, "draft")  # nothing finalized

    def test_finalize_blocked_when_not_ready(self):
        sub = self._mk(mapping={"PlotID": {"canonical_field": "treatment", "source": "user"}})
        resp = self.client.post(
            reverse("workbench-submit-finalize", args=[sub.public_token]),
            data={"name": "Ada", "email": "ada@example.org"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("checks", resp.json())

    def test_finalize_is_idempotent(self):
        sub = self._mk(status="submitted")
        resp = self.client.post(
            reverse("workbench-submit-finalize", args=[sub.public_token]),
            data={"name": "Ada", "email": "ada@example.org"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)  # already sent

    def test_detail_shows_contributor_trust(self):
        # SubmitTests is authed as staff via the mixin, so it can hit the curation page.
        sub = self._mk(contributor_email="ada@harvard.edu")
        detail = self.client.get(reverse("workbench-curation-detail", args=[sub.public_token]))
        self.assertContains(detail, "Academic / institutional")

    def test_submitted_state_shows_confirmation(self):
        sub = self._mk(status="submitted", contributor_email="ada@example.org")
        resp = self.client.get(reverse("workbench-submit", args=[sub.public_token]))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Submission received", body)
        self.assertIn("ada@example.org", body)


class PromoteTests(AuthedWorkbenchTestCase):
    def _mk(self, **overrides):
        data = dict(
            parsed_content={
                "source": "file", "source_name": "s.csv",
                "sheets": [{
                    "name": "s.csv",
                    "headers": ["date", "n2o_flux", "PlotID"],
                    "rows": [["2018-05-01", "1.2", "NT"], ["2018-05-02", "2.1", "CT"]],
                    "dtypes": ["date", "float", "string"],
                    "n_rows": 2, "n_rows_stored": 2, "header_row": 0, "truncated": False,
                }],
            },
            mapping={
                "date": {"canonical_field": "date", "source": "user"},
                "n2o_flux": {"canonical_field": "n2oflux", "unit": "g N/ha/d", "source": "user"},
                "PlotID": {"canonical_field": "treatment", "source": "user"},
            },
            metadata_values={
                "site_latitude": {"value": 42.4, "source": "user"},
                "site_longitude": {"value": -85.4, "source": "user"},
                "pub_title": {"value": "N2O from maize", "source": "user"},
                "pub_doi": {"value": "10.1234/abcd", "source": "user"},
            },
            treatment_details={
                "treatments": ["NT", "CT"],
                "global": {"primary_crop": {"value": "maize", "source": "user"}},
            },
            status="approved",
        )
        data.update(overrides)
        return UploadSubmission.objects.create(**data)

    def test_dry_run_no_writes(self):
        from n2odb.services import promote
        from n2odb.models import Publication
        before = Publication.objects.count()
        plan = promote.dry_run(self._mk())
        self.assertEqual(plan["blocking"], [])
        self.assertEqual(plan["counts"]["treatments"], 2)
        self.assertTrue(plan["promotable"])
        self.assertEqual(Publication.objects.count(), before)  # nothing written

    def test_promote_creates_linked_rows(self):
        from n2odb.services import promote
        from n2odb.models import (Publication, Site, Experiment, Treatment,
                                  RawMeasurementTreatment)
        sub = self._mk()
        result = promote.promote(sub)

        pub = Publication.objects.get(pk=result["pubid"])
        site = Site.objects.get(pk=result["siteid"])
        exp = Experiment.objects.get(pk=result["experimentid"])
        # FK linkage is correct end-to-end.
        self.assertEqual(exp.PubID_id, pub.pk)
        self.assertEqual(exp.SiteID_id, site.pk)
        self.assertEqual(pub.Link, "https://doi.org/10.1234/abcd")  # DOI became the link
        self.assertEqual(exp.ExperimentName, "N2O from maize")
        self.assertEqual(len(result["treatmentids"]), 2)
        for tid in result["treatmentids"]:
            trt = Treatment.objects.get(pk=tid)
            self.assertEqual(trt.ExperimentID_id, exp.pk)
            self.assertEqual(trt.PrimaryCrop, "maize")
            self.assertTrue(RawMeasurementTreatment.objects
                            .filter(treatmentid=trt).exists())
        sub.refresh_from_db()
        self.assertEqual(sub.status, "promoted")
        self.assertEqual(sub.promotion["raw_count"], result["raw_count"])

    def test_promote_derives_continent_and_country_from_coords(self):
        # (42.4, -85.4) is Michigan, USA — offline reverse-geocode should fill
        # Continent and (since the submission gives no country) Country too.
        from n2odb.services import promote
        from n2odb.models import Site
        result = promote.promote(self._mk())
        site = Site.objects.get(pk=result["siteid"])
        self.assertEqual(site.Continent, "North America")
        self.assertEqual(site.Country, "United States")

    def test_contributor_country_wins_over_coords(self):
        # A contributor-supplied country is not overwritten by the coord-derived one.
        from n2odb.services import promote
        from n2odb.models import Site
        sub = self._mk()
        md = sub.metadata_values
        md["country"] = {"value": "Canada", "source": "user"}
        sub.metadata_values = md
        sub.save(update_fields=["metadata_values"])
        result = promote.promote(sub)
        site = Site.objects.get(pk=result["siteid"])
        self.assertEqual(site.Country, "Canada")
        self.assertEqual(site.Continent, "North America")  # still derived

    def test_promote_uses_readable_name_for_treatmentname(self):
        # Identifier keys the rows; the optional readable name becomes TreatmentName.
        from n2odb.services import promote
        from n2odb.models import Treatment
        sub = self._mk(treatment_details={
            "treatments": ["NT", "CT"],
            "names": {"NT": "No-till + urea"},   # CT left unnamed → falls back to identifier
            "global": {"primary_crop": {"value": "maize", "source": "user"}},
        })
        result = promote.promote(sub)
        names = sorted(Treatment.objects.filter(pk__in=result["treatmentids"])
                       .values_list("TreatmentName", flat=True))
        self.assertEqual(names, ["CT", "No-till + urea"])

    def test_promote_is_idempotent(self):
        from n2odb.services import promote
        sub = self._mk()
        promote.promote(sub)
        with self.assertRaises(promote.PromotionError):
            promote.promote(sub)  # second attempt refused

    def test_promote_stamps_contributor_public_id(self):
        # The submitter's persistent id lands on the Publication so all their promoted
        # datasets are findable by that one handle.
        from django.contrib.auth import get_user_model
        from n2odb.services import promote
        from n2odb.models import Publication
        contributor = get_user_model().objects.create_user(
            "contrib", email="contrib@university.edu", password="pw")
        sub = self._mk(owner=contributor)
        result = promote.promote(sub)

        pub = Publication.objects.get(pk=result["pubid"])
        self.assertEqual(pub.Contributor, contributor.public_id)
        # The tracking query the field exists for:
        self.assertIn(pub, Publication.objects.filter(Contributor=contributor.public_id))

    def test_promote_without_owner_leaves_contributor_null(self):
        from n2odb.services import promote
        from n2odb.models import Publication
        result = promote.promote(self._mk())  # _mk creates no owner
        self.assertIsNone(Publication.objects.get(pk=result["pubid"]).Contributor)

    def test_promote_refuses_draft(self):
        from n2odb.services import promote
        sub = self._mk(status="draft")
        with self.assertRaises(promote.PromotionError):
            promote.promote(sub)

    def test_promote_blocks_without_coordinates(self):
        from n2odb.services import promote
        sub = self._mk(metadata_values={"pub_title": {"value": "x", "source": "user"}})
        with self.assertRaises(promote.PromotionError):
            promote.promote(sub)

    def test_promote_blocks_out_of_range_coordinates(self):
        from n2odb.services import promote
        sub = self._mk(metadata_values={
            "site_latitude": {"value": 200, "source": "user"},    # out of range
            "site_longitude": {"value": -85.4, "source": "user"},
        })
        with self.assertRaises(promote.PromotionError):
            promote.promote(sub)

    def test_doi_helpers(self):
        from n2odb.services import promote
        from n2odb.models import Publication
        self.assertEqual(promote.normalize_doi("https://doi.org/10.1/AB"), "10.1/ab")
        self.assertEqual(promote.normalize_doi("doi:10.2/cd/"), "10.2/cd")
        Publication.objects.create(PubTitle="Prior", Link="https://doi.org/10.1234/abcd")
        dups = promote.find_duplicate_publications("10.1234/ABCD")
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0]["title"], "Prior")

    def test_submit_page_warns_on_duplicate_doi(self):
        from n2odb.models import Publication
        Publication.objects.create(PubTitle="Prior work", Link="https://doi.org/10.1234/abcd")
        sub = self._mk(status="draft")
        resp = self.client.get(reverse("workbench-submit", args=[sub.public_token]))
        self.assertContains(resp, "already in the database")
        self.assertContains(resp, "Prior work")


class CurationTests(TestCase):
    def _mk(self, status="submitted", **overrides):
        data = dict(
            status=status,
            contributor_name="Dr Ada", contributor_email="ada@example.org",
            parsed_content={
                "source": "file", "source_name": "s.csv",
                "sheets": [{
                    "name": "s.csv",
                    "headers": ["date", "n2o_flux", "PlotID"],
                    "rows": [["2018-05-01", "1.2", "NT"], ["2018-05-02", "2.1", "CT"]],
                    "dtypes": ["date", "float", "string"],
                    "n_rows": 2, "n_rows_stored": 2, "header_row": 0, "truncated": False,
                }],
            },
            mapping={
                "date": {"canonical_field": "date", "source": "user"},
                "n2o_flux": {"canonical_field": "n2oflux", "unit": "g N/ha/d", "source": "user"},
                "PlotID": {"canonical_field": "treatment", "source": "user"},
            },
            metadata_values={
                "site_latitude": {"value": 42.4, "source": "user"},
                "site_longitude": {"value": -85.4, "source": "user"},
                "pub_title": {"value": "N2O from maize", "source": "user"},
            },
            treatment_details={
                "treatments": ["NT", "CT"],
                "global": {"primary_crop": {"value": "maize", "source": "user"}},
            },
        )
        data.update(overrides)
        return UploadSubmission.objects.create(**data)

    def _login_staff(self):
        from django.contrib.auth import get_user_model
        u = get_user_model().objects.create_user("curator", password="x", is_staff=True)
        self.client.force_login(u)
        return u

    def test_list_flags_and_filters_missing_metadata(self):
        self._login_staff()
        sub = self._mk()  # no Link/Abstract
        lst = self.client.get(reverse("workbench-curation"))
        self.assertContains(lst, "⚠ meta")
        filtered = self.client.get(reverse("workbench-curation") + "?missing=1")
        self.assertContains(filtered, str(sub.public_token))

    def test_missing_filter_hides_complete_records(self):
        self._login_staff()
        complete = self._mk(metadata_values={
            "pub_link": {"value": "https://doi.org/10.1234/abc", "source": "user"},
            "pub_abstract": {"value": "An abstract.", "source": "user"},
            "pub_citation": {"value": "Smith, J. (2019). A paper.", "source": "user"},
        })
        filtered = self.client.get(reverse("workbench-curation") + "?missing=1")
        self.assertNotContains(filtered, str(complete.public_token))

    def test_enrich_action_fills_the_abstract_and_citation(self):
        from unittest import mock
        from n2odb.services import doi_enrich
        self._login_staff()
        sub = self._mk(metadata_values={"pub_doi": {"value": "10.1234/abc", "source": "user"}})
        cite = "Smith, J. (2019). A paper. Journal, 1, 2. https://doi.org/10.1234/abc"
        with mock.patch.object(doi_enrich, "fetch_abstract", return_value="Fetched abstract."), \
             mock.patch.object(doi_enrich, "fetch_citation", return_value=(cite, "crossref")):
            self.client.post(
                reverse("workbench-curation-action", args=[sub.public_token]),
                data={"action": "enrich"},
            )
        sub.refresh_from_db()
        self.assertEqual(sub.metadata_values["pub_abstract"]["value"], "Fetched abstract.")
        self.assertEqual(sub.metadata_values["pub_citation"]["value"], cite)

    def test_requires_staff(self):
        sub = self._mk()
        # Anonymous is bounced (redirect to the admin login).
        self.assertEqual(self.client.get(reverse("workbench-curation")).status_code, 302)
        self.assertEqual(
            self.client.get(reverse("workbench-curation-detail", args=[sub.public_token])).status_code, 302)

    def test_list_and_detail_render(self):
        self._login_staff()
        sub = self._mk()
        lst = self.client.get(reverse("workbench-curation"))
        self.assertContains(lst, "N2O from maize")
        detail = self.client.get(reverse("workbench-curation-detail", args=[sub.public_token]))
        self.assertContains(detail, 'id="review-data"')   # graph/table panel embedded inline
        self.assertContains(detail, "Promotion")
        self.assertContains(detail, "Promote to GRAND")

    def test_approve_action(self):
        self._login_staff()
        sub = self._mk()
        resp = self.client.post(
            reverse("workbench-curation-action", args=[sub.public_token]),
            data={"action": "approve", "note": "looks good"})
        self.assertEqual(resp.status_code, 302)
        sub.refresh_from_db()
        self.assertEqual(sub.status, "approved")
        self.assertEqual(sub.review_notes[0]["author"], "curator")

    def test_promote_action_writes(self):
        from n2odb.models import Publication
        self._login_staff()
        sub = self._mk(status="approved")
        before = Publication.objects.count()
        self.client.post(reverse("workbench-curation-action", args=[sub.public_token]),
                         data={"action": "promote"})
        sub.refresh_from_db()
        self.assertEqual(sub.status, "promoted")
        self.assertEqual(Publication.objects.count(), before + 1)

    def test_promote_action_blocked_leaves_db_untouched(self):
        from n2odb.models import Publication
        self._login_staff()
        # Missing coordinates → promotion is refused; nothing written, status unchanged.
        sub = self._mk(status="approved",
                       metadata_values={"pub_title": {"value": "x", "source": "user"}})
        before = Publication.objects.count()
        self.client.post(reverse("workbench-curation-action", args=[sub.public_token]),
                         data={"action": "promote"})
        sub.refresh_from_db()
        self.assertEqual(sub.status, "approved")
        self.assertEqual(Publication.objects.count(), before)

    def test_request_changes_emails_contributor_with_resume_link(self):
        from django.core import mail
        self._login_staff()
        sub = self._mk()
        self.client.post(reverse("workbench-curation-action", args=[sub.public_token]),
                         data={"action": "request_changes", "note": "Please add MAT."})
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["ada@example.org"])
        self.assertIn("Please add MAT.", msg.body)
        self.assertIn(str(sub.public_token), msg.body)   # a resume link back into the wizard
        sub.refresh_from_db()
        self.assertEqual(sub.status, "changes_requested")

    def test_request_changes_requires_a_note(self):
        from django.core import mail
        self._login_staff()
        sub = self._mk()
        self.client.post(reverse("workbench-curation-action", args=[sub.public_token]),
                         data={"action": "request_changes", "note": "   "})  # blank
        sub.refresh_from_db()
        self.assertEqual(sub.status, "submitted")   # not changed
        self.assertEqual(len(mail.outbox), 0)       # nothing emailed

    def test_reject_emails_contributor(self):
        from django.core import mail
        self._login_staff()
        sub = self._mk()
        self.client.post(reverse("workbench-curation-action", args=[sub.public_token]),
                         data={"action": "reject", "note": "Out of scope."})
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("not accepted", mail.outbox[0].body)

    def test_promote_emails_contributor(self):
        from django.core import mail
        self._login_staff()
        sub = self._mk(status="approved")
        self.client.post(reverse("workbench-curation-action", args=[sub.public_token]),
                         data={"action": "promote"})
        self.assertTrue(any("published" in m.body for m in mail.outbox))


class ConfirmOnContinueTests(AuthedWorkbenchTestCase):
    """Continue confirms accepted paper prefills: the client sends document-sourced
    values, and the save endpoints must persist them (with provenance intact)."""

    def test_site_save_persists_document_value(self):
        sub = UploadSubmission.objects.create()
        self.client.post(
            reverse("workbench-site-save", args=[sub.public_token]),
            data={"values": {"pub_title": {"value": "Paper X", "source": "document"}}},
            content_type="application/json")
        sub.refresh_from_db()
        self.assertEqual(sub.metadata_values["pub_title"]["value"], "Paper X")
        self.assertEqual(sub.metadata_values["pub_title"]["source"], "document")

    def test_soil_save_persists_document_value(self):
        sub = UploadSubmission.objects.create()
        self.client.post(
            reverse("workbench-soil-save", args=[sub.public_token]),
            data={"treatments": ["NT"], "scopes": {"Crop & management": "global"},
                  "global": {"primary_crop": {"value": "maize", "source": "document"}},
                  "per_treatment": {}},
            content_type="application/json")
        sub.refresh_from_db()
        pc = sub.treatment_details["global"]["primary_crop"]
        self.assertEqual(pc["value"], "maize")
        self.assertEqual(pc["source"], "document")

    def test_initial_values_tag_saved_answers(self):
        from n2odb.views_workbench import _initial_site_values
        from n2odb.services import site_fields
        sub = UploadSubmission.objects.create(
            metadata_values={"pub_title": {"value": "Saved", "source": "user"}},
            manuscript_facts={"metadata": {"country": {"value": "USA"}}},
        )
        vals = _initial_site_values(sub, site_fields.global_fields())
        self.assertTrue(vals["pub_title"].get("_saved"))   # persisted → confirmed
        self.assertNotIn("_saved", vals["country"])         # paper-only → unconfirmed


class EmailTrustTests(TestCase):
    def test_classification(self):
        from n2odb.views_workbench import _email_trust
        self.assertEqual(_email_trust("ada@harvard.edu")["kind"], "academic")
        self.assertEqual(_email_trust("j@ox.ac.uk")["kind"], "academic")
        self.assertEqual(_email_trust("x@uni.edu.au")["kind"], "academic")
        self.assertEqual(_email_trust("someone@gmail.com")["kind"], "personal")
        self.assertEqual(_email_trust("ceo@agcorp.com")["kind"], "other")
        self.assertEqual(_email_trust("ada@harvard.edu")["domain"], "harvard.edu")
        self.assertIsNone(_email_trust(""))
        self.assertIsNone(_email_trust("not-an-email"))


class UnitsConvertTests(TestCase):
    def test_linear_and_affine(self):
        from n2odb.services import units
        self.assertEqual(units.convert("n2oflux", 1, "kg N/ha/d"), 1000)   # kg → g
        self.assertEqual(units.convert("nitrogenapplied", 1, "g N/m²"), 10)
        self.assertEqual(units.convert("precip", 1, "cm"), 10)
        # areal + per-second rates: 1 ng N/m²/s = 0.864 g N/ha/d (not 0.000864).
        self.assertAlmostEqual(units.convert("n2oflux", 1, "ng N/m²/s"), 0.864)
        self.assertAlmostEqual(units.convert("n2oflux", 1, "mg N/m²/h"), 240)
        self.assertAlmostEqual(units.convert("soilt", 32, "°F"), 0.0)
        self.assertAlmostEqual(units.convert("soilt", 212, "°F"), 100.0)
        self.assertAlmostEqual(units.convert("soilt", 273.15, "K"), 0.0)

    def test_noop_and_incompatible_pass_through(self):
        from n2odb.services import units
        self.assertEqual(units.convert("soilt", 20, "°C"), 20)      # already canonical
        self.assertEqual(units.convert("vwc", 30, "%"), 30)         # field has no unit concept
        self.assertEqual(units.convert("soilnh4", 5, "kg N/ha"), 5)  # dimensionally incompatible
        self.assertIsNone(units.convert("n2oflux", None, "kg N/ha/d"))


def _ms(iso: str) -> int:
    import datetime as _dt
    d = _dt.datetime.fromisoformat(iso).replace(tzinfo=_dt.timezone.utc)
    return int(d.timestamp() * 1000)


class IsoDateTests(TestCase):
    def test_valid_and_invalid(self):
        from n2odb.views_workbench import _iso_date
        self.assertEqual(_iso_date("2018-05-01"), "2018-05-01")
        self.assertEqual(_iso_date("2018-05-01T00:00:00"), "2018-05-01")  # trailing time tolerated
        self.assertIsNone(_iso_date("late July"))
        self.assertIsNone(_iso_date("2018-07"))   # month only is not a full date
        self.assertIsNone(_iso_date(""))
        self.assertIsNone(_iso_date(None))


class ReplicationAveragingTests(AuthedWorkbenchTestCase):
    """treatmentid + replication mapped → average replicates per treatment×date and
    compute N2O flux standard error (Treatment/RawMeasurementTreatment hold averages)."""

    def _mk(self, rows, map_rep=True, status="draft"):
        mapping = {
            "date": {"canonical_field": "date", "source": "user"},
            "n2o_flux": {"canonical_field": "n2oflux", "unit": "g N/ha/d", "source": "user"},
            "PlotID": {"canonical_field": "treatment", "source": "user"},
        }
        if map_rep:
            mapping["rep"] = {"canonical_field": "replication", "source": "user"}
        return UploadSubmission.objects.create(
            owner=self.user, status=status,
            parsed_content={"source": "file", "source_name": "s.csv", "sheets": [{
                "name": "s.csv", "headers": ["date", "n2o_flux", "PlotID", "rep"],
                "rows": rows, "dtypes": ["date", "float", "string", "string"],
                "n_rows": len(rows), "n_rows_stored": len(rows), "header_row": 0,
                "truncated": False}]},
            mapping=mapping,
            metadata_values={"site_latitude": {"value": 42.4, "source": "user"},
                             "site_longitude": {"value": -85.4, "source": "user"}},
            treatment_details={"treatments": ["NT"],
                               "global": {"primary_crop": {"value": "maize", "source": "user"}}},
        )

    def test_averages_replicates_and_computes_se(self):
        from n2odb.views_workbench import _normalized_treatments
        sub = self._mk([["2018-05-01", "1.0", "NT", "1"], ["2018-05-01", "3.0", "NT", "2"]])
        norm = _normalized_treatments(sub)
        self.assertTrue(norm["reps_averaged"])
        rec = norm["perTreatment"]["NT"]["records"][0]
        self.assertAlmostEqual(rec["n2oflux"], 2.0)           # mean of 1 and 3
        self.assertAlmostEqual(rec["fluxstandarderror"], 1.0)  # stdev([1,3])/sqrt(2)

    def test_missing_rep_averages_what_is_present(self):
        from n2odb.views_workbench import _normalized_treatments
        sub = self._mk([
            ["2018-05-01", "2.0", "NT", "1"], ["2018-05-01", "4.0", "NT", "2"],
            ["2018-05-02", "5.0", "NT", "1"],  # rep 2 missing this date
        ])
        recs = {r["date"]: r for r in _normalized_treatments(sub)["perTreatment"]["NT"]["records"]}
        self.assertAlmostEqual(recs["2018-05-01"]["n2oflux"], 3.0)
        self.assertAlmostEqual(recs["2018-05-02"]["n2oflux"], 5.0)   # single value stands
        self.assertIsNone(recs["2018-05-02"]["fluxstandarderror"])   # 1 rep → no SE

    def test_subdaily_without_rep_column_daily_averaged(self):
        # High-resolution data (no rep column): multiple points/day → daily mean, no SE.
        from n2odb.views_workbench import _normalized_treatments
        sub = self._mk([["2018-05-01", "1.0", "NT", "1"], ["2018-05-01", "3.0", "NT", "2"]],
                       map_rep=False)
        norm = _normalized_treatments(sub)
        self.assertFalse(norm["reps_averaged"])
        self.assertEqual(norm["daily_averaged"], 1)
        rec = norm["perTreatment"]["NT"]["records"][0]
        self.assertAlmostEqual(rec["n2oflux"], 2.0)          # daily mean, not first-wins
        self.assertIsNone(rec.get("fluxstandarderror"))       # no reps → no SE

    def test_subdaily_per_rep_then_treatment_average(self):
        # Two reps, each with two sub-daily points on the same day. Level (a): daily-per-rep
        # means are rep1=(1,3)->2, rep2=(5,7)->6. Level (b): treatment mean=(2+6)/2=4,
        # SE across rep daily means stdev([2,6])/sqrt(2) = 2.0.
        from n2odb.views_workbench import _normalized_treatments
        sub = self._mk([
            ["2018-05-01", "1.0", "NT", "1"], ["2018-05-01", "3.0", "NT", "1"],
            ["2018-05-01", "5.0", "NT", "2"], ["2018-05-01", "7.0", "NT", "2"],
        ])
        norm = _normalized_treatments(sub)
        self.assertTrue(norm["reps_averaged"])
        self.assertEqual(norm["daily_averaged"], 1)
        rec = norm["perTreatment"]["NT"]["records"][0]
        self.assertAlmostEqual(rec["n2oflux"], 4.0)
        self.assertAlmostEqual(rec["fluxstandarderror"], 2.0)  # SE across rep daily means

    def test_time_weighted_daily_average(self):
        # High-res data with timestamps → trapezoidal (time-weighted) daily mean, not simple.
        from n2odb.views_workbench import _normalized_treatments
        sub = self._mk([
            ["2018-05-01 00:00:00", "0", "NT", "1"],
            ["2018-05-01 06:00:00", "0", "NT", "1"],
            ["2018-05-01 12:00:00", "12", "NT", "1"],
        ], map_rep=False)
        norm = _normalized_treatments(sub)
        self.assertTrue(norm["time_weighted"])
        self.assertEqual(norm["daily_averaged"], 1)
        rec = norm["perTreatment"]["NT"]["records"][0]
        self.assertAlmostEqual(rec["n2oflux"], 3.0)   # trapezoidal (simple mean would be 4.0)

    def test_promotion_stores_average_and_se(self):
        from n2odb.services import promote
        from n2odb.models import RawMeasurementTreatment
        sub = self._mk([["2018-05-01", "1.0", "NT", "1"], ["2018-05-01", "3.0", "NT", "2"]],
                       status="submitted")
        result = promote.promote(sub)
        rows = RawMeasurementTreatment.objects.filter(
            treatmentid__in=result["treatmentids"], date="2018-05-01")
        self.assertEqual(rows.count(), 1)   # one averaged row, not two rep rows
        r = rows.first()
        self.assertAlmostEqual(r.n2oflux, 2.0)
        self.assertAlmostEqual(r.fluxstandarderror, 1.0)


class ColumnProvidedEventsTests(AuthedWorkbenchTestCase):
    """When an event field is mapped as a column, the Timeline step treats that section
    as done — no prompt, no saved events, no overlay (the column is the single source)."""

    def _mk(self, **mapping_extra):
        mapping = {
            "date": {"canonical_field": "date", "source": "user"},
            "n2o_flux": {"canonical_field": "n2oflux", "unit": "g N/ha/d", "source": "user"},
            "PlotID": {"canonical_field": "treatment", "source": "user"},
        }
        mapping.update(mapping_extra)
        return UploadSubmission.objects.create(
            owner=self.user,
            parsed_content={
                "source": "file", "source_name": "s.csv",
                "sheets": [{
                    "name": "s.csv",
                    "headers": ["date", "n2o_flux", "PlotID", "N_applied", "N_form"],
                    "rows": [["2018-05-01", "1.2", "NT", "100", "urea"],
                             ["2018-05-02", "2.1", "CT", "0", ""]],
                    "dtypes": ["date", "float", "string", "float", "string"],
                    "n_rows": 2, "n_rows_stored": 2, "header_row": 0, "truncated": False,
                }],
            },
            mapping=mapping,
            treatment_details={"treatments": ["NT", "CT"]},
        )

    def _fert_both(self):
        # Fertilisation is only fully provided when BOTH paired columns are mapped.
        return self._mk(
            N_applied={"canonical_field": "nitrogenapplied", "source": "user"},
            N_form={"canonical_field": "nitrogenform", "source": "user"},
        )

    def test_detects_provided_section(self):
        from n2odb.views_workbench import _column_provided_event_sections
        provided = _column_provided_event_sections(self._fert_both())
        self.assertEqual(provided.get("fertilisation"), ["N_applied", "N_form"])
        self.assertNotIn("planting", provided)  # nothing mapped for it

    def test_one_paired_column_is_not_fully_provided(self):
        # Only N applied (no form) → partial, so not marked fully provided.
        from n2odb.views_workbench import _column_provided_event_sections
        sub = self._mk(N_applied={"canonical_field": "nitrogenapplied", "source": "user"})
        self.assertNotIn("fertilisation", _column_provided_event_sections(sub))

    def test_partial_fertilisation_seeds_events(self):
        from n2odb.views_workbench import _column_seeded_fertilisation
        sub = self._mk(N_applied={"canonical_field": "nitrogenapplied", "source": "user"})
        seed = _column_seeded_fertilisation(sub)
        self.assertEqual(seed["field"], "n_amount")
        self.assertEqual(seed["missing"], "form")
        self.assertIn("date", seed["locked"])
        self.assertIn("n_amount", seed["locked"])
        # NT has a non-zero application; CT's row is 0 → no event.
        self.assertEqual(list(seed["per_treatment"]), ["NT"])
        ev = seed["per_treatment"]["NT"][0]
        self.assertEqual(ev["fields"]["date"], "2018-05-01")
        self.assertEqual(ev["fields"]["n_amount"], "100")
        self.assertNotIn("form", ev["fields"])

    def test_partial_fertilisation_reverse_form_column(self):
        from n2odb.views_workbench import _column_seeded_fertilisation
        sub = self._mk(N_form={"canonical_field": "nitrogenform", "source": "user"})
        seed = _column_seeded_fertilisation(sub)
        self.assertEqual(seed["field"], "form")
        self.assertEqual(seed["missing"], "n_amount")
        ev = seed["per_treatment"]["NT"][0]
        self.assertEqual(ev["fields"]["form"], "urea")
        self.assertNotIn("n_amount", ev["fields"])

    def test_both_columns_no_seed(self):
        from n2odb.views_workbench import _column_seeded_fertilisation
        self.assertIsNone(_column_seeded_fertilisation(self._fert_both()))

    def test_nothing_provided_without_event_column(self):
        from n2odb.views_workbench import _column_provided_event_sections
        self.assertEqual(_column_provided_event_sections(self._mk()), {})

    def test_timeline_view_flags_provided_section(self):
        sub = self._fert_both()
        body = self.client.get(reverse("workbench-timeline", args=[sub.public_token])).content.decode()
        # The section registry serialized for the page carries the flag + source columns.
        self.assertIn("column_provided", body)
        self.assertIn("N_applied", body)

    def test_timeline_view_seeds_partial_section(self):
        sub = self._mk(N_applied={"canonical_field": "nitrogenapplied", "source": "user"})
        body = self.client.get(reverse("workbench-timeline", args=[sub.public_token])).content.decode()
        self.assertIn("column_seeded", body)
        self.assertIn("N_applied", body)   # the seeded source column is named

    def test_save_timeline_drops_provided_section(self):
        import json
        sub = self._fert_both()
        self.client.post(
            reverse("workbench-timeline-save", args=[sub.public_token]),
            data=json.dumps({"global": {
                "fertilisation": [{"fields": {"date": "2018-05-01", "n_amount": 100}}],
                "planting": [{"fields": {"date": "2018-04-01", "crop": "maize"}}],
            }}),
            content_type="application/json",
        )
        sub.refresh_from_db()
        self.assertNotIn("fertilisation", sub.events.get("global", {}))  # column-owned → dropped
        self.assertIn("planting", sub.events.get("global", {}))          # still user-entered

    def test_provided_section_events_not_overlaid(self):
        from n2odb.views_workbench import _event_row_values
        sub = self._fert_both()
        # Even if stale fertilisation events exist, they must not overlay onto rows.
        sub.events = {"scopes": {}, "global": {
            "fertilisation": [{"fields": {"date": "2018-05-02", "n_amount": 999}}]}, "per_treatment": {}}
        sub.save(update_fields=["events"])
        rows = _event_row_values(sub, "NT")
        self.assertEqual(rows, {})  # fertilisation skipped (column is the source)


class DateFormatTests(AuthedWorkbenchTestCase):
    """Contributor-declared date format removes MM/DD vs DD/MM ambiguity (#5)."""

    def _mk(self, date_format=None, dates=("2018-05-01", "2018-05-02")):
        date_entry = {"canonical_field": "date", "source": "user"}
        if date_format:
            date_entry["date_format"] = date_format
        return UploadSubmission.objects.create(
            owner=self.user,
            parsed_content={
                "source": "file", "source_name": "s.csv",
                "sheets": [{
                    "name": "s.csv",
                    "headers": ["date", "n2o_flux", "PlotID"],
                    "rows": [[dates[0], "1.2", "NT"], [dates[1], "2.1", "CT"]],
                    "dtypes": ["date", "float", "string"],
                    "n_rows": 2, "n_rows_stored": 2, "header_row": 0, "truncated": False,
                }],
            },
            mapping={
                "date": date_entry,
                "n2o_flux": {"canonical_field": "n2oflux", "unit": "g N/ha/d", "source": "user"},
                "PlotID": {"canonical_field": "treatment", "source": "user"},
            },
            metadata_values={
                "site_latitude": {"value": 42.4, "source": "user"},
                "site_longitude": {"value": -85.4, "source": "user"},
            },
            treatment_details={
                "treatments": ["NT", "CT"],
                "global": {"primary_crop": {"value": "maize", "source": "user"}},
            },
        )

    def test_strict_parse_disambiguates(self):
        from n2odb.views_workbench import _parse_date
        self.assertEqual(_parse_date("03/04/2020", "mdy").isoformat(), "2020-03-04")
        self.assertEqual(_parse_date("03/04/2020", "dmy").isoformat(), "2020-04-03")
        self.assertEqual(_parse_date("2020-03-04", "iso").isoformat(), "2020-03-04")
        self.assertIsNone(_parse_date("2020-03-04", "mdy"))   # ISO doesn't fit MDY → error, not a guess

    def test_auto_is_tolerant(self):
        from n2odb.views_workbench import _parse_date
        self.assertIsNotNone(_parse_date("2020-03-04"))     # no format → legacy guess
        self.assertIsNotNone(_parse_date("03/04/2020"))

    def test_two_digit_year_dashed_format(self):
        from n2odb.views_workbench import _parse_date, _DATE_FORMAT_IDS
        self.assertIn("mdy2", _DATE_FORMAT_IDS)                       # selectable in the dropdown
        self.assertEqual(_parse_date("11-20-05", "mdy2").isoformat(), "2005-11-20")
        self.assertEqual(_parse_date("05-06-07", "mdy2").isoformat(), "2007-05-06")  # month-first
        self.assertIsNone(_parse_date("2020-03-04", "mdy2"))         # 4-digit ISO doesn't fit → flagged

    def test_parses_datetimes_and_strips_time_for_date(self):
        from n2odb.views_workbench import _parse_datetime, _parse_date
        dt = _parse_datetime("07/04/2018 13:45:00", "mdy")
        self.assertEqual((dt.month, dt.day, dt.hour, dt.minute), (7, 4, 13, 45))
        # the date wrapper strips the time
        self.assertEqual(_parse_date("07/04/2018 13:45:00", "mdy").isoformat(), "2018-07-04")
        self.assertEqual(_parse_date("07/04/2018", "mdy").isoformat(), "2018-07-04")  # bare still ok
        self.assertEqual(_parse_date("2018-05-01T12:30:00", "iso").isoformat(), "2018-05-01")

    def test_readiness_ok_by_default(self):
        from n2odb.views_workbench import _submit_readiness
        chk = next(c for c in _submit_readiness(self._mk()) if "date format" in c["label"].lower())
        self.assertTrue(chk["ok"])

    def test_readiness_flags_mismatch(self):
        from n2odb.views_workbench import _submit_readiness
        # ISO data declared as US MM/DD/YYYY → nothing parses → flagged with samples.
        sub = self._mk(date_format="mdy")
        chk = next(c for c in _submit_readiness(sub) if "date format" in c["label"].lower())
        self.assertFalse(chk["ok"])
        self.assertIn("2018-05-01", chk["detail"])

    def test_review_surfaces_date_issue(self):
        from n2odb.views_workbench import _build_review
        data = _build_review(self._mk(date_format="mdy"))
        self.assertIsNotNone(data["dateIssue"])
        self.assertIn("2018-05-01", data["dateIssue"]["samples"])

    def test_save_mapping_persists_date_format(self):
        import json
        sub = self._mk()
        resp = self.client.post(
            reverse("workbench-mapping-save", args=[sub.public_token]),
            data=json.dumps({"mapping": {"date": {"field": "date", "date_format": "dmy"}}}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        sub.refresh_from_db()
        self.assertEqual(sub.mapping["date"]["date_format"], "dmy")

    def test_save_mapping_rejects_bogus_date_format(self):
        import json
        sub = self._mk()
        self.client.post(
            reverse("workbench-mapping-save", args=[sub.public_token]),
            data=json.dumps({"mapping": {"date": {"field": "date", "date_format": "nonsense"}}}),
            content_type="application/json",
        )
        sub.refresh_from_db()
        self.assertNotIn("date_format", sub.mapping["date"])  # invalid dropped, not stored
