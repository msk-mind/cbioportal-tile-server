-- WSI Slide Summary Pipeline
-- Nightly Databricks Job: computes patient-wide diagnostic slide availability
-- stats and writes them to sample_wsi_summary for fast query by tools and the
-- tile server.
--
-- Output table: cdsi_prod.pathology_data_mining.sample_wsi_summary
-- Job is managed via databricks.yml (Databricks Asset Bundle).
--
-- Usage (manual run):
--   databricks bundle run wsi-summary-pipeline

CREATE OR REPLACE TABLE cdsi_prod.pathology_data_mining.sample_wsi_summary AS
WITH impact_samples AS (
    SELECT DISTINCT
        d.sample_id AS sample_id,
        d.PATIENT_ID AS patient_id
    FROM cdsi_eng_phi.pdm_base_tables_dev.impact_block_matched_slides_v1 d
    WHERE d.sample_id IS NOT NULL
      AND d.PATIENT_ID IS NOT NULL
),
patient_map AS (
    SELECT DISTINCT
        mrn,
        PATIENT_ID AS patient_id
    FROM cdsi_eng_phi.pdm_base_tables_dev.impact_block_matched_slides_v1
    WHERE mrn IS NOT NULL
      AND PATIENT_ID IS NOT NULL
),
diagnostic_slide_universe AS (
    SELECT DISTINCT
        p.patient_id AS patient_id,
        c.image_id AS image_id,
        c.stain_name AS stain_name,
        CASE
            WHEN c.stain_group IN ('H&E (Initial)', 'H&E (Other)') THEN 'H&E'
            WHEN c.stain_group = 'IHC' THEN 'IHC'
            ELSE NULL
        END AS stain_bucket
    FROM cdsi_eng_phi.pdm_base_tables_dev.case_breakdown_cleaned_v2 c
    INNER JOIN patient_map p ON c.mrn = p.mrn
    WHERE c.image_id IS NOT NULL
      AND (
        c.stain_group IN ('H&E (Initial)', 'H&E (Other)')
        OR (
            c.stain_group = 'IHC'
            AND LOWER(TRIM(COALESCE(c.stain_name, ''))) NOT LIKE 'immuno recut%'
            AND LOWER(COALESCE(c.stain_name, '')) NOT LIKE '%unstained%'
        )
      )
),
servable_inventory AS (
    SELECT DISTINCT image_id
    FROM cdsi_eng_phi.pdm_base_tables.slide_inventory
    WHERE path LIKE 's3://%'
),
viewable_patient_summary AS (
SELECT
    d.PATIENT_ID                                     AS patient_id,
    COUNT(DISTINCT d.image_id)                       AS servable_slide_count,
    MAX(CASE
        WHEN d.stain_group IN ('H&E (Initial)', 'H&E (Other)')
        THEN 1 ELSE 0
    END)                                             AS has_hne,
    MAX(CASE
        WHEN d.stain_group = 'IHC'
         AND LOWER(TRIM(COALESCE(d.stain_name, ''))) NOT LIKE 'immuno recut%'
         AND LOWER(COALESCE(d.stain_name, '')) NOT LIKE '%unstained%'
        THEN 1 ELSE 0
    END)                                             AS has_ihc,
    ARRAY_JOIN(
        ARRAY_SORT(COLLECT_SET(d.stain_name)),
        ';'
    )                                                AS stain_types
FROM cdsi_eng_phi.pdm_base_tables_dev.impact_block_matched_slides_v1 d
INNER JOIN servable_inventory s ON d.image_id = s.image_id
WHERE d.image_id IS NOT NULL
  AND d.PATIENT_ID IS NOT NULL
GROUP BY d.PATIENT_ID
),
non_viewable_patient_summary AS (
SELECT
    d.patient_id                                     AS patient_id,
    COUNT(DISTINCT CASE
        WHEN d.stain_bucket = 'H&E' AND s.image_id IS NULL
        THEN d.image_id
        ELSE NULL
    END)                                             AS non_servable_hne_slide_count,
    COUNT(DISTINCT CASE
        WHEN d.stain_bucket = 'IHC' AND s.image_id IS NULL
        THEN d.image_id
        ELSE NULL
    END)                                             AS non_servable_ihc_slide_count
FROM diagnostic_slide_universe d
LEFT JOIN servable_inventory s ON d.image_id = s.image_id
GROUP BY d.patient_id
)
SELECT
    i.sample_id                                      AS sample_id,
    i.patient_id                                     AS patient_id,
    COALESCE(v.servable_slide_count, 0)               AS servable_slide_count,
    COALESCE(n.non_servable_hne_slide_count, 0)       AS non_servable_hne_slide_count,
    COALESCE(n.non_servable_ihc_slide_count, 0)       AS non_servable_ihc_slide_count,
    COALESCE(v.has_hne, 0)                            AS has_hne,
    COALESCE(v.has_ihc, 0)                            AS has_ihc,
    COALESCE(v.stain_types, '')                       AS stain_types
FROM impact_samples i
LEFT JOIN viewable_patient_summary v ON i.patient_id = v.patient_id
LEFT JOIN non_viewable_patient_summary n ON i.patient_id = n.patient_id
