-- WSI Slide Summary Pipeline
-- Nightly Databricks Job: computes per-sample slide availability stats
-- and writes them to sample_wsi_summary for fast query by tools and the tile server.
--
-- Output table: cdsi_prod.pathology_data_mining.sample_wsi_summary
-- Job is managed via databricks.yml (Databricks Asset Bundle).
--
-- Usage (manual run):
--   databricks bundle run wsi-summary-pipeline

CREATE OR REPLACE TABLE cdsi_prod.pathology_data_mining.sample_wsi_summary AS
SELECT
    d.sample_id                                      AS sample_id,
    d.PATIENT_ID                                     AS patient_id,
    COUNT(DISTINCT CASE
        WHEN s.path LIKE 's3://%'
         AND (
            LOWER(COALESCE(d.stain_group, d.stain_name, '')) LIKE '%h&e%'
            OR LOWER(COALESCE(d.stain_group, '')) LIKE '%ihc%'
         )
        THEN d.image_id
        ELSE NULL
    END)                                             AS servable_slide_count,
    MAX(CASE WHEN LOWER(COALESCE(d.stain_group, d.stain_name, '')) LIKE '%h&e%' AND s.path LIKE 's3://%' THEN 1 ELSE 0 END) AS has_hne,
    MAX(CASE WHEN LOWER(COALESCE(d.stain_group, '')) LIKE '%ihc%' AND s.path LIKE 's3://%' THEN 1 ELSE 0 END) AS has_ihc,
    ARRAY_JOIN(
        ARRAY_SORT(
            COLLECT_SET(
                CASE
                    WHEN s.path LIKE 's3://%'
                     AND (
                        LOWER(COALESCE(d.stain_group, d.stain_name, '')) LIKE '%h&e%'
                        OR LOWER(COALESCE(d.stain_group, '')) LIKE '%ihc%'
                     )
                    THEN d.stain_name
                    ELSE NULL
                END
            )
        ),
        ';'
    )                                                AS stain_types
FROM cdsi_eng_phi.pdm_base_tables_dev.impact_block_matched_slides_v1 d
LEFT JOIN cdsi_eng_phi.pdm_base_tables.slide_inventory s ON d.image_id = s.image_id
WHERE d.sample_id IS NOT NULL
GROUP BY d.sample_id, d.PATIENT_ID
