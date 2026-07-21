-- WSI Canonical Association Pipeline
-- Nightly Databricks Job: computes the canonical patient/sample/slide
-- association universe used by the tile server and downstream ETL.
--
-- Output table:
--   cdsi_prod.pathology_data_mining.canonical_slide_associations
--
-- This materializes the same block-match / part-match / unmatched logic that
-- previously lived only inside PATIENT_ASSOCIATIONS_SQL in app/meta_store.py.

CREATE OR REPLACE TABLE cdsi_prod.pathology_data_mining.canonical_slide_associations AS
WITH sample_sequencing AS (
    SELECT
        sample_id,
        MAX(sequencing_date) AS sequencing_date
    FROM (
        SELECT
            SAMPLE_ID AS sample_id,
            TRY_CAST(DATE_SEQUENCING_REPORT AS DATE) AS sequencing_date
        FROM cdsi_eng_phi.cdm_eng_pathology_report_segmentation.table_pathology_impact_sample_summary_dop_anno_epic_idb_combined
        WHERE SAMPLE_ID IS NOT NULL
          AND DATE_SEQUENCING_REPORT IS NOT NULL

        UNION ALL

        SELECT
            SAMPLE_ID AS sample_id,
            CAST(
                SUBSTR(DTE_TUMOR_SEQUENCING, 13, 4) || '-' ||
                CASE SUBSTR(DTE_TUMOR_SEQUENCING, 9, 3)
                    WHEN 'Jan' THEN '01'
                    WHEN 'Feb' THEN '02'
                    WHEN 'Mar' THEN '03'
                    WHEN 'Apr' THEN '04'
                    WHEN 'May' THEN '05'
                    WHEN 'Jun' THEN '06'
                    WHEN 'Jul' THEN '07'
                    WHEN 'Aug' THEN '08'
                    WHEN 'Sep' THEN '09'
                    WHEN 'Oct' THEN '10'
                    WHEN 'Nov' THEN '11'
                    WHEN 'Dec' THEN '12'
                END || '-' ||
                SUBSTR(DTE_TUMOR_SEQUENCING, 6, 2) AS DATE
            ) AS sequencing_date
        FROM cdsi_prod.cdm_idbw_impact_pipeline_prod.ddp_pathology_reports
        WHERE SAMPLE_ID IS NOT NULL
          AND DTE_TUMOR_SEQUENCING IS NOT NULL
    ) x
    WHERE sequencing_date IS NOT NULL
    GROUP BY sample_id
),
inventory_paths AS (
    SELECT image_id, path
    FROM (
        SELECT
            CAST(image_id AS STRING) AS image_id,
            path,
            ROW_NUMBER() OVER (
                PARTITION BY CAST(image_id AS STRING)
                ORDER BY
                    CASE
                        WHEN path LIKE 's3://mskmind-bkt/reef-slides/%' THEN 0
                        WHEN path LIKE 's3://%' THEN 1
                        ELSE 2
                    END,
                    path
            ) AS row_num
        FROM cdsi_eng_phi.pdm_base_tables.slide_inventory
        WHERE image_id IS NOT NULL
          AND path IS NOT NULL
    ) ranked_inventory
    WHERE row_num = 1
),
sample_patient_pairs AS (
    SELECT DISTINCT
        PATIENT_ID AS patient_id,
        sample_id,
        mrn
    FROM cdsi_eng_phi.pdm_base_tables_dev.impact_block_matched_slides_v1
    WHERE PATIENT_ID IS NOT NULL
      AND sample_id IS NOT NULL

    UNION

    SELECT DISTINCT
        PATIENT_ID_IMPACT AS patient_id,
        SAMPLE_ID_IMPACT AS sample_id,
        mrn
    FROM cdsi_eng_phi.pdm_base_tables.impact_matched_slides
    WHERE PATIENT_ID_IMPACT IS NOT NULL
      AND SAMPLE_ID_IMPACT IS NOT NULL
),
patient_reference AS (
    SELECT
        patient_id,
        sample_id AS reference_sample_id,
        sequencing_date AS reference_sequencing_date
    FROM (
        SELECT
            spp.patient_id,
            spp.sample_id,
            ss.sequencing_date,
            ROW_NUMBER() OVER (
                PARTITION BY spp.patient_id
                ORDER BY ss.sequencing_date ASC, spp.sample_id ASC
            ) AS row_num
        FROM sample_patient_pairs spp
        INNER JOIN sample_sequencing ss ON spp.sample_id = ss.sample_id
        WHERE ss.sequencing_date IS NOT NULL
    ) ranked_reference
    WHERE row_num = 1
),
patient_map AS (
    SELECT DISTINCT
        patient_id,
        mrn
    FROM sample_patient_pairs
    WHERE mrn IS NOT NULL
),
procedure_dates AS (
    SELECT
        surgical.ACCESSION_NUMBER AS accession_number,
        MAX(surgical.PROCEDURE_DATE) AS procedure_date
    FROM cdsi_eng_phi.cdm_eng_pathology_report_segmentation.surgical_specimen_diagnoses_combined surgical
    INNER JOIN (
        SELECT DISTINCT cleaned.accession_number
        FROM cdsi_eng_phi.pdm_base_tables_dev.case_breakdown_cleaned_v2 cleaned
        INNER JOIN patient_map pm ON cleaned.mrn = pm.mrn
        WHERE cleaned.accession_number IS NOT NULL
    ) patient_accessions
        ON surgical.ACCESSION_NUMBER = patient_accessions.accession_number
    WHERE surgical.PROCEDURE_DATE IS NOT NULL
    GROUP BY surgical.ACCESSION_NUMBER
),
slide_procedure_dates AS (
    SELECT DISTINCT
        pm.patient_id,
        CAST(cleaned.image_id AS STRING) AS image_id,
        TRY_CAST(procedure_dates.procedure_date AS DATE) AS procedure_date
    FROM cdsi_eng_phi.pdm_base_tables_dev.case_breakdown_cleaned_v2 cleaned
    INNER JOIN patient_map pm ON cleaned.mrn = pm.mrn
    LEFT JOIN procedure_dates
        ON cleaned.accession_number = procedure_dates.accession_number
    WHERE cleaned.image_id IS NOT NULL
),
block_matches AS (
    SELECT DISTINCT
        'BLOCK' AS match_level,
        d.PATIENT_ID AS patient_id,
        d.sample_id,
        d.CANCER_TYPE,
        d.CANCER_TYPE_DETAILED,
        d.ONCOTREE_CODE,
        d.PRIMARY_SITE,
        d.SAMPLE_TYPE,
        d.METASTATIC_SITE,
        TRY_CAST(d.TUMOR_PURITY AS DOUBLE) AS TUMOR_PURITY,
        d.ONCOGENIC_MUTATIONS,
        TRY_CAST(d.`#ONCOGENIC_MUTATIONS` AS DOUBLE) AS NUM_ONCOGENIC_MUTATIONS,
        TRY_CAST(d.CVR_TMB_SCORE AS DOUBLE) AS CVR_TMB_SCORE,
        d.MSI_TYPE,
        CAST(d.image_id AS STRING) AS image_id,
        d.block_id,
        d.block_label,
        d.part_type,
        d.part_description,
        d.part_description AS path_dx_title,
        d.stain_name,
        d.stain_group,
        d.magnification,
        d.file_size_bytes,
        COALESCE(
            TRY_CAST(d.dop AS DATE),
            fallback_dates.procedure_date
        ) AS procedure_date,
        inventory_paths.path AS slide_path
    FROM cdsi_eng_phi.pdm_base_tables_dev.impact_block_matched_slides_v1 d
    LEFT JOIN inventory_paths ON CAST(d.image_id AS STRING) = inventory_paths.image_id
    LEFT JOIN slide_procedure_dates fallback_dates
        ON fallback_dates.patient_id = d.PATIENT_ID
       AND fallback_dates.image_id = CAST(d.image_id AS STRING)
    WHERE d.PATIENT_ID IS NOT NULL
      AND d.image_id IS NOT NULL
),
part_matches AS (
    SELECT DISTINCT
        'PART' AS match_level,
        d.PATIENT_ID_IMPACT AS patient_id,
        d.SAMPLE_ID_IMPACT AS sample_id,
        d.CANCER_TYPE,
        d.CANCER_TYPE_DETAILED,
        d.ONCOTREE_CODE,
        d.PRIMARY_SITE,
        d.SAMPLE_TYPE,
        d.METASTATIC_SITE,
        TRY_CAST(d.TUMOR_PURITY AS DOUBLE) AS TUMOR_PURITY,
        d.ONCOGENIC_MUTATIONS,
        TRY_CAST(d.`#ONCOGENIC_MUTATIONS` AS DOUBLE) AS NUM_ONCOGENIC_MUTATIONS,
        TRY_CAST(d.CVR_TMB_SCORE AS DOUBLE) AS CVR_TMB_SCORE,
        d.MSI_TYPE,
        CAST(d.image_id AS STRING) AS image_id,
        CONCAT(
            'part/',
            COALESCE(CAST(d.PART_NUMBER AS STRING), '?'),
            '-',
            COALESCE(CAST(d.BLOCK_NUMBER AS STRING), d.BLOCK_LABEL, '?')
        ) AS block_id,
        d.block_label,
        d.part_type,
        d.part_description,
        d.PATH_DX_SPEC_TITLE AS path_dx_title,
        d.stain_name,
        d.stain_group,
        d.magnification,
        d.file_size_bytes,
        COALESCE(
            TRY_CAST(d.DATE_OF_PROCEDURE_SURGICAL AS DATE),
            fallback_dates.procedure_date
        ) AS procedure_date,
        COALESCE(inventory_paths.path, d.SLIDE_URL) AS slide_path
    FROM cdsi_eng_phi.pdm_base_tables.impact_matched_slides d
    LEFT JOIN inventory_paths ON CAST(d.image_id AS STRING) = inventory_paths.image_id
    LEFT JOIN slide_procedure_dates fallback_dates
        ON fallback_dates.patient_id = d.PATIENT_ID_IMPACT
       AND fallback_dates.image_id = CAST(d.image_id AS STRING)
    WHERE d.PATIENT_ID_IMPACT IS NOT NULL
      AND d.image_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM block_matches b
          WHERE b.patient_id = d.PATIENT_ID_IMPACT
            AND b.sample_id = d.SAMPLE_ID_IMPACT
            AND b.image_id = CAST(d.image_id AS STRING)
      )
),
matched_associations_raw AS (
    SELECT * FROM block_matches
    UNION ALL
    SELECT * FROM part_matches
),
matched_associations AS (
    SELECT
        match_level,
        patient_id,
        sample_id,
        CANCER_TYPE,
        CANCER_TYPE_DETAILED,
        ONCOTREE_CODE,
        PRIMARY_SITE,
        SAMPLE_TYPE,
        METASTATIC_SITE,
        TUMOR_PURITY,
        ONCOGENIC_MUTATIONS,
        NUM_ONCOGENIC_MUTATIONS,
        CVR_TMB_SCORE,
        MSI_TYPE,
        image_id,
        block_id,
        block_label,
        part_type,
        part_description,
        path_dx_title,
        stain_name,
        stain_group,
        magnification,
        file_size_bytes,
        COALESCE(
            procedure_date,
            MAX(procedure_date) OVER (
                PARTITION BY patient_id, sample_id, block_id
            )
        ) AS procedure_date,
        slide_path
    FROM matched_associations_raw
),
slide_universe AS (
    SELECT DISTINCT
        pm.patient_id,
        CAST(c.image_id AS STRING) AS image_id,
        c.block_id,
        c.block_label,
        c.part_type,
        c.part_description,
        c.part_description AS path_dx_title,
        c.stain_name,
        c.stain_group,
        c.magnification,
        c.file_size_bytes,
        slide_procedure_dates.procedure_date,
        inventory_paths.path AS slide_path
    FROM cdsi_eng_phi.pdm_base_tables_dev.case_breakdown_cleaned_v2 c
    INNER JOIN patient_map pm ON c.mrn = pm.mrn
    LEFT JOIN inventory_paths ON CAST(c.image_id AS STRING) = inventory_paths.image_id
    LEFT JOIN slide_procedure_dates
        ON slide_procedure_dates.patient_id = pm.patient_id
       AND slide_procedure_dates.image_id = CAST(c.image_id AS STRING)
    WHERE c.image_id IS NOT NULL
),
unmatched_associations AS (
    SELECT
        'UNMATCHED' AS match_level,
        su.patient_id,
        CAST(NULL AS STRING) AS sample_id,
        CAST(NULL AS STRING) AS CANCER_TYPE,
        CAST(NULL AS STRING) AS CANCER_TYPE_DETAILED,
        CAST(NULL AS STRING) AS ONCOTREE_CODE,
        CAST(NULL AS STRING) AS PRIMARY_SITE,
        CAST(NULL AS STRING) AS SAMPLE_TYPE,
        CAST(NULL AS STRING) AS METASTATIC_SITE,
        CAST(NULL AS DOUBLE) AS TUMOR_PURITY,
        CAST(NULL AS STRING) AS ONCOGENIC_MUTATIONS,
        CAST(NULL AS DOUBLE) AS NUM_ONCOGENIC_MUTATIONS,
        CAST(NULL AS DOUBLE) AS CVR_TMB_SCORE,
        CAST(NULL AS STRING) AS MSI_TYPE,
        su.image_id,
        su.block_id,
        su.block_label,
        su.part_type,
        su.part_description,
        su.path_dx_title,
        su.stain_name,
        su.stain_group,
        su.magnification,
        su.file_size_bytes,
        su.procedure_date,
        su.slide_path
    FROM slide_universe su
    WHERE NOT EXISTS (
        SELECT 1
        FROM matched_associations matched
        WHERE matched.patient_id = su.patient_id
          AND matched.image_id = su.image_id
    )
),
associations_raw AS (
    SELECT * FROM matched_associations
    UNION ALL
    SELECT * FROM unmatched_associations
),
canonical_associations AS (
    SELECT *
    FROM (
        SELECT
            associations_raw.*,
            ROW_NUMBER() OVER (
                PARTITION BY
                    associations_raw.patient_id,
                    associations_raw.image_id
                ORDER BY
                    CASE
                        WHEN associations_raw.slide_path LIKE 's3://mskmind-bkt/reef-slides/%' THEN 0
                        WHEN associations_raw.slide_path LIKE 's3://%' THEN 1
                        ELSE 2
                    END,
                    CASE associations_raw.match_level
                        WHEN 'BLOCK' THEN 0
                        WHEN 'PART' THEN 1
                        WHEN 'UNMATCHED' THEN 2
                        ELSE 3
                    END,
                    CASE
                        WHEN associations_raw.sample_id IS NOT NULL THEN 0
                        ELSE 1
                    END,
                    COALESCE(associations_raw.sample_id, '~~~~~~~~'),
                    COALESCE(associations_raw.block_id, '~~~~~~~~'),
                    COALESCE(associations_raw.block_label, '~~~~~~~~'),
                    CASE
                        WHEN associations_raw.procedure_date IS NOT NULL THEN 0
                        ELSE 1
                    END,
                    CASE
                        WHEN associations_raw.stain_name IS NOT NULL THEN 0
                        ELSE 1
                    END,
                    CASE
                        WHEN associations_raw.part_description IS NOT NULL THEN 0
                        ELSE 1
                    END,
                    associations_raw.image_id
            ) AS association_row_num
        FROM associations_raw
    ) ranked_associations
    WHERE ranked_associations.association_row_num = 1
)
SELECT
    'canonical_slide_associations_v1' AS association_version,
    CURRENT_TIMESTAMP() AS updated_at,
    COALESCE(associations.sample_id, '__UNMATCHED__') AS sample_bucket,
    CASE
        WHEN associations.sample_id IS NULL THEN 'Unmatched pathology slides'
        ELSE associations.sample_id
    END AS sample_label,
    associations.match_level,
    associations.patient_id,
    associations.sample_id,
    associations.CANCER_TYPE,
    associations.CANCER_TYPE_DETAILED,
    associations.ONCOTREE_CODE,
    associations.PRIMARY_SITE,
    associations.SAMPLE_TYPE,
    associations.METASTATIC_SITE,
    associations.TUMOR_PURITY,
    associations.ONCOGENIC_MUTATIONS,
    associations.NUM_ONCOGENIC_MUTATIONS,
    associations.CVR_TMB_SCORE,
    associations.MSI_TYPE,
    associations.image_id,
    associations.block_id,
    associations.block_label,
    associations.part_type,
    associations.part_description,
    associations.path_dx_title,
    associations.stain_name,
    associations.stain_group,
    associations.magnification,
    associations.file_size_bytes,
    associations.slide_path,
    associations.procedure_date,
    patient_reference.reference_sample_id,
    patient_reference.reference_sequencing_date,
    DATEDIFF(
        associations.procedure_date,
        patient_reference.reference_sequencing_date
    ) AS slide_timepoint_days,
    CASE
        WHEN associations.procedure_date IS NOT NULL
         AND patient_reference.reference_sequencing_date IS NOT NULL
            THEN 'Procedure date relative to tumor sequencing'
        ELSE NULL
    END AS slide_timepoint_source
FROM canonical_associations associations
LEFT JOIN patient_reference
    ON patient_reference.patient_id = associations.patient_id
;
