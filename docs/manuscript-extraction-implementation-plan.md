# Source-Document Ingestion & Wizard Integration — Implementation Plan (plumbing + GUI)

**Audience:** the engineer/model building the ingestion pipeline, storage, and UI.
Self-contained for that scope — you should not need prior conversation context.

**This plan is split in two.** The single part that calls a language model — reading
an uploaded document and returning structured suggestions — is specified separately
in [manuscript-llm-module.md](manuscript-llm-module.md), behind a small, fixed
interface (§5). **Everything in this document is ordinary web-app work** (Django
models, file parsing, storage, a wizard step, endpoints, tests) and can be built and
fully tested against a stub of that interface. The language-model module is a
drop-in you wire in last.

**Companion:** [data-upload-workbench-roadmap.md](data-upload-workbench-roadmap.md)
defines the wider workbench — the upload wizard, the `UploadSubmission` envelope, and
the staging/review flow referenced below.

**Domain (brief, neutral).** This is tooling for a public agricultural
field-research database. A contributor uploads a data file and, optionally, the
scientific paper the data came from. The values read from that paper are ordinary
study metadata — trial location, soil physical properties, crop and management
practices — used to **pre-fill review forms the contributor then confirms**. Nothing
is written to the database without confirmation.

---

## 0. What this builds

1. Attach a **source document** (PDF, DOCX, or pasted text) to a submission.
2. Extract its plain text up front and store it.
3. Run cheap **deterministic parsers** (DOI, geographic coordinates) on the text.
4. Call the **extraction module** (§5) to get structured, provenance-tagged
   suggestions.
5. Render those suggestions as **editable, badged controls** in the wizard's review
   forms and event timeline. Confirming commits them; nothing auto-saves.

**Non-negotiable principles**
- **Graceful degradation.** No model key configured, or the module errors → text is
  still captured, deterministic parsers still run, and the review forms simply start
  empty. The module is never a hard dependency and never blocks a submission.
- **Suggestions, never facts.** Everything is pre-filled and badged; the contributor
  confirms or edits before anything is saved.
- **Provenance.** Every suggestion carries the verbatim `source_quote` it came from,
  so the contributor (and later a reviewer) can check it against the stored text.

---

## 1. Where it slots into the wizard

```
Stage 0  Upload ─────────────┐
  data file(s) ──────────────┼─► [existing] parse → column mapping
  source document ───────────┘
        │
        ▼
  §3 extract_text()   (fast, deterministic, always runs) → ManuscriptSource.extracted_text
        │
        ▼
  §4 deterministic parsers (DOI, coordinates)  +  §5 extract_facts()  (opt-in module)
        │  merged into UploadSubmission.manuscript_facts (JSON)
        ▼
Stage 4  Review forms      ◄── pre-filled from manuscript_facts.metadata
Stage 5  Event timeline    ◄── pre-filled from manuscript_facts.events
```

Run `extract_text()` synchronously on upload (fast). Run the module's
`extract_facts()` once, when the contributor first reaches the review stage (so it
can be told which fields the tabular data already supplies, and so early abandoners
never trigger it). Cache the result on the submission; offer a "re-run" button.

---

## 2. Data model changes

Add to `n2odb/models_upload.py` (the module the roadmap introduces for
`UploadSubmission`). Both are `managed=True` (default) → one migration. Run
`makemigrations`/`migrate` **inside the app container**, not on the host.

### 2.1 `ManuscriptSource`

```python
class ManuscriptSource(models.Model):
    KIND = [("pdf", "PDF"), ("docx", "DOCX"), ("text", "Pasted text")]
    STATUS = [("pending", "Pending"), ("extracted", "Extracted"),
              ("failed", "Failed")]

    submission        = models.ForeignKey("UploadSubmission", on_delete=models.CASCADE,
                                          related_name="documents")
    kind              = models.CharField(max_length=8, choices=KIND)
    original_name     = models.CharField(max_length=255, blank=True)
    sha256            = models.CharField(max_length=64, blank=True)
    size_bytes        = models.IntegerField(null=True, blank=True)
    # Original bytes: DB blob for MVP (Railway FS is ephemeral; no MEDIA/S3 set up).
    # Null for pasted text. Swap to a volume/S3 path later if re-download is wanted.
    file_bytes        = models.BinaryField(null=True, blank=True)
    extracted_text    = models.TextField(blank=True)
    page_count        = models.IntegerField(null=True, blank=True)
    char_count        = models.IntegerField(null=True, blank=True)
    extraction_status = models.CharField(max_length=10, choices=STATUS, default="pending")
    extraction_error  = models.TextField(blank=True)
    created_at        = models.DateTimeField(auto_now_add=True)
```

### 2.2 `UploadSubmission` additions

```python
    # Opaque JSON produced by the extraction module (§5). The plumbing does not
    # interpret its contents — it renders them. Shape is the §5 contract.
    manuscript_facts        = models.JSONField(default=dict, blank=True)
    manuscript_extracted_at = models.DateTimeField(null=True, blank=True)
```

---

## 3. Text-extraction service — `n2odb/services/manuscript_text.py`

Pure, deterministic, no network. Dispatch on `kind`.

```python
@dataclass
class ExtractedText:
    text: str
    page_count: int | None
    char_count: int

def extract_text(kind: str, data: bytes | str, *, filename: str = "") -> ExtractedText: ...
```

- **PDF** — try `pdfplumber` first (better multi-column/layout), fall back to `pypdf`
  if it errors or yields near-empty text. Join pages with `\n\n`; record `page_count`.
- **DOCX** — `python-docx`: join paragraph text **and** walk `document.tables` (site
  characteristics are often tabular). `page_count = None`.
- **Pasted text** — passthrough.

**Normalization (`_normalize`), applied to all three:** de-hyphenate line breaks
(`r"(\w)-\n(\w)" → r"\1\2"`); join intra-paragraph single newlines to spaces while
keeping blank-line paragraph breaks; collapse whitespace; strip control chars;
NFC-normalize. Output must stay human-readable for the document panel (§7).

**Guards & errors:** enforce `WORKBENCH_DOC_MAX_MB` / `WORKBENCH_DOC_MAX_PAGES`
(§8) before parsing; wrap parsing in try/except; on failure set
`extraction_status="failed"` + `extraction_error` and return empty text — **never
raise into the request.**

**OCR (Phase C, optional):** if a multi-page PDF yields `< N` chars it is likely
scanned images. Behind `WORKBENCH_OCR_ENABLED`, run `pdf2image` → `pytesseract`.
This needs system packages (`tesseract-ocr`, `poppler-utils`) in the `Dockerfile` —
call it out in the PR; don't add it silently.

**Licensing:** use `pdfplumber` (MIT), `pypdf` (BSD), `python-docx` (MIT). **Do not**
add PyMuPDF/`fitz` — it's AGPL, and this project is heading toward open-source
release.

---

## 4. Deterministic parsers — `n2odb/services/manuscript_regex.py`

Cheap, reliable, and a cross-check on the module's output. Run regardless of the
module.

- **DOI:** `r"10\.\d{4,9}/[-._;()/:A-Z0-9]+"` (case-insensitive) → publication link.
- **Coordinates:** parse decimal (`-33.87`, `151.21`) and DMS (`33°52'11"S`,
  `151°12'30"E`) into decimal degrees, handling N/S/E/W suffixes and "lat, long"
  ordering. → site latitude/longitude.

Return each as `{value, source_quote, source: "regex"}` and merge into
`manuscript_facts.metadata` (§5 shape). When a deterministic parser and the module
both propose a value, prefer the deterministic number, keep the module's
`source_quote`, and flag a mismatch if they disagree beyond a tolerance.

---

## 5. The extraction seam — the interface you build against

This is the boundary between this plan and the language-model module. **Build and
test everything here against the contract and stub below; the real implementation
lives in [manuscript-llm-module.md](manuscript-llm-module.md).**

### 5.1 The two functions

```python
# n2odb/services/llm_assist.py   (the shared model seam; also used elsewhere)
def is_enabled() -> bool:
    """True only if a key is configured and the feature flag is on."""

# n2odb/services/manuscript_extract.py
def extract_facts(text: str, already_have: list[str]) -> dict:
    """Return the §5.2 contract. Must never raise: on any failure return
    {"status": "failed", "metadata": {}, "events": []}."""
```

### 5.2 Return contract (render the GUI against exactly this shape)

```jsonc
{
  "status": "ok" | "skipped" | "failed",
  "metadata": {                         // keyed by review-form field id (§6)
    "<field_id>": {
      "value": <string|number|null>,
      "unit":  <string|null>,           // as reported; the wizard's unit layer converts
      "source_quote": <string|null>,    // verbatim snippet from the document
      "confidence":   <number 0..1|null>
    }
    // ...only keys the module actually found
  },
  "events": [                           // dated field operations (rendered generically)
    {
      "type": "planting" | "harvest" | "tillage" | "irrigation" | "input_application",
      "date_iso":  <string|null>,
      "date_text": <string|null>,       // as stated ("early May 2018")
      "fields": [                       // module-supplied; GUI renders each generically
        { "key": <string>, "label": <string>, "value": <any|null>, "unit": <string|null> }
      ],
      "source_quote": <string|null>,
      "confidence":   <number 0..1|null>
    }
  ],
  "model": <string>, "generated_at": <iso8601>, "usage": {...}
}
```

**Key point for this plan:** the GUI treats `metadata` values and `events[].fields`
**generically** — it renders each returned entry against its form control (metadata)
or as label/value/unit inputs (events) and does not need to interpret their meaning.
That is what keeps this document free of any domain schema; the schema lives entirely
in the module.

### 5.3 Development stub (no model needed)

```python
def extract_facts(text, already_have):
    if not llm_assist.is_enabled():
        return {"status": "skipped", "metadata": {}, "events": []}
    # Real implementation: manuscript-llm-module.md
    raise NotImplementedError
```

For tests and for building the GUI, ship a fake that returns a saved fixture in the
contract shape (a couple of metadata keys + one event). The whole review-form/
timeline pre-fill can be built and verified against that fixture.

---

## 6. Feeding the wizard (generic rendering + provenance)

The review-form **field-id registry** (the keys the module may return in
`metadata`). These correspond to fields the roadmap already defines on
`Publication` / `Site` / `Treatment`; the GUI just needs the id → control mapping:

```
pub_title, pub_lead_author, pub_citation, pub_year, pub_doi, pub_abstract,
site_latitude, site_longitude, site_name, country, map, mat,
sand, silt, clay, texture_class, ph, bulk_density, som,
primary_crop, secondary_crop, cover_crop, tillage, management, flux_instrument
```

1. **Review forms (Stage 4).** For each returned `metadata[field_id]`, pre-fill the
   matching control when it isn't already satisfied by the tabular mapping. Show a
   **"from document"** badge, colour it by `confidence` (green/amber), and put the
   `source_quote` in a tooltip. The value stays editable; **contributor confirmation
   is what commits it** to the submission draft.
2. **Event timeline (Stage 5).** Seed the timeline with each `events[]` entry as an
   editable card: date + type + its `fields` rendered as generic label/value/unit
   inputs. On save, confirmed events are written onto the matching measurement
   date(s) per the roadmap's timeline editor.
3. **Provenance panel.** Clicking a badge scrolls the document-text panel (§7) to the
   `source_quote` and highlights it. This is the trust feature — it lets a reviewer
   verify any suggestion against the exact wording in the paper.

Everything stays a **suggestion on the draft**. Staging and promotion are the
roadmap's later stage.

---

## 7. UI additions

Server-rendered Django + Bootstrap 5 + a light reactive layer (match whatever the
wizard shell uses — Alpine.js per the roadmap, or jQuery to match the existing app).

- **Stage 0 template:** add a document dropzone (PDF/DOCX) **and** a "paste text"
  textarea beside the data-file dropzone. Copy: "Optional but recommended — attach
  the paper and we'll pre-fill the study details for you."
- **Post-upload chip:** after `extract_text()`, show "Document captured — N pages,
  ~M words." After `extract_facts()`, show "Found K suggestions from your document."
- **Document panel:** a collapsible panel/tab showing `extracted_text` (read-only,
  scrollable) — the target for provenance highlighting. Include a "re-run" button.
- **Endpoints** (`n2odb/urls.py` + views), scoped by the submission's `public_token`
  (roadmap §3.1), CSRF as the rest of the app:
  - `POST /workbench/<token>/document/`         → create `ManuscriptSource`, run
    `extract_text()`, return status + counts.
  - `POST /workbench/<token>/document/extract/` → run/re-run `extract_facts()`,
    return `manuscript_facts`.
  - `GET  /workbench/<token>/document/text/`     → serve `extracted_text` for the panel.

---

## 8. Settings & dependencies

**`requirements.txt` (new):**
```
pdfplumber
pypdf
python-docx
anthropic            # used only by the module (§5); safe to install now
# Phase C OCR only (also add tesseract-ocr + poppler-utils to the Dockerfile):
# pytesseract
# pdf2image
```

**`fluxapp/settings.py`** (via `python-decouple`'s `config(...)`, matching the
existing style):
```python
WORKBENCH_LLM_ENABLED  = config("WORKBENCH_LLM_ENABLED", default=False, cast=bool)
WORKBENCH_DOC_MAX_MB    = config("WORKBENCH_DOC_MAX_MB", default=32, cast=int)
WORKBENCH_DOC_MAX_PAGES = config("WORKBENCH_DOC_MAX_PAGES", default=60, cast=int)
WORKBENCH_OCR_ENABLED   = config("WORKBENCH_OCR_ENABLED", default=False, cast=bool)
# Model-call settings (WORKBENCH_LLM_MODEL, ANTHROPIC_API_KEY, …) belong to the
# module — see manuscript-llm-module.md §Settings.
```

---

## 9. Security & robustness

- **Untrusted files:** enforce size/page limits before parsing; sniff type, don't
  trust `content-type`; wrap parsing in try/except; cap `BinaryField` storage or move
  originals to a volume if documents are large. A bad/scanned/oversized file must
  degrade to a friendly message, never a 500.
- **Untrusted text:** the extracted text is contributor-supplied. The plumbing only
  stores and displays it; the module (§5) is responsible for how it is handed to the
  model. Escape it when rendering the document panel.
- **Privacy:** the document panel and stored text stay within the app; the module's
  network behaviour is documented in its own spec and is opt-in via
  `WORKBENCH_LLM_ENABLED`.

---

## 10. Testing & acceptance

**Unit (CI, no network):**
- `manuscript_text`: fixtures for a small text-based PDF, a DOCX with a table, and
  pasted text → assert non-empty normalized text, correct `page_count`,
  de-hyphenation. A deliberately broken PDF → asserts `failed` status, no raise.
- `manuscript_regex`: DOI + decimal + DMS coordinate fixtures (incl. S/W
  hemispheres, "lat, long" order) → correct decimal degrees + snippet.
- **Seam:** with the §5.3 stub returning `{"status":"skipped",...}` and with a
  fixture fake returning a full contract object → assert the review-form pre-fill and
  event-timeline pre-fill render the badges, tooltips, and generic event inputs
  correctly. **No live model call in CI.**

**Acceptance**
- `WORKBENCH_LLM_ENABLED=False`: upload works, text captured and shown, DOI/coords
  auto-suggested, forms start empty — no errors anywhere.
- With a fixture fake wired in: review forms and timeline arrive pre-filled with
  badged, quote-backed, editable suggestions; nothing commits without confirmation.
- Corrupt/scanned/oversized document → clear message, never a 500.

---

## 11. Phasing

- **Phase A — Capture (no model).** Models + migration; `manuscript_text.py`;
  `manuscript_regex.py`; Stage-0 dropzone + paste; document panel; the three
  endpoints; the §5 seam + stub. *Done = document text stored + shown; DOI/coords
  auto-suggested; GUI renders suggestions from the fixture fake.*
- **Phase B — Wire the module.** Drop in the real `extract_facts()` from
  [manuscript-llm-module.md](manuscript-llm-module.md); review-form pre-fill goes
  live. *Done = study metadata pre-filled from the paper.*
- **Phase C — Events + fidelity.** Event-timeline pre-fill; native-PDF input
  (module option); OCR for scanned PDFs. *Done = dated field operations proposed.*

Phase A is entirely within this document and has **no** dependency on the module.
