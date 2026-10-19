"""Tests for the manuscript fact-extraction module (manuscript-llm-module.md §7).

The Anthropic client is mocked — no network in CI. We assert the request carries the
cached system block + the ``already_have`` list + the delimited manuscript, that the
flatten produces the plumbing's contract shape, and that the seam degrades to
``skipped``/``failed`` instead of raising.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import anthropic
from django.test import TestCase, override_settings

from n2odb.services import manuscript_extract as mx
from n2odb.services.manuscript_extract import (
    EventFact,
    Fact,
    ManuscriptFacts,
    extract_facts,
)


# All schema fields are required-but-nullable now (see manuscript_extract), so these
# factories fill the unlisted keys with None to keep test construction terse.
def _fact(field, value=None, unit=None, source_quote=None, confidence=None):
    return Fact(field=field, value=value, unit=unit, source_quote=source_quote, confidence=confidence)


_EV_DEFAULTS = dict(
    date_iso=None, date_text=None, amount=None, unit=None, nitrogen_form=None,
    nitrogen_type=None, crop=None, tillage_instrument=None, notes=None,
    source_quote=None, confidence=None,
)


def _event(type, **kw):
    return EventFact(type=type, **{**_EV_DEFAULTS, **kw})


def _canned() -> ManuscriptFacts:
    return ManuscriptFacts(
        facts=[
            _fact(field="site_latitude", value="42.4", unit="°N", source_quote="42°24' N", confidence=0.9),
            _fact(field="site_longitude", value="-85.4", source_quote="85°24' W", confidence=0.9),
            _fact(field="site_name", value="KBS LTER", confidence=0.7),
            _fact(field="ph", value="6.4", source_quote="pH 6.4", confidence=0.8),
            _fact(field="sand", value="43"),
            _fact(field="silt", value="38"),
            _fact(field="clay", value="19"),
            _fact(field="primary_crop", value="maize", confidence=0.95),
        ],
        events=[_event(
            type="fertilisation", date_text="early May 2018", date_iso="2018-05-01",
            amount=135, unit="kg N/ha", nitrogen_form="urea", nitrogen_type="mineral",
            source_quote="135 kg N ha-1 as urea", confidence=0.85,
        )],
    )


def _mock_client(parsed=None, error=None) -> MagicMock:
    client = MagicMock()
    if error is not None:
        client.messages.parse.side_effect = error
    else:
        resp = MagicMock()
        resp.parsed_output = parsed
        client.messages.parse.return_value = resp
    return client


class TextureTriangleTests(TestCase):
    def test_known_classes(self):
        # (sand, silt, clay) -> expected USDA class
        cases = {
            (43, 40, 17): "Loam",
            (33, 33, 34): "Clay loam",
            (20, 60, 20): "Silt loam",
            (90, 7, 3): "Sand",
            (10, 10, 80): "Clay",
            (50, 10, 40): "Sandy clay",
            (10, 50, 40): "Silty clay",
            (5, 90, 5): "Silt",
        }
        for (sand, silt, clay), expected in cases.items():
            with self.subTest(split=(sand, silt, clay)):
                self.assertEqual(mx._usda_texture(sand, silt, clay), expected)


class StripReferencesTests(TestCase):
    def test_trailing_reference_list_removed(self):
        body = "Intro and methods. " * 40
        text = body + "\nReferences\nSmith J. 2020. Soil N2O. Journal 1:2-3.\n"
        out = mx._strip_references(text)
        self.assertIn("Intro and methods.", out)
        self.assertNotIn("Smith J. 2020", out)

    def test_early_reference_mention_kept(self):
        # A "references" heading in the first half is not the bibliography — keep all.
        text = "References\n" + ("Real body content follows here. " * 60)
        self.assertEqual(mx._strip_references(text), text)

    def test_no_heading_returns_unchanged(self):
        text = "A short manuscript with no reference section."
        self.assertEqual(mx._strip_references(text), text)


_IMRAD = """Soil N2O from a maize trial
J. Smith and A. Jones, 2020

Abstract
KEEP_ABSTRACT: we measured N2O fluxes over two seasons.

1. Introduction
DROP_INTRO: nitrous oxide is a potent greenhouse gas discussed at length here.

2. Materials and Methods
KEEP_METHODS: the study site is at 42N, soil pH 6.4, maize, urea applied 1 May.

2.1 Soil sampling
KEEP_SUB: cores were taken to 30 cm and analysed for texture.

3. Results
DROP_RESULTS: cumulative fluxes were high in the fertilised plots.

4. Discussion
DROP_DISCUSSION: our findings agree with previous work.

5. Conclusions
DROP_CONCL: management matters.

Acknowledgements
DROP_ACK: we thank the technicians.

References
DROP_REF: Smith J. 2020. Journal 1:2-3.
"""


class SectionTrimTests(TestCase):
    def test_keeps_abstract_and_methods_drops_the_rest(self):
        out = mx._keep_relevant_sections(_IMRAD)
        for kept in ("KEEP_ABSTRACT", "KEEP_METHODS", "KEEP_SUB"):
            self.assertIn(kept, out)
        for dropped in ("DROP_INTRO", "DROP_RESULTS", "DROP_DISCUSSION",
                        "DROP_CONCL", "DROP_ACK", "DROP_REF"):
            self.assertNotIn(dropped, out)

    def test_combined_results_and_discussion_dropped(self):
        text = _IMRAD.replace("3. Results\nDROP_RESULTS: cumulative fluxes were high in the fertilised plots.\n\n4. Discussion\nDROP_DISCUSSION: our findings agree with previous work.",
                              "3. Results and Discussion\nDROP_COMBINED: fluxes were high and agree with prior work.")
        out = mx._keep_relevant_sections(text)
        self.assertNotIn("DROP_COMBINED", out)
        self.assertIn("KEEP_METHODS", out)

    def test_no_methods_section_returns_unchanged(self):
        text = "Intro line\n1. Introduction\nbody\n3. Results\nmore body\n"
        self.assertEqual(mx._keep_relevant_sections(text), text)

    def test_no_headings_returns_unchanged(self):
        text = "Just some running text with no section headings at all."
        self.assertEqual(mx._keep_relevant_sections(text), text)

    @override_settings(WORKBENCH_TRIM_SECTIONS=False)
    def test_disabled_returns_unchanged(self):
        self.assertEqual(mx._keep_relevant_sections(_IMRAD), _IMRAD)


class SchemaTests(TestCase):
    def test_field_ids_cover_all_site_fields(self):
        # The flat schema's field ids must stay in sync with the plumbing registry,
        # so every site field the model can fill maps to a real contract id.
        from n2odb.services import site_fields
        site_ids = {f.id for f in site_fields.SITE_FIELDS}
        self.assertEqual(site_ids - mx._VALID_FIELD_IDS, set())


class DisabledTests(TestCase):
    @override_settings(WORKBENCH_LLM_ENABLED=False)
    def test_disabled_returns_skipped(self):
        # Force-disabled regardless of the ambient .env (which may carry a key).
        out = extract_facts("some manuscript text", [])
        self.assertEqual(out, {"status": "skipped", "metadata": {}, "events": []})

    @patch("n2odb.services.llm_assist.is_enabled", return_value=True)
    def test_empty_text_skipped_without_calling_model(self, _en):
        with patch("n2odb.services.llm_assist._client") as client:
            out = extract_facts("   ", ["n2oflux"])
        self.assertEqual(out["status"], "skipped")
        client.assert_not_called()


@patch("n2odb.services.llm_assist.is_enabled", return_value=True)
class EnabledTests(TestCase):
    def test_ok_flattens_to_contract(self, _en):
        client = _mock_client(parsed=_canned())
        with patch("n2odb.services.llm_assist._client", return_value=client):
            out = extract_facts("The KBS LTER site (42°24' N, 85°24' W)...", ["n2oflux", "vwc"])

        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["model"], "claude-haiku-5-5")
        self.assertIn("generated_at", out)

        meta = out["metadata"]
        self.assertEqual(meta["site_latitude"]["value"], "42.4")
        self.assertEqual(meta["ph"]["value"], "6.4")
        self.assertEqual(meta["primary_crop"]["value"], "maize")
        self.assertEqual(meta["site_name"]["value"], "KBS LTER")

        self.assertEqual(len(out["events"]), 1)
        ev = out["events"][0]
        self.assertEqual(ev["type"], "input_application")  # "fertilisation" remapped
        self.assertEqual(ev["date_iso"], "2018-05-01")
        keys = {f["key"]: f for f in ev["fields"]}
        self.assertEqual(keys["amount"]["value"], 135)
        self.assertEqual(keys["amount"]["unit"], "kg N/ha")
        self.assertEqual(keys["input_form"]["value"], "urea")
        self.assertEqual(keys["input_source"]["value"], "mineral")

    def test_request_carries_cached_system_and_delimited_doc(self, _en):
        client = _mock_client(parsed=_canned())
        with patch("n2odb.services.llm_assist._client", return_value=client):
            extract_facts("MANUSCRIPT BODY TEXT HERE", ["n2oflux"])

        kwargs = client.messages.parse.call_args.kwargs
        # system: a single text block that names the already-have field. With the
        # default WORKBENCH_LLM_CACHE_TTL="none" no cache_control is attached —
        # cache writes bill at a premium that a one-shot call never earns back.
        sys_block = kwargs["system"][0]
        self.assertNotIn("cache_control", sys_block)
        self.assertIn("Nitrous oxide flux", sys_block["text"])
        # user content: the manuscript wrapped in an explicit delimiter, likewise
        # uncached by default.
        doc_block = kwargs["messages"][0]["content"][0]
        self.assertNotIn("cache_control", doc_block)
        content_text = doc_block["text"]
        self.assertIn("<manuscript>", content_text)
        self.assertIn("MANUSCRIPT BODY TEXT HERE", content_text)
        # structured output + configured model.
        self.assertIs(kwargs["output_format"], ManuscriptFacts)
        self.assertEqual(kwargs["model"], "claude-haiku-5-5")
        self.assertEqual(kwargs["output_config"], {"effort": "medium"})
        self.assertEqual(kwargs["max_tokens"], 16000)

    def test_pdf_native_sends_document_block_only_when_enabled(self, _en):
        for native, expect_doc in ((False, False), (True, True)):
            client = _mock_client(parsed=_canned())
            with patch("n2odb.services.llm_assist._client", return_value=client), \
                    self.settings(WORKBENCH_LLM_PDF_NATIVE=native):
                extract_facts("TEXT BODY", ["n2oflux"], pdf_bytes=b"%PDF-1.4 fake")
            content = client.messages.parse.call_args.kwargs["messages"][0]["content"]
            self.assertEqual(content[0]["type"] == "document", expect_doc)
            if expect_doc:
                self.assertEqual(content[0]["source"]["media_type"], "application/pdf")
                self.assertNotIn("TEXT BODY", content[1]["text"])

    def test_cache_ttl_opt_in_tags_blocks(self, _en):
        # Setting a TTL re-enables prompt caching on both blocks.
        client = _mock_client(parsed=_canned())
        with patch("n2odb.services.llm_assist._client", return_value=client), \
                self.settings(WORKBENCH_LLM_CACHE_TTL="1h"):
            extract_facts("MANUSCRIPT BODY TEXT HERE", ["n2oflux"])
        kwargs = client.messages.parse.call_args.kwargs
        for block in (kwargs["system"][0], kwargs["messages"][0]["content"][0]):
            self.assertEqual(block["cache_control"], {"type": "ephemeral", "ttl": "1h"})

    def test_planting_and_tillage_event_fields_flatten(self, _en):
        facts = ManuscriptFacts(events=[
            _event(type="planting", date_iso="2018-04-20", crop="maize"),
            _event(type="tillage", date_text="spring", tillage_instrument="moldboard plough"),
        ])
        client = _mock_client(parsed=facts)
        with patch("n2odb.services.llm_assist._client", return_value=client):
            out = extract_facts("text", [])
        by_type = {e["type"]: e for e in out["events"]}
        plant = {f["key"]: f for f in by_type["planting"]["fields"]}
        self.assertEqual(plant["crop"]["value"], "maize")
        till = {f["key"]: f for f in by_type["tillage"]["fields"]}
        self.assertEqual(till["tillage_instrument"]["value"], "moldboard plough")

    def test_confidence_clamped_and_bad_coord_dropped(self, _en):
        facts = ManuscriptFacts(facts=[
            _fact(field="site_latitude", value="999", confidence=1.5),   # impossible → dropped
            _fact(field="site_name", value="Farm A", confidence=1.7),    # clamp to 1.0
        ])
        client = _mock_client(parsed=facts)
        with patch("n2odb.services.llm_assist._client", return_value=client):
            out = extract_facts("text", [])

        self.assertEqual(out["status"], "ok")
        self.assertNotIn("site_latitude", out["metadata"])
        self.assertEqual(out["metadata"]["site_name"]["confidence"], 1.0)

    def test_option_choices_enforced(self, _en):
        facts = ManuscriptFacts(facts=[
            _fact(field="flux_instrument", value="Static Chamber"),      # in list -> kept
            _fact(field="management", value="conventional"),            # lowercase -> normalised
            _fact(field="texture_class", value="fine-loamy family, mesic"),  # off-list -> dropped
        ])
        client = _mock_client(parsed=facts)
        opts = {
            "flux_instrument": ["Static Chamber", "Eddy Covariance"],
            "management": ["Conventional", "No-till"],
            "texture_class": ["Loam", "Clay"],
        }
        with patch("n2odb.services.llm_assist._client", return_value=client):
            out = extract_facts("text", [], opts)
        meta = out["metadata"]
        self.assertEqual(meta["flux_instrument"]["value"], "Static Chamber")
        self.assertEqual(meta["management"]["value"], "Conventional")  # canonical casing
        self.assertNotIn("texture_class", meta)                        # off-list dropped
        # The allowed lists are handed to the model in the (cached) system prompt.
        sys_text = client.messages.parse.call_args.kwargs["system"][0]["text"]
        self.assertIn("CONTROLLED VOCABULARIES", sys_text)
        self.assertIn("Static Chamber", sys_text)

    def test_option_near_variants_resolved(self, _en):
        # The model may return a near-canonical variant; resolve it, don't drop it.
        facts = ManuscriptFacts(facts=[
            _fact(field="flux_instrument", value="static chambers"),  # fuzzy -> Static Chamber
            _fact(field="management", value="no till"),               # normalized -> No-till
        ])
        client = _mock_client(parsed=facts)
        opts = {
            "flux_instrument": ["Static Chamber", "Automatic Chamber", "Eddy Covariance"],
            "management": ["Conventional", "No-till", "Reduced till"],
        }
        with patch("n2odb.services.llm_assist._client", return_value=client):
            out = extract_facts("text", [], opts)
        self.assertEqual(out["metadata"]["flux_instrument"]["value"], "Static Chamber")
        self.assertEqual(out["metadata"]["management"]["value"], "No-till")  # not "Reduced till"

    def test_texture_derived_from_fractions_overrides_text(self, _en):
        from n2odb.services import site_fields
        usda = list(site_fields.get("texture_class").options)
        facts = ManuscriptFacts(facts=[
            _fact(field="sand", value="43", unit="%"),
            _fact(field="clay", value="17", unit="%"),   # silt inferred = 40 -> Loam
            _fact(field="texture_class", value="Clay"),   # text guess, should be overridden
        ])
        client = _mock_client(parsed=facts)
        with patch("n2odb.services.llm_assist._client", return_value=client):
            out = extract_facts("text", [], {"texture_class": usda})
        tex = out["metadata"]["texture_class"]
        self.assertEqual(tex["value"], "Loam")
        self.assertIn("derived", tex["source_quote"])

    def test_texture_falls_back_to_text_without_fractions(self, _en):
        from n2odb.services import site_fields
        usda = list(site_fields.get("texture_class").options)
        facts = ManuscriptFacts(facts=[_fact(field="texture_class", value="Silt loam")])
        client = _mock_client(parsed=facts)
        with patch("n2odb.services.llm_assist._client", return_value=client):
            out = extract_facts("text", [], {"texture_class": usda})
        self.assertEqual(out["metadata"]["texture_class"]["value"], "Silt loam")

    def test_api_error_returns_failed(self, _en):
        err = anthropic.APIError("boom", httpx.Request("POST", "http://x"), body=None)
        client = _mock_client(error=err)
        with patch("n2odb.services.llm_assist._client", return_value=client):
            out = extract_facts("text", [])
        self.assertEqual(out, {"status": "failed", "metadata": {}, "events": []})

    def test_no_parseable_output_returns_failed(self, _en):
        client = _mock_client(parsed=None)  # model returned nothing usable
        with patch("n2odb.services.llm_assist._client", return_value=client):
            out = extract_facts("text", [])
        self.assertEqual(out["status"], "failed")


class TreatmentScopeTests(TestCase):
    """WORKBENCH_LLM_TREATMENT_SCOPE: whole-site values stay in `metadata`; per-treatment ones
    go to `treatment_metadata`; events carry their treatment scope."""

    def _fact(self, field, value, treatment):
        return mx.ScopedFact(field=field, value=value, unit=None, source_quote=None, confidence=0.9,
                             treatment=treatment)

    def _event(self, date, treatment):
        return mx.ScopedEventFact(type="fertilisation", date_iso=date, date_text=None, amount=50.0,
                                  unit="kg N/ha", nitrogen_form="urea", nitrogen_type=None, crop=None,
                                  tillage_instrument=None, notes=None, source_quote=None, confidence=0.9,
                                  treatment=treatment)

    def test_scoped_contract(self):
        trts = [mx.TreatmentInfo(name="N0", abbreviation=None, description="unfertilised"),
                mx.TreatmentInfo(name="N150", abbreviation=None, description="150 kg N/ha")]
        facts = mx.ScopedManuscriptFacts(
            treatments=trts,
            facts=[self._fact("ph", "6.1", "N150"), self._fact("ph", "6.0", None),
                   self._fact("ph", "5.9", "N0")],
            events=[self._event("2020-05-01", None), self._event("2020-06-01", "N150")],
        )
        out = mx._to_contract(facts)
        self.assertEqual([t["name"] for t in out["treatments"]], ["N0", "N150"])
        self.assertEqual(out["metadata"]["ph"]["value"], "6.0")  # whole-site value wins
        self.assertEqual(out["treatment_metadata"]["N0"]["ph"]["value"], "5.9")
        self.assertEqual(out["treatment_metadata"]["N150"]["ph"]["value"], "6.1")
        self.assertEqual([e["treatment"] for e in out["events"]], [None, "N150"])

    @patch("n2odb.services.llm_assist.is_enabled", return_value=True)
    def test_flag_switches_schema_and_prompt(self, _en):
        for scoped in (False, True):
            client = _mock_client(parsed=_canned())
            with patch("n2odb.services.llm_assist._client", return_value=client), \
                    self.settings(WORKBENCH_LLM_TREATMENT_SCOPE=scoped):
                extract_facts("text", [])
            kw = client.messages.parse.call_args.kwargs
            self.assertIs(kw["output_format"], mx.ScopedManuscriptFacts if scoped else ManuscriptFacts)
            self.assertEqual("TREATMENTS." in kw["system"][0]["text"], scoped)


    @patch("n2odb.services.llm_assist.is_enabled", return_value=True)
    def test_scoped_events_pass_tags_events_and_adds_treatments(self, _en):
        """With scope + events pass, the events call uses the scoped schema; treatments it names
        that the facts call didn't are added."""
        def parse(**kw):
            resp = MagicMock()
            resp.usage.input_tokens, resp.usage.output_tokens = 10, 5
            if kw["output_format"] is mx.ScopedEventsOnly:
                resp.parsed_output = mx.ScopedEventsOnly(
                    treatments=[mx.TreatmentInfo(name="CT", abbreviation="CT", description=None)],
                    events=[self._event("2020-06-01", "CT")])
            else:
                resp.parsed_output = mx.ScopedManuscriptFacts(
                    treatments=[mx.TreatmentInfo(name="NT", abbreviation="NT", description=None)],
                    facts=[self._fact("ph", "6.0", None)], events=[])
            return resp
        client = MagicMock()
        client.messages.parse.side_effect = parse
        with patch("n2odb.services.llm_assist._client", return_value=client), \
                self.settings(WORKBENCH_LLM_TREATMENT_SCOPE=True, WORKBENCH_LLM_EVENTS_PASS=True):
            out = extract_facts("text", [])
        self.assertEqual([t["name"] for t in out["treatments"]], ["NT", "CT"])
        self.assertEqual([e["treatment"] for e in out["events"]], ["CT"])


class PlaceholderTextTests(TestCase):
    def test_placeholder_text_is_no_value(self):
        """'None' / 'Not explicitly reported' instead of null would sit in the form as text to delete."""
        facts = ManuscriptFacts(
            facts=[_fact(field="clay", value="Not explicitly reported"), _fact(field="cover_crop", value="None"),
                   _fact(field="ph", value="6.4")],
            events=[_event(type="fertilisation", date_iso=None, date_text="not stated", amount=50.0, unit="kg N/ha",
                           nitrogen_form="N/A")])
        out = mx._to_contract(facts)
        self.assertEqual(set(out["metadata"]), {"ph"})
        ev = out["events"][0]
        self.assertIsNone(ev["date_text"])
        self.assertEqual([f["key"] for f in ev["fields"]], ["amount"])


class NoTillTests(TestCase):
    def test_no_till_is_no_implement(self):
        """No-till is recorded as Management; in the tillage-implement fields it is a value to delete."""
        for v in ("No-till", "no till", "No-tillage", "Zero tillage", "Continuous no-till corn; No-till (continuous corn)",
                  "No-till (direct sowing on untilled soil, residue retention)", "NT", "Direct drilling",
                  "None (undisturbed)", "never tilled"):
            self.assertTrue(mx.is_no_till(v), v)
        for v in ("Chisel plow", "Conventional tillage (CT) and no-till (NT)", "Tillage to a depth of 25 cm", None, ""):
            self.assertFalse(mx.is_no_till(v), v)
        facts = ManuscriptFacts(
            facts=[_fact(field="tillage", value="No-till"), _fact(field="ph", value="6.4")],
            events=[_event(type="tillage", date_iso="2020-04-01", date_text=None, tillage_instrument="no-till drill")])
        out = mx._to_contract(facts)
        self.assertEqual(set(out["metadata"]), {"ph"})
        self.assertEqual(out["events"][0]["fields"], [])


class MapTreatmentsTests(TestCase):
    @patch("n2odb.services.llm_assist.is_enabled", return_value=True)
    def test_links_filtered_to_known_names_and_labels(self, _en):
        links = mx.TreatmentLinks(links=[mx.TagLink(label="HI", data_treatments=["KT_85%", "invented"]),
                                         mx.TagLink(label="ZZ", data_treatments=["KT_50%"])])
        client = _mock_client(parsed=links)
        with patch("n2odb.services.llm_assist._client", return_value=client):
            out = mx.map_treatments(["HI"], [], ["KT_50%", "KT_85%"])
        self.assertEqual(out, {"HI": ["KT_85%"]})

    def test_off_or_nothing_to_map_returns_none(self):
        self.assertIsNone(mx.map_treatments([], [], ["A"]))
        with patch("n2odb.services.llm_assist.is_enabled", return_value=False):
            self.assertIsNone(mx.map_treatments(["HI"], [], ["A"]))


@patch("n2odb.services.llm_assist.is_enabled", return_value=True)
class EventsPassTests(TestCase):
    """WORKBENCH_LLM_EVENTS_PASS: a second, events-only call supplies the events."""

    def _client(self, events_error=None):
        def parse(**kw):
            if kw["output_format"] is mx.EventsOnly:
                if events_error:
                    raise events_error
                resp = MagicMock()
                resp.parsed_output = mx.EventsOnly(events=_canned().events * 2)
                resp.usage.input_tokens, resp.usage.output_tokens = 100, 20
                return resp
            resp = MagicMock()
            resp.parsed_output = _canned()
            resp.usage.input_tokens, resp.usage.output_tokens = 100, 50
            return resp
        client = MagicMock()
        client.messages.parse.side_effect = parse
        return client

    def test_events_come_from_the_events_call(self, _en):
        client = self._client()
        with patch("n2odb.services.llm_assist._client", return_value=client), \
                self.settings(WORKBENCH_LLM_EVENTS_PASS=True):
            out = extract_facts("text", [])
        self.assertEqual(out["status"], "ok")
        self.assertEqual(len(out["events"]), 2)
        self.assertEqual(out["events_status"], "ok")
        self.assertEqual(out["usage"], {"calls": 2, "input_tokens": 200, "output_tokens": 70})
        systems = [c.kwargs["system"][0]["text"] for c in client.messages.parse.call_args_list]
        self.assertTrue(any("SEPARATE PASS" in t for t in systems))

    def test_events_call_gets_a_larger_output_cap(self, _en):
        client = self._client()
        with patch("n2odb.services.llm_assist._client", return_value=client), \
                self.settings(WORKBENCH_LLM_EVENTS_PASS=True):
            extract_facts("text", [])
        caps = {c.kwargs["output_format"].__name__: c.kwargs["max_tokens"]
                for c in client.messages.parse.call_args_list}
        self.assertEqual(caps, {"EventsOnly": 32000, "ManuscriptFacts": 16000})

    def test_metadata_survives_a_failed_events_call(self, _en):
        client = self._client(events_error=anthropic.APIConnectionError(request=httpx.Request("POST", "http://x")))
        with patch("n2odb.services.llm_assist._client", return_value=client), \
                self.settings(WORKBENCH_LLM_EVENTS_PASS=True):
            out = extract_facts("text", [])
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["metadata"]["ph"]["value"], "6.4")
        self.assertEqual((out["events"], out["events_status"]), ([], "failed"))
