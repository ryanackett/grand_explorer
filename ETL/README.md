# n2odb ETL — Loading & Publication Enrichment

This directory holds the ETL that brings the Dorich Global N₂O dataset into the
`n2odb` Django schema, plus the publication-metadata enrichment that backfills
`Publication.Link`, `.Abstract`, `.Citation`, and `.PubTitle`.

The pipeline spans **two repositories**:

| Stage | Repo | Tool |
|---|---|---|
| 1. Transform raw exports → per-table CSVs | `DorichScraper` | `build_tables.py` (see its README) |
| 2. Load CSVs → MySQL (FK order, reversible) | `fluxapp` | `manage.py load_tables` |
| 3. Enrich publications (links / abstracts / citations) | `fluxapp` | `manage.py apply_enrichment` |

Everything writes through reversible, transactional management commands that
emit an **undo manifest**, so any step can be rolled back without harming the
schema, migrations, or pre-existing data.

---

## 1. Transform (DorichScraper)

`DorichScraper/build_tables.py` streams the raw EDI exports (`DailyGHG_V1.csv`,
`Sitelibrary_V1.csv`, `Summary_V1.csv`) and emits one normalized CSV per table
into `DorichData/cleaned/`: `Publication.csv`, `Site.csv`, `Experiment.csv`,
`Treatment.csv`, `RawMeasurementTreatment.csv`. See the DorichScraper README for
the join logic, enrichment rules (WFPS↔VWC, NitrogenForm→NitrogenType, gap-fill,
treatment-averaging), GRACEnet dropping, and surrogate-key offsetting.

The CSV column headers are the **db_column** names (CamelCase: `N2OFlux`,
`RawTreatmentID`, `pH`), not the lowercase Django field names.

## 2. Load — `manage.py load_tables`

```bash
# inside the fluxapp devcontainer (reads .env -> DATABASE_URL -> host `db`)
python manage.py load_tables <dir-with-the-5-CSVs> --dry-run   # parse, insert, roll back
python manage.py load_tables <dir-with-the-5-CSVs>             # commit + write manifest
python manage.py load_tables --undo <manifest.json>            # reverse exactly
```

- Loads in FK order (Publication → Site → Experiment → Treatment →
  RawMeasurementTreatment) in **one atomic transaction**, using the explicit
  surrogate PKs/FKs from the CSVs.
- The CSV PKs are **offset above the live `MAX(pk)`** (the DorichScraper
  `etl.sh` reads those offsets first), so every loaded row occupies a known,
  isolated id range. `load_tables` refuses to load if that range is already
  occupied.
- The undo manifest records each table's `pk_min/pk_max/count`; `--undo`
  deletes exactly those ranges in reverse FK order and aborts if the live rows
  no longer match (so it can never clobber unrelated data).

> The end-to-end transform+load is also driven by `DorichScraper/etl.sh`
> (`LOAD_CMD=load_tables ./etl.sh`).

## 3. Enrich publications — `manage.py apply_enrichment`

`apply_enrichment` fills `Publication` fields from a reviewed CSV. Only the
columns present in the CSV are written; empty cells never overwrite anything.

```bash
python manage.py apply_enrichment ETL/<sheet>.csv [--overwrite] [--dry-run]
python manage.py apply_enrichment --undo ETL/enrich_manifest_<ts>.json
```

- Default is **fill-empty** (won't touch a field that already has a value).
- `--overwrite` replaces existing values too — needed only when *replacing* a
  value (e.g. swapping a GRACEnet umbrella citation for the individual-study
  one). The backup manifest still makes it fully reversible.
- Fields handled: `Link`, `Abstract`, `Citation`, `PubTitle` (length-capped to
  the model's `max_length`).

### The enrichment worksheets (committed here)

| File | Pubs | What it sets |
|---|---|---|
| `pub_enrichment.csv` | journal articles | `Link` + `Abstract` (DOI sourced from each pub's own citation) |
| `gracenet_candidates.csv` | the 32 GRACEnet/repository datasets | `Link` + `DOI` + `Abstract` + individual-study `Citation` |
| `title_fixes.csv` | pubs with a stored-title typo/truncation | corrected `PubTitle` (authoritative title from the DOI) |

**Apply order matters** (the two sheets overlap on PubIDs 17–48):

```bash
# 1. datasets first — authoritative, replaces the umbrella citations
python manage.py apply_enrichment ETL/gracenet_candidates.csv --overwrite
# 2. journal pubs — fill-empty (skips the datasets, whose Link is now filled)
python manage.py apply_enrichment ETL/pub_enrichment.csv
# 3. title corrections (review the 12 rows first; prune any you don't want)
python manage.py apply_enrichment ETL/title_fixes.csv --overwrite
```

`pub_enrichment.csv` proposes the **umbrella** DOI `10.15482/USDA.ADC/1235557`
as the Link for the GRACEnet pubs — applying gracenet first (with its correct
individual links) means fill-empty skips those, so the umbrella link never lands.

## 4. Treatment convention fixes — `manage.py apply_treatment_fixes`

The transform loaded Treatment rows before some conventions were applied. The
fixes (now also baked into `build_tables.py` for future runs) are captured in
`treatment_fixes.csv` (`TreatmentID, PrimaryCrop, FluxInstrument, Tillage, Source`):

- **FluxInstrument** → `Static Chamber` / `Automatic Chamber` / `Eddy Covariance`
  (the `flux gradient` micromet method is recorded as Eddy Covariance per convention).
- **PrimaryCrop** → `Scientific name (Common Name)` matching the existing rows,
  backfilled from `DorichData/CropYield_V1.csv` and, where flagged `Source=infer:*`,
  inferred from the linked publication (treatment-name hint or a single crop named
  in the abstract). 28 cryptic-coded treatments remain `Unknown` (no reliable source).
- **Tillage** → title-cased.

```bash
# review the Source=infer:* rows first, then (these are corrections, so --overwrite):
python manage.py apply_treatment_fixes ETL/treatment_fixes.csv --overwrite --dry-run
python manage.py apply_treatment_fixes ETL/treatment_fixes.csv --overwrite
python manage.py apply_treatment_fixes --undo ETL/treatment_manifest_<ts>.json
```

`treatment_fixes.csv` is keyed by the live `TreatmentID` (verified to match), and
empty CSV cells never overwrite (a blank Tillage won't null an existing value).

## 5. Soil & climate enrichment — `apply_soil` / `apply_climate`

Fills the soil physics/chemistry on `Treatment` (`SandMean`, `SiltMean`,
`ClayMean`, `pHMean`, `BulkDensityMean`, `SOMMean`) and `MAP`/`MAT` on `Site`.

**The values are already in the Dorich export** — `DorichData/Summary_V1.csv`
carries `Sand`/`Silt`/`Clay`/`pH`/`BD`/`soilC_perc` and `MeanPrec_mm`/`MeanTemp_c`
per (Reference, Treatment), which join the live DB on
`(Experiment.ExperimentName, Treatment.TreatmentName)`. These are site-level and
authoritative. `SOMMean` stores `soilC_perc` **as-is** (organic C %), not converted
to organic matter.

Where Summary has no value (coverage ~35–80%), and for legacy rows (1–334, which
have no Summary match), a **gridded fallback** fills the gap, flagged per field in
the `Source` column:

- **SoilGrids v2.0** (ISRIC) — 0–30 cm depth-weighted sand/silt/clay/pH/bulk
  density/SOC, by site lat/lon.
- **NASA POWER** climatology — annual `T2M` (MAT) and `PRECTOTCORR`×365.25 (MAP).

Texture stays coherent (Sand+Silt+Clay = 100) and single-provenance per triplet:
a full Summary triplet is used as-is (`Source=*:summary`); where Summary measured
only some components (e.g. clay), the measured ones are kept and the rest filled
from SoilGrids **rescaled** to the remaining fraction (`Source=*:soilgrids_scaled`);
with no Summary texture, a full SoilGrids triplet is used (`Source=*:soilgrids`).

Sites with invalid coordinates (e.g. the `(0,0)` null-island placeholder) are
**skipped** from the gridded fallback and reported — fix the lat/lon in `Site`,
then re-run `build_soil.py` to pick them up.

### Generate the worksheets — `ETL/build_soil.py`

Run from a host with Docker-socket access + outbound internet (queries the live
DB for join keys / current NULLs, then the gridded APIs once per site, cached to
`ETL/_grid_cache.json`). Dump the live state first:

```bash
# TSV dumps the generator reads (see the SQL in build_soil.py's docstring)
#   /tmp/treatments_dump.tsv  and  /tmp/sites_dump.tsv
python3 ETL/build_soil.py            # writes soil_enrichment.csv + site_climate.csv
python3 ETL/build_soil.py --offline  # re-emit from cache + Summary only (no network)
```

### Apply (reversible, fill-only)

```bash
python manage.py apply_soil    ETL/soil_enrichment.csv --dry-run
python manage.py apply_soil    ETL/soil_enrichment.csv
python manage.py apply_climate ETL/site_climate.csv --dry-run
python manage.py apply_climate ETL/site_climate.csv
# reverse:
python manage.py apply_soil    --undo ETL/soil_manifest_<ts>.json
python manage.py apply_climate --undo ETL/climate_manifest_<ts>.json
```

`apply_soil` fills only NULL Treatment fields (never overwrites a measured value).
`apply_climate` treats `0` as the legacy "unknown" placeholder, so it fills NULL
**or 0** MAP/MAT but leaves a real non-zero value alone. Both write a backup
manifest and reverse exactly. The worksheets only contain currently-missing
fields, so an apply is inherently additive.

### Which values are estimated — `ESTIMATED_VALUES.md`

Because the fallback values are modelled (SoilGrids 250 m / NASA POWER climatology),
not site measurements, every gridded fill is tracked. `ETL/build_provenance.py`
reads the two worksheets + the site/treatment map and writes
[`ESTIMATED_VALUES.md`](ESTIMATED_VALUES.md) — a per-site table of exactly which
fields and treatment ranges carry an estimate (`soilgrids`, `soilgrids_scaled`,
`nasa_power`) versus the authoritative `summary` source. Regenerate it after any
re-run:

```bash
# dump the map the table needs (utf8mb4 so site names render):
#   SELECT s.SiteID, s.SiteName, t.TreatmentID, e.ExperimentName ...  -> /tmp/site_trt_map.tsv
python3 ETL/build_provenance.py        # writes ETL/ESTIMATED_VALUES.md
```

## How the worksheets were generated — `enrichment/`

The `enrichment/` scripts are the provenance tools that produced the worksheets.
They were run from a host with Docker-socket access to the `db` container and
outbound internet (they query the live DB to fingerprint each pub, then resolve
sources from public APIs). They are **reference/repeatability artifacts**, not
part of the in-container workflow. Set `MYSQL_PASSWORD` in the environment before
running (the DB password is not hard-coded).

| Script | Role |
|---|---|
| `dock.py` | Minimal Docker-socket client; `exec_run` runs `mysql` inside the `db` container |
| `resolve_cite.py` | Journal pubs: extract the **DOI from each pub's own citation**, fetch canonical title/abstract via **CrossRef-by-DOI** (+ Semantic Scholar / OpenAlex fallback) |
| `final_match.py` | GRACEnet datasets: match each pub to the **Ag Data Commons** (Figshare) study by city + study name/acronym, then pull DOI/abstract/citation |
| `fill_meta.py` | Multi-source metadata filler for confirmed links (Ag Data Commons, NAL GeoNetwork, journal DOIs) |

### Data sources used

- **CrossRef** (`api.crossref.org`) — DOI → title/authors/abstract; bibliographic search.
- **OpenAlex** (`api.openalex.org`) — abstract reconstruction from `abstract_inverted_index`.
- **Semantic Scholar** (`api.semanticscholar.org`) — abstract fallback by DOI.
- **Ag Data Commons / Figshare** (`api.figshare.com`, `agdatacommons.nal.usda.gov`) — GRACEnet study datasets (DOIs `10.15482/USDA.ADC/...`). This is the authoritative GRACEnet catalog; `geodata.nal.usda.gov/geonetwork` is an older/partial mirror.
- **n2o.net.au** metacat — Australian dataset records (some access-restricted, e.g. `kelly.30.33`).

## Reversibility summary

Every committing run writes a JSON manifest (`*_manifest_<timestamp>.json`,
git-ignored). `load_tables --undo` deletes the loaded pk ranges; `apply_enrichment
--undo` restores prior field values (and refuses if a value changed since, unless
`--force`). No step alters the schema or migrations.

## Files in this directory

- `pub_enrichment.csv`, `gracenet_candidates.csv`, `title_fixes.csv` — reviewed enrichment inputs (**committed**).
- `treatment_fixes.csv` — Treatment convention fixes (**committed**).
- `soil_enrichment.csv`, `site_climate.csv` — reviewed soil/climate worksheets (**committed**).
- `build_soil.py` — generator for the soil/climate worksheets (**committed**).
- `build_provenance.py` + `ESTIMATED_VALUES.md` — provenance table of modelled fills (**committed**).
- `enrichment/` — provenance resolver scripts (**committed**).
- `RawData.csv` — 82 MB legacy denormalized export for `import_rawdata` (**git-ignored**).
- `_grid_cache.json` — per-site SoilGrids/NASA POWER cache (**git-ignored**).
- `*_manifest_*.json` — run-specific undo manifests (**git-ignored**; keep locally for rollback).
