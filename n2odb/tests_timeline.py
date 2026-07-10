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
                "n2o_flux": {"canonical_field": "n2oflux", "source": "user"},
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
                "n2o_flux": {"canonical_field": "n2oflux", "source": "user"},
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

    def test_readiness_flags_missing_coordinates(self):
        from n2odb.views_workbench import _submit_readiness
        sub = self._mk(metadata_values={})  # no lat/long
        coord = next(c for c in _submit_readiness(sub) if "coordinates" in c["label"])
        self.assertFalse(coord["ok"])

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
            data={"name": "Dr Ada", "email": "ada@example.org", "message": "thanks"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        sub.refresh_from_db()
        self.assertEqual(sub.status, "submitted")
        self.assertIsNotNone(sub.submitted_at)
        self.assertEqual(sub.contributor_name, "Dr Ada")
        self.assertEqual(sub.contributor_email, "ada@example.org")
        self.assertEqual(sub.review_notes[0]["note"], "thanks")

    def test_finalize_validates_contributor(self):
        sub = self._mk()
        resp = self.client.post(
            reverse("workbench-submit-finalize", args=[sub.public_token]),
            data={"name": "", "email": "not-an-email"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("name", resp.json()["fields"])
        self.assertIn("email", resp.json()["fields"])
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
                "n2o_flux": {"canonical_field": "n2oflux", "source": "user"},
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

    def test_promote_is_idempotent(self):
        from n2odb.services import promote
        sub = self._mk()
        promote.promote(sub)
        with self.assertRaises(promote.PromotionError):
            promote.promote(sub)  # second attempt refused

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
                "n2o_flux": {"canonical_field": "n2oflux", "source": "user"},
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
