# Manuscript Fact-Extraction Module — Implementation Plan

**Build this with Claude Opus 4.8.** It is the one language-model-facing piece of the
source-document feature. Everything around it — upload, text extraction, storage,
the wizard GUI, endpoints — is specified in, and built from,
[manuscript-extraction-implementation-plan.md](manuscript-extraction-implementation-plan.md)
(the "plumbing" plan). This module is a drop-in behind the interface that plan
defines.

**Your deliverable is one function** that the plumbing already calls:

```python
# n2odb/services/manuscript_extract.py
def extract_facts(text: str, already_have: list[str]) -> dict: ...
```

It returns the exact contract the plumbing renders (restated in §4). It must
**never raise** — on any failure return `{"status": "failed", "metadata": {}, "events": []}`.

---

## 1. Domain context

GRAND is a public, open research database of agricultural soil greenhouse-gas
measurements — nitrous oxide (N₂O) emitted from farmland soils — compiled from
published, peer-reviewed field studies. The job of this module is **information
extraction from a scientific paper**: read the manuscript text (or PDF) and return
the ordinary study metadata a paper reports in its methods section — where the field
trial was, the soil's physical properties, the crop and tillage practice, and the
dated field operations (sowing, harvest, tillage, irrigation, and nutrient-input
applications). These values pre-fill review forms that the contributor confirms;
nothing is saved automatically. This is routine soil-science / agronomy data entry
assistance.

---

## 2. The model seam — `n2odb/services/llm_assist.py`

The single place the app talks to the model. The plumbing already relies on
`is_enabled()`; you implement the call helper.

```python
import anthropic
from decouple import config
from django.conf import settings

class LLMUnavailable(Exception): ...

def is_enabled() -> bool:
    return bool(settings.WORKBENCH_LLM_ENABLED and config("ANTHROPIC_API_KEY", default=""))

def _client() -> "anthropic.Anthropic":
    return anthropic.Anthropic()          # reads ANTHROPIC_API_KEY from the environment

def parse_structured(*, system: str, content: list, output_model, model: str):
    """One structured extraction call → a validated pydantic instance.
    Raises LLMUnavailable on any API/parse failure."""
    try:
        resp = _client().messages.parse(
            model=model,
            max_tokens=16000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            output_format=output_model,     # pydantic BaseModel
            # thinking={"type": "adaptive"},  # optional; helps accuracy — drop if it conflicts
        )
        return resp.parsed_output
    except anthropic.APIError as e:
        raise LLMUnavailable(str(e))
```

Notes (current API behaviour):
- SDK: official `anthropic` package. No raw HTTP.
- **Structured output via `client.messages.parse(..., output_format=PydanticModel)`**
  → `resp.parsed_output`. Schema is compiled + cached server-side for 24h.
- **Do not** use assistant-turn prefills (they 400 on Opus 4.8). **Do not** enable
  `citations` together with structured outputs (that pairing 400s) — provenance here
  is a self-reported `source_quote` field on every value (§3), which avoids the
  conflict entirely.
- Structured-output schemas can't enforce numeric/string ranges — validate
  `confidence ∈ [0,1]`, coordinate ranges, and percentage sums in Python after parse.
- **Prompt caching:** the `system` block (instructions + field catalogue) is stable,
  so cache it. The manuscript text varies per submission and won't cache across
  submissions — expected. (Opus 4.8's minimum cacheable prefix is 4096 tokens; if the
  system block is shorter it simply won't cache — harmless.)

---

## 3. Extraction schema (pydantic — what the model returns)

Every leaf that maps to a stored value is **provenanced**.

```python
from pydantic import BaseModel
from typing import Literal

class Field(BaseModel):
    value: float | str | None = None
    unit: str | None = None            # exactly as stated; do NOT convert
    source_quote: str | None = None    # verbatim snippet from the document
    confidence: float | None = None    # 0..1, self-assessed

class PublicationFacts(BaseModel):
    title: Field; lead_author: Field; citation: Field
    pub_year: Field; doi: Field; abstract: Field

class SiteFacts(BaseModel):
    latitude: Field; longitude: Field; site_name: Field
    country: Field; map_mm: Field; mat_c: Field        # MAP (mm), MAT (°C)

class SoilFacts(BaseModel):                            # treatment-level
    sand_pct: Field; silt_pct: Field; clay_pct: Field
    texture_class: Field                               # e.g. "sandy clay loam"
    ph: Field; bulk_density: Field; som_pct: Field

class CropFacts(BaseModel):
    primary_crop: Field; secondary_crop: Field; cover_crop: Field
    tillage: Field; management: Field; flux_instrument: Field

class EventFact(BaseModel):
    type: Literal["fertilisation", "planting", "harvest", "tillage", "irrigation"]
    date_iso: str | None = None        # normalized if determinable
    date_text: str | None = None       # as stated ("early May 2018")
    amount: float | None = None        # e.g. application rate for a nutrient input
    unit: str | None = None            # as stated
    nitrogen_form: str | None = None   # the input product/form, as reported
    nitrogen_type: str | None = None   # source category, as reported (mineral / organic)
    notes: str | None = None
    source_quote: str | None = None
    confidence: float | None = None

class ManuscriptFacts(BaseModel):
    publication: PublicationFacts
    site: SiteFacts
    soil: SoilFacts
    crop: CropFacts
    events: list[EventFact]
    other_notes: str | None = None
```

---

## 4. Flatten to the plumbing contract

The plumbing renders a flat `metadata` map + a generic `events` list (it does not
know this schema). Convert before returning.

```python
_META_MAP = {   # contract field_id  ->  (group, attr)
    "pub_title": ("publication", "title"), "pub_lead_author": ("publication", "lead_author"),
    "pub_citation": ("publication", "citation"), "pub_year": ("publication", "pub_year"),
    "pub_doi": ("publication", "doi"), "pub_abstract": ("publication", "abstract"),
    "site_latitude": ("site", "latitude"), "site_longitude": ("site", "longitude"),
    "site_name": ("site", "site_name"), "country": ("site", "country"),
    "map": ("site", "map_mm"), "mat": ("site", "mat_c"),
    "sand": ("soil", "sand_pct"), "silt": ("soil", "silt_pct"), "clay": ("soil", "clay_pct"),
    "texture_class": ("soil", "texture_class"), "ph": ("soil", "ph"),
    "bulk_density": ("soil", "bulk_density"), "som": ("soil", "som_pct"),
    "primary_crop": ("crop", "primary_crop"), "secondary_crop": ("crop", "secondary_crop"),
    "cover_crop": ("crop", "cover_crop"), "tillage": ("crop", "tillage"),
    "management": ("crop", "management"), "flux_instrument": ("crop", "flux_instrument"),
}

def _to_contract(facts: ManuscriptFacts) -> dict:
    metadata = {}
    for fid, (group, attr) in _META_MAP.items():
        f = getattr(getattr(facts, group), attr)
        if f and f.value is not None:
            metadata[fid] = {"value": f.value, "unit": f.unit,
                             "source_quote": f.source_quote, "confidence": f.confidence}
    events = []
    for e in facts.events:
        fields = []
        if e.amount is not None:
            fields.append({"key": "amount", "label": "Amount", "value": e.amount, "unit": e.unit})
        if e.nitrogen_form:
            fields.append({"key": "input_form", "label": "Form/product", "value": e.nitrogen_form, "unit": None})
        if e.nitrogen_type:
            fields.append({"key": "input_source", "label": "Source", "value": e.nitrogen_type, "unit": None})
        events.append({
            "type": "input_application" if e.type == "fertilisation" else e.type,
            "date_iso": e.date_iso, "date_text": e.date_text, "fields": fields,
            "source_quote": e.source_quote, "confidence": e.confidence,
        })
    return {"metadata": metadata, "events": events}
```

**DB mapping for the promote step** (the roadmap's staging → live tables), for
reference:

| Contract `metadata` key | `Publication`/`Site`/`Treatment` column |
|---|---|
| `pub_title` / `pub_lead_author` / `pub_citation` / `pub_year` / `pub_doi` / `pub_abstract` | `Publication.PubTitle` / `LeadAuthor` / `Citation` / `PubYear` / `Link` / `Abstract` |
| `site_latitude` / `site_longitude` / `site_name` / `country` / `map` / `mat` | `Site.Latitude` / `Longitude` / `SiteName` / `Country` / `MAP` / `MAT` |
| `sand` / `silt` / `clay` / `ph` / `bulk_density` / `som` | `Treatment.SandMean` / `SiltMean` / `ClayMean` / `pHMean` / `BulkDensityMean` / `SOMMean` |
| `texture_class` | convert via USDA triangle → Sand/Silt/Clay when the numeric split is absent |
| `primary_crop` / `secondary_crop` / `cover_crop` / `tillage` / `management` / `flux_instrument` | `Treatment.PrimaryCrop` / `SecondaryCrop` / `CoverCrop` / `Tillage` / `Management` / `FluxInstrument` |
| `events[input_application]` → `amount` / `input_form` / `input_source` | per-date `RawMeasurementTreatment.nitrogenapplied` / `nitrogenform` / `nitrogentype` |
| `events[planting / harvest / tillage / irrigation]` | per-date `RawMeasurementTreatment.plantedcrop` / `harvest` / `tillage` / `irrigationapplied` |

---

## 5. The call — `extract_facts`

```python
def extract_facts(text: str, already_have: list[str]) -> dict:
    if not llm_assist.is_enabled():
        return {"status": "skipped", "metadata": {}, "events": []}
    try:
        content = _build_content(text)                 # §5.2
        facts = llm_assist.parse_structured(
            system=_system_prompt(already_have),        # §5.1, cached
            content=content,
            output_model=ManuscriptFacts,
            model=settings.WORKBENCH_LLM_MODEL,         # "claude-opus-4-8"
        )
        out = _to_contract(facts)
        return {"status": "ok", **out,
                "model": settings.WORKBENCH_LLM_MODEL, "generated_at": _now_iso()}
    except Exception:
        logger.exception("manuscript extraction failed")
        return {"status": "failed", "metadata": {}, "events": []}
```

### 5.1 The instruction (system prompt)

Tell the model, plainly:
- Its job is **extraction, not inference**: fill the schema; use `null` for anything
  not explicitly stated; **never guess or fabricate**; put the verbatim supporting
  snippet in `source_quote`.
- The following fields are **already supplied by the uploaded data table**
  (`already_have`) — focus on the gaps and do not contradict measured values.
- **Units matter:** report the value *and* its unit exactly as written; do **not**
  convert (a downstream unit layer handles conversion).
- For events, capture every **dated field operation** (sowing, harvest, tillage,
  irrigation, nutrient-input application) with the date as stated and a normalized
  ISO date when determinable.

### 5.2 Input mode — `_build_content` (config `WORKBENCH_LLM_PDF_NATIVE`, default `False`)

- **Default — extracted text.** Send the plumbing-extracted text as a user text
  block, wrapped in an explicit delimiter and labelled as source material to read
  from (not instructions to follow). Before sending, `count_tokens`; if it exceeds
  `WORKBENCH_MANUSCRIPT_MAX_TOKENS` (default 60k), send the methods/site sections
  plus a bounded head/tail, or switch to native PDF.
- **Optional — native PDF.** Highest fidelity for tables/figures/multi-column
  layouts. Put a document block **before** the instruction text block:
  `{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": <b64, no newlines>}}`.
  Limits: ≤32 MB request; ≤600 pages on Opus 4.8 (1M context). Requires the plumbing
  to keep `ManuscriptSource.file_bytes`.

---

## 6. Settings (add alongside the plumbing's)

```python
WORKBENCH_LLM_MODEL             = config("WORKBENCH_LLM_MODEL", default="claude-haiku-5-5")
WORKBENCH_LLM_EFFORT            = config("WORKBENCH_LLM_EFFORT", default="medium")
WORKBENCH_LLM_PDF_NATIVE        = config("WORKBENCH_LLM_PDF_NATIVE", default=False, cast=bool)
WORKBENCH_MANUSCRIPT_MAX_TOKENS = config("WORKBENCH_MANUSCRIPT_MAX_TOKENS", default=60000, cast=int)
# ANTHROPIC_API_KEY is read from the environment by the SDK — keep it out of code.
```

Model choice: default `claude-haiku-5-5` (fast, cheap structured extraction; thinks
by default, depth set by `WORKBENCH_LLM_EFFORT`). `claude-sonnet-5-5` or
`claude-opus-5-5` are the escalation knobs — expose via `WORKBENCH_LLM_MODEL`,
don't hardcode a change. Haiku 5.5 can decline a request (`stop_reason: "refusal"`),
which surfaces as a "failed" extraction.

---

## 7. Tests & acceptance

- **Mock the `anthropic` client** (no network in CI): feed a canned `ManuscriptFacts`
  and assert (a) the request carries the cached system block, the `already_have`
  list, and the delimited document block; (b) `_to_contract` produces valid
  `metadata` + `events` in the plumbing's shape; (c) `is_enabled()==False`
  short-circuits to `{"status":"skipped",...}`; (d) an `anthropic.APIError` yields
  `{"status":"failed",...}` — never a raise.
- **Post-parse validation:** confidence clamped to [0,1]; coordinates in range;
  Sand+Silt+Clay ≈ 100 when all three present (warn, don't drop).
- **Manual smoke (opt-in, real key):** one open-access paper end-to-end → sensible,
  quote-backed suggestions for location, soil properties, crop/tillage, and ≥1 dated
  field operation.

Acceptance: dropping this module in makes the plumbing's Phase B/C pre-fill go live,
with no change to the plumbing's contract or GUI.

---

## 8. Cost (per submission, one call)

Full paper ≈ 7k–20k input tokens; small output (plus thinking). `claude-haiku-5-5`
(≤100K-token prompts: $0.10 / $0.50 per MTok) is well under 1¢ per paper — once per
submission, human-in-the-loop. Negligible at expected volume; the model is a config knob.
