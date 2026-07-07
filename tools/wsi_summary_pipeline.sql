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
    d.SAMPLE_ID_IMPACT                               AS sample_id,
    d.PATIENT_ID_IMPACT                              AS patient_id,
    COUNT(DISTINCT CASE
        WHEN s.path LIKE 's3://%'
         AND (
            d.IS_HNE = 1
            OR d.IS_IHC = 1
         )
        THEN d.image_id
        ELSE NULL
    END)                                             AS servable_slide_count,
    MAX(CASE WHEN d.IS_HNE = 1 AND s.path LIKE 's3://%' THEN 1 ELSE 0 END) AS has_hne,
    MAX(CASE WHEN d.IS_IHC = 1 AND s.path LIKE 's3://%' THEN 1 ELSE 0 END) AS has_ihc,
    ARRAY_JOIN(
        ARRAY_SORT(
            COLLECT_SET(
                CASE
                    WHEN s.path LIKE 's3://%'
                     AND (d.IS_HNE = 1 OR d.IS_IHC = 1)
                    THEN d.stain_name
                    ELSE NULL
                END
            )
        ),
        ';'
    )                                                AS stain_types
FROM cdsi_prod.pathology_data_mining.impact_matched_slides_deid d
LEFT JOIN cdsi_eng_phi.pdm_base_tables.slide_inventory s ON d.image_id = s.image_id
WHERE d.SAMPLE_ID_IMPACT IS NOT NULL
GROUP BY d.SAMPLE_ID_IMPACT, d.PATIENT_ID_IMPACT
