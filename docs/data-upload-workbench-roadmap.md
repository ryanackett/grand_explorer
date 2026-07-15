# Assisted Data Upload Workbench — Implementation Roadmap

A guided, "dummy-proof" workbench that lets a scientist drop in arbitrary CSV/Excel
files of N₂O flux data and walk out having contributed a clean, reviewed study to
GRAND — with smart ontology/unit suggestions, gap-filling for fields that aren't in
the file, a staging + manager-review gate, and optional external augmentation.

Status: design/roadmap. Nothing here is built yet. The `submit_data` view
(`n2odb/views.py`) is a stub and the nav link is commented "coming soon."

**Companion specs (source-document feature, split for hand-off):**
[manuscript-extraction-implementation-plan.md](manuscript-extraction-implementation-plan.md)
covers the plumbing + GUI (attach a PDF/DOCX/pasted paper at Stage 0, extract its
text, store it, and pre-fill the Plane B / Plane C review forms), all built against a
fixed interface; [manuscript-llm-module.md](manuscript-llm-module.md) covers the one
language-model step behind that interface. It plugs into Stage 0 and Stages 4–5 below.

---

## 1. Feasibility verdict (read this first)

**Feasible, and cheaper than it looks — because the hard back-end pieces already
exist in this repo.** The new work is mostly a guided front-end plus a staging
layer. What we already have to build on:

| Requirement | Already exists | Location |
|---|---|---|
| Build the Pub→Site→Exp→Treatment→Measurement hierarchy with dedup | `import_rawdata` | `n2odb/management/commands/import_rawdata.py` |
| Type coercion (float/int/bool/date/null-ish, string clipping) | helper fns | same file, top |
| Soil texture/chem augmentation (SoilGrids fallback, reversible) | `apply_soil` | `n2odb/management/commands/apply_soil.py` |
| Climate MAP/MAT augmentation (NASA POWER) | `apply_climate` | `.../apply_climate.py` |
| Country/Continent from lat/lon (offline reverse-geocode) | `backfill_geography` | `.../backfill_geography.py` |
| Outlier / hot-moment QC (pyod) | `hot_moment_detection` | `.../hot_moment_detection.py` |
| Time-series JSON for flux charts | `get_graph_json` | `n2odb/views.py` |
| Bulk review/edit UI for managers | `django-import-export` admin | `n2odb/admin.py` |
| Charts | Plotly 2.27 (already loaded) | `n2odb/templates/data-cart.html` |
| Date picker component | Mobiscroll (vendored) | `n2odb/static/js/` |

Models are **`managed=True`** (real migrations through `0010`), so staging tables
are just normal Django models + one migration. The canonical ingestion target is
**`RawMeasurementTreatment`** — `import_rawdata` writes
`Publication → Site → Experiment → Treatment → RawMeasurementTreatment` and skips
the normalized `Replication`/`RawMeasurement` path. The workbench should target the
same table.

The **augmentation stretch goal is ~80% done** — the three enrichment commands are
already reversible, fills-only, and idempotent. They just need to be callable
per-submission against staged rows with a user opt-in toggle.

### Biggest risks / honest caveats

1. **No file storage configured.** Settings only wire whitenoise for *static*
   files; there's no `MEDIA_ROOT`/S3, and Railway's filesystem is ephemeral.
   → **Decision: store parsed content as a compressed JSON blob on the submission
   row in the DB** (survives restarts, no new infra). Optionally keep original file
   bytes in a Railway Volume or S3 only if re-download is needed. DB-blob for MVP.
2. **Excel is hostile**: merged cells, multi-row headers, a units row under the
   header, notes rows, several tables per sheet. Robust "find-the-table + find-the-
   header-row" detection is real work; always provide a manual override.
3. **Multi-file / multi-sheet joins** are the hardest *data* problem — relating a
   "site info" sheet to a "daily flux" sheet needs a key the user may not have made
   explicit. Solve with a visual "link these" step + a "apply to all rows" fallback.
4. **The ×10/×100 unit-error class** (pH×10, % vs fraction) — catch with value-range
   plausibility checks, not just header parsing.
5. **LLM cost/privacy** — keep any Claude assist optional/opt-in and off the
   critical path; deterministic layers handle the majority.

---

## 2. The one architectural insight everything hinges on

User-submitted data splits into **three planes**, and this split falls straight out
of the existing schema. Getting this right is the whole game:

| Plane | What it is | In the file as… | Maps to | Collected via |
|---|---|---|---|---|
| **A. Time-series** | per-date measurements | **columns/rows** | `RawMeasurementTreatment` (date, n2oflux, vwc, wfps, soilt, airt, precip, soilnh4, soilno3…) | **column mapping** |
| **B. Study metadata** | one-per-study facts | prose, filename, a stray cell, or the user's head | `Site` (lat/lon, MAP, MAT), `Treatment` (crop, tillage, Sand/Silt/Clay, pH, BD, SOM), `Experiment`, `Publication` | **guided forms** |
| **C. Events** | sparse per-date interventions | rarely a column; described in prose (e.g. "fertiliser applied before sowing") | per-date fields on `RawMeasurementTreatment` (`nitrogenapplied`, `nitrogenform`, `nitrogentype`, `tillage`, `harvest`, `irrigationapplied`, `plantedcrop`) | **timeline editor** |

This is exactly the user's requirement — "coordinates and texture are almost always
in text, not tabular" (Plane B) and "notice `nitrogenapplied` is missing and let
users mark fertilization events" (Plane C). The schema *already* models N-applied as
a per-date column that's null except on application dates, so the timeline editor
just writes values onto the right rows. **Do column mapping first (Plane A), then
use the schema's required/important set to "notice" what's missing and route it to
Plane B forms or the Plane C timeline.**

---

## 3. Data model additions

Two groups: a **submission envelope** (the durable draft) and **staging mirror
tables** (the reviewable copy of the six live tables).

### 3.1 Submission envelope — `n2odb/models_upload.py`

```
UploadSubmission
  id, public_token (uuid, for resumable links)
  owner            FK User (nullable if anonymous — see decisions)
  contributor_email, contributor_name
  status           draft → submitted → in_review → changes_requested → approved → promoted → rejected
  created / updated / submitted_at
  # everything below is JSON during editing — cheap, no schema churn per keystroke
  parsed_content   JSONField        # normalized sheets: {sheet: {headers, rows, dtypes}}  (compressed)
  structure        JSONField        # per-sheet role: timeseries | metadata | dictionary | ignore
  mapping          JSONField        # column -> {canonical_field, unit, confidence, source}
  metadata_values  JSONField        # Plane B form answers (site/treatment/pub)
  events           JSONField        # Plane C: [{date, type, amount, form, ...}]
  augmentation     JSONField        # opt-in toggles + resolved values + provenance
  review_notes     JSONField        # manager <-> contributor thread
  qc               JSONField        # pyod flags, validation summary

UploadedFile
  submission FK, original_name, sha256, size, bytes (blob or volume path)
```

### 3.2 Staging mirror tables — `Staging{Publication,Site,Experiment,Treatment,RawMeasurementTreatment}`

Each mirrors its live counterpart's columns **plus** `submission_id` FK and a
row-level `review_status`. Because they're field-identical, managers review them
with the **same import-export admin** they already use, and promotion is a plain
field-copy insert. Generate them from the live models to avoid drift (a small
factory or an explicit copy — keep them in one module and add a test asserting
column parity with the live models).

Why real tables and not "just JSON until promote"? Two reasons: (1) reviewers get
the existing admin/query/export tooling for free; (2) you can run the real pyod QC
and even preview charts against staged rows using `get_graph_json` generalized to a
staging queryset.

**Editing lives in JSON on `UploadSubmission`; materialization to staging happens
once, at "Submit for review."** This keeps the interactive phase fast and
schema-stable, and gives reviewers strongly-typed rows.

---

## 4. The pipeline (wizard stages)

```
0 Upload & parse ─ 1 Detect structure ─ 2 Map columns ─ 3 Units ─ 4 Gap-fill
  ─ 5 Events ─ 6 Assemble hierarchy ─ 7 Review (table+charts+QC) ─ 8 Augment (opt-in)
  ─ 9 Submit → Staging → Manager review → Promote to live
```

**0 · Upload & parse.** Accept multiple CSV/XLSX. Parse with pandas +
`openpyxl`; sniff encoding with `charset-normalizer`, delimiter with `csv.Sniffer`.
Detect header row and data start heuristically (first row where cells are mostly
non-numeric strings followed by rows that are mostly numeric); always expose a
"header is row N, data starts row M" manual override. Persist normalized sheets to
`parsed_content`. Handle multiple tables per sheet by blank-row segmentation.

**1 · Detect structure.** Classify each sheet: *timeseries* (has a date-like column
+ numeric series), *metadata* (few rows, many one-off descriptors), *dictionary*
(looks like a legend/codebook — use it to enrich the alias map!), or *ignore*. Show
the guess with a one-click correction.

**2 · Map columns (Plane A).** The core UX. Per column: canonical-field suggestion +
confidence + live sample values (see §5, §6).

**3 · Units.** Per mapped numeric column: inferred unit + conversion, with a live
before/after preview and plausibility warnings (§5).

**4 · Gap-fill (Plane B).** Compare mapped fields against the **required/important
set** (below). Anything missing becomes a friendly task with a small form:
coordinates (with a parser + map-pin confirm), soil texture (numbers *or* a USDA
class dropdown → Sand/Silt/Clay), pH, bulk density, crop, tillage, publication/DOI.

**5 · Events (Plane C).** A timeline of the study's date range with an "add event"
action (fertilization / tillage / harvest / irrigation / planting). Fertilization
captures amount + form + type; on save it writes those values onto the matching
`RawMeasurementTreatment` date(s). Mobiscroll for date entry.

**6 · Assemble hierarchy.** Group rows into Treatments/Reps. Simplest studies = one
treatment (auto). If a "treatment"/"plot" column exists, offer to split by it.

**7 · Review.** Paginated data table + Plotly flux time-series (reuse
`get_graph_json`'s shape) + pyod QC badges on anomalous points. Plain-language
summary: "This will create 1 site, 1 experiment, 3 treatments, 412 measurements."

**8 · Augment (opt-in).** Checkboxes for SoilGrids texture, NASA POWER MAP/MAT,
reverse-geocode Country/Continent. Runs the existing enrichment logic against the
draft; shows each filled value badged by source with per-field opt-out.

**9 · Submit → review → promote.** Materialize to staging; notify managers; manager
approves/requests-changes/rejects; approval runs the shared ingest service to upsert
into live tables in one transaction with business-key dedup.

### Required / important field set (drives "noticing")

- **Hard-required:** a date, N₂O flux, site latitude+longitude, primary crop.
- **Important (warn, augmentable):** MAP, MAT, soil texture, pH, bulk density,
  N applied, publication/DOI.
- **Nice-to-have:** WFPS/VWC, soil/air temp, precip, NH₄/NO₃, irrigation.

---

## 5. The suggestion engine (deterministic-first, LLM-optional)

`n2odb/services/suggest.py`. Layer it — cheapest/highest-precision first, LLM only
for the residual:

1. **Normalize** header: lowercase, strip units-in-parens, split camel/snake,
   unaccent, drop punctuation.
2. **Alias dictionary** (highest precision): curated map of known variants →
   canonical field, e.g. `{n2o, n2o_flux, nitrous oxide flux, f_n2o} → n2oflux`.
   Seed it from your own scraped corpus + any uploaded *data dictionary* sheets.
3. **Fuzzy match** (`rapidfuzz.token_set_ratio`) against canonical names + aliases.
4. **Value-based inference** — sniff the column's values when headers are useless
   ("Column1"): datetime-parseable? numeric range consistent with a known field
   (WFPS 0–100, pH 3–10, lat −90..90, flux ≥ ~0)? This disambiguates and also
   validates header guesses.
5. **Unit inference** — from header tokens (`kg/ha`, `g N m-2`, `ppm`, `°C/°F`,
   `%`) + value magnitude → canonical unit + conversion factor + plausibility flag.
6. **LLM assist (opt-in, residual only)** — Claude with a structured tool-use schema
   for (a) columns still low-confidence after 1–5 and (b) free-text extraction:
   pull coordinates, a soil texture class, or a dated field operation out of a
   methods paragraph the user pastes. The deterministic layer still validates
   whatever the LLM returns (e.g. texture class → Sand/Silt/Clay via USDA triangle;
   coords → range check).
7. **Confidence → UI**: **green** (auto-applied, collapsed) · **yellow** (pre-filled,
   confirm) · **red** (needs you). Never hard-block.

Dependencies to add: `rapidfuzz`, `charset-normalizer`, `openpyxl`. LLM optional via
the `anthropic` SDK (latest Claude models).

---

## 6. UI/UX — the #1 priority

Stack: server-rendered Django templates + **Bootstrap 5** (already in base) + a
light reactive layer. Recommend **Alpine.js** (≈15 kB, no build step) for the
mapping/units reactivity rather than more jQuery — it keeps the two-panel mapping
screen declarative. HTMX is a fine alternative for step navigation + autosave.

Design principles (dummy-proofing):

- **Wizard with a persistent progress rail**, you-are-here, and **autosave every
  step** — the submission is a durable draft, so "leave and come back" always works
  (resumable via `public_token`).
- **Never block; always default.** Every suggestion is pre-applied; the user edits
  by exception. Missing required fields are friendly tasks, not red errors.
- **Dual labels everywhere:** plain-language + scientific term + unit, e.g.
  "Water-filled pore space (WFPS, %)". Tooltip = one-line definition + example value.
- **Always show the data.** Every mapping decision shows 3–5 live sample values +
  a mini distribution, so a non-expert sanity-checks by eyeballing.
- **Traffic-light completeness board**, always visible: the required canonical
  fields colored by status. This *is* the "noticing" mechanism — unmet items become
  guided fill-ins (Plane B) or timeline prompts (Plane C).
- **Unit chips with instant preview:** pick a unit, watch sample values convert
  live; warn on implausible magnitudes ("these pH values look ×10").
- **Escape hatches on every field:** "not sure / skip for now / not relevant /
  store as free-text note." Nothing forces a wrong answer.
- **Plain-language validation:** "3 rows have a flux but no date — view them?" with
  click-to-inspect, not a wall of errors.
- **Provenance on the final review:** color each value by origin — from file /
  you entered / auto-augmented — with per-field opt-out on augmented values.

Screens: Upload · Structure · **Mapping** (two-panel: source columns ↔ canonical
fields, drag or dropdown, confidence colors, live samples) · Units · Completeness &
gap-fill forms · Timeline · Review (table + Plotly + QC) · Augmentation · Submit/
confirm · Status tracker.

---

## 7. Reuse map (do NOT fork logic)

- **`import_rawdata` → `n2odb/services/ingest.py`.** Extract the coercion helpers
  (`to_float/to_int/to_bool_flag/to_date/norm/clip`) and the hierarchy/dedup builder
  into a service used by *both* the CLI command and the workbench's promote step.
  One source of truth for how a study becomes rows.
- **`apply_soil` / `apply_climate` / `backfill_geography` →
  `n2odb/services/enrichment.py`.** Wrap their cores so they can run against one
  submission's staged rows, gated by opt-in. Already reversible/fills-only.
- **`hot_moment_detection` (pyod) →** run on staged flux series as an auto QC pass;
  surface flagged points in review for both contributor and manager.
- **`get_graph_json` →** generalize to accept a staging queryset for review charts.
- **`django-import-export` admin →** manager bulk review/approve tooling on the
  staging tables.
- **Plotly 2.27 & Mobiscroll →** already vendored; reuse for charts & date entry.

---

## 8. Manager review & promotion

- **Review dashboard** (custom admin view or a staff-only page): queue of submissions
  by status; per-submission diff of staging vs. existing (does this site/pub already
  exist?); the same table+chart+QC review the contributor saw.
- **Actions:** approve · request changes (thread back to contributor, status →
  `changes_requested`, draft re-opens) · reject.
- **Promote** = shared ingest service inside `transaction.atomic`, with normalized
  business-key dedup so re-submitting or overlapping a known study doesn't
  double-insert. Record `promoted` + created live IDs on the submission for audit.
- **Notifications:** email via the already-configured `DEFAULT_FROM_EMAIL`
  (contributor on status change; managers on new submission).

---

## 9. Phased delivery

**Phase 0 — Plumbing.** `UploadSubmission` + staging mirror models + migration;
robust upload/parse (pandas/openpyxl/charset-normalizer) → normalized JSON in DB;
wizard shell with autosave/resume; auth gate; refactor `import_rawdata` into
`services/ingest.py`. *Done = upload a file, it persists, you can resume.*

**Phase 1 — Single-file happy path (core loop).** Suggestion engine layers 1–5;
mapping UI (confidence colors + live samples); unit detection/conversion;
completeness board + Plane B gap-fill forms; tabular review; submit → staging;
admin approve → promote via shared service. *Done = a clean single CSV goes
end-to-end to the live DB via manager approval.*

**Phase 2 — Events + free-text + multi-sheet.** Timeline/event editor (Plane C);
coordinate parser + free-text metadata extraction (deterministic; LLM optional);
multi-sheet within one workbook. *Done = studies where N-applied and coords aren't
tabular still work.*

**Phase 3 — Multi-file join + augmentation + graphical review + LLM assist.** Visual
"link files" step; augmentation opt-in wired to `services/enrichment.py` with
per-field badges/opt-out; Plotly review + pyod QC; optional Claude assist for
residual headers/free-text. *Done = full stretch vision.*

**Phase 4 — Reviewer & polish.** Review dashboard with diff + bulk actions;
change-request thread; email notifications; audit trail; contributor status tracker.

---

## 10. Decisions to confirm (with recommended defaults)

1. **Auth for submitters** — *Recommend:* require a lightweight account (email +
   password or magic link) so drafts have an owner, resume is secure, and spam is
   controllable. Anonymous-with-email is possible but complicates resume/abuse.
2. **LLM assist** — *Recommend:* ship Phases 0–2 fully deterministic; add Claude as
   an **opt-in** helper in Phase 3 for residual ambiguous headers + free-text
   extraction only. Keeps cost/privacy controlled and the critical path offline.
3. **Ingestion target** — *Recommend:* target `RawMeasurementTreatment` only (matches
   `import_rawdata`); treat the `Replication`/`RawMeasurement` normalized path as
   out of scope for the workbench.
4. **File retention** — *Recommend:* store normalized JSON in DB (MVP); keep original
   bytes only if you want contributor re-download — then a Railway Volume or S3.
5. **Who are "managers"** — *Recommend:* `is_staff` users via a dedicated review
   dashboard; no new role system needed initially.
```
