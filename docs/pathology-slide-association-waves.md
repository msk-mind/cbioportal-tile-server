# Pathology Slide Association Waves

This document tracks the current implementation state for the slide-association
cleanup and performance work.

## Current state

Completed in the tile server:

- Python-side serializer dedupe in `app/meta.py` for repeated association rows.
- SQL-side canonicalization in `app/meta_store.py` inside `PATIENT_SQL`:
  repeated association rows are collapsed before the `/patient/{patient_id}`
  payload is assembled.
- A materialized upstream SQL asset now exists at
  `tools/wsi_canonical_associations_pipeline.sql`, with the Databricks bundle
  configured to build
  `cdsi_prod.pathology_data_mining.canonical_slide_associations` nightly
  before the WSI summary pipeline runs.
- The tile server association read path now prefers
  `cdsi_prod.pathology_data_mining.canonical_slide_associations` and falls
  back to the legacy inline SQL only when the canonical table is missing.
- Regression coverage in `tests/test_meta.py` for duplicate
  `slide_associations`.

Verified on July 17, 2026:

- Focused metadata tests pass:
  `uv run pytest tests/test_meta.py -k 'duplicate_slide_associations_are_deduplicated or single_slide_full_nesting or two_slides_same_block'`
- Live tile-server payload for `P-0002438` at `http://pllimsksparky3:8081`
  no longer returns duplicate H&E associations at `-142` days.
- The materialized canonical Databricks table now exists at
  `cdsi_prod.pathology_data_mining.canonical_slide_associations`.
- Representative canonical-vs-legacy validation now passes for:
  - `P-0002438`
  - `P-0048660`
  - `P-0011144`

## What this wave does

- Prevents duplicate `slide_associations` from inflating:
  - pathology clinical-data counts
  - summary pathology timeline counts
  - match-filter counts in the WSI viewer
- Keeps `sample_id = null` as the canonical representation for unmatched slides.

## What this wave does not do

- It does not remove the legacy inline association SQL yet; runtime still keeps
  that path as a missing-table fallback.
- It does not move patient summary, clinical-data pathology rows, or study-view
  pathology counts off runtime augmentation and onto ClickHouse.
- It does not fully cut over downstream aggregation to the canonical table yet.

## Remaining waves

### Wave 1 remainder

- Promote the current canonical association logic to a shared Databricks table
  or view instead of only embedding it in the tile-server query.
  Status on July 17, 2026: the shared SQL asset and nightly bundle task now
  exist, and the tile server prefers the upstream materialized table with a
  missing-table fallback to the legacy inline query.
- Add explicit `association_version`, `updated_at`, `sample_bucket`, and
  `sample_label` in the shared upstream dataset.

### Wave 2

- Point all tile-server association reads at the shared Databricks dataset.
- Add patient-level cache invalidation tied to targeted backfills.
  Status on July 17, 2026: tile-server Redis patient cache eviction is now
  available through `app.cache.delete_patient(...)` and the operational helper
  `tools/invalidate_patient_cache.py`.
- Study refresh tools now support `--invalidate-patient-cache` so a targeted
  regenerate-and-reload workflow can evict stale `/patient/{patient_id}`
  payloads for the study cohort in the same run:
  - `tools/generate_wsi_clinical_attrs.py`
  - `tools/generate_wsi_timepoint_clinical_attrs.py`
  - `tools/generate_resource_patient.py`

### Wave 3

- Load canonical association rows into ClickHouse.
- Build aggregate ClickHouse tables for:
  - patient summary pathology timeline
  - patient clinical-data pathology rows
  - study pathology sorting and counts

### Wave 4

- Build validation harnesses comparing:
  - canonical Databricks associations
  - tile-server `/patient/{patient_id}` payloads
  - ClickHouse aggregates
  Status on July 17, 2026: a canonical-vs-legacy Databricks comparison helper
  now exists at `tools/validate_canonical_associations.py`. Validation against
  tile-server payloads and ClickHouse aggregates is still pending.

### Wave 5

- Cut patient summary pathology reads over to ClickHouse aggregates.
- Cut patient clinical-data pathology reads over to ClickHouse aggregates.
- Cut study-view pathology counts and sort paths over to ClickHouse aggregates.

## Operational note

The frontend dev server on `pllimsksparky3:3000` proxies `/patient/P-*` to the
local tile server on `localhost:8081`. Tile-server code changes require a tile
server rebuild and restart on that host before the frontend picks them up.
