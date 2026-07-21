#!/usr/bin/env python3
"""Batch-compare canonical-slide-association rows against cohort-scoped legacy SQL."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.constants import (  # noqa: E402
    CANONICAL_ASSOCIATION_TABLE,
    CLEANED_SLIDE_TABLE,
    DEFAULT_WAREHOUSE_ID,
    DEID_TABLE,
    INVENTORY_TABLE,
    PART_MATCH_TABLE,
)
from tools.patient_cache_utils import read_patient_ids_from_clinical_sample  # noqa: E402

_COMPARE_FIELDS = (
    "match_level",
    "patient_id",
    "sample_id",
    "image_id",
    "block_id",
    "block_label",
    "part_type",
    "part_description",
    "path_dx_title",
    "stain_name",
    "stain_group",
    "slide_path",
    "procedure_date",
    "reference_sample_id",
    "reference_sequencing_date",
    "slide_timepoint_days",
    "slide_timepoint_source",
)


def _chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _patient_ids_from_args(args: argparse.Namespace) -> list[str]:
    patient_ids = list(args.patient_ids)
    if args.study_dir:
        patient_ids.extend(
            read_patient_ids_from_clinical_sample(
                Path(args.study_dir).expanduser().resolve()
            )
        )
    return list(dict.fromkeys(patient_ids))


def _patient_cohort_values(patient_ids: list[str]) -> str:
    escaped = [patient_id.replace("'", "\\'") for patient_id in patient_ids]
    return ",\n    ".join(f"('{patient_id}')" for patient_id in escaped)


def _row_sig_expr(alias: str) -> str:
    parts = [
        f"COALESCE(CAST({alias}.{field} AS STRING), '')" for field in _COMPARE_FIELDS
    ]
    return "CONCAT_WS('\\u001f',\n            " + ",\n            ".join(parts) + "\n        )"


def _build_batch_validation_query(patient_ids: list[str], counts_only: bool = False) -> str:
    cohort_values = _patient_cohort_values(patient_ids)
    canonical_sig = _row_sig_expr("canonical_rows")
    legacy_sig = _row_sig_expr("legacy_rows")
    if counts_only:
        return f"""
WITH patient_cohort(patient_id) AS (
    VALUES
    {cohort_values}
),
sample_sequencing AS (
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
        FROM {INVENTORY_TABLE}
        WHERE image_id IS NOT NULL
          AND path IS NOT NULL
    ) ranked_inventory
    WHERE row_num = 1
),
sample_patient_pairs AS (
    SELECT DISTINCT
        d.PATIENT_ID AS patient_id,
        d.sample_id,
        d.mrn
    FROM {DEID_TABLE} d
    INNER JOIN patient_cohort pc
        ON d.PATIENT_ID = pc.patient_id
    WHERE d.sample_id IS NOT NULL
    UNION
    SELECT DISTINCT
        d.PATIENT_ID_IMPACT AS patient_id,
        d.SAMPLE_ID_IMPACT AS sample_id,
        d.mrn
    FROM {PART_MATCH_TABLE} d
    INNER JOIN patient_cohort pc
        ON d.PATIENT_ID_IMPACT = pc.patient_id
    WHERE d.SAMPLE_ID_IMPACT IS NOT NULL
),
patient_map AS (
    SELECT DISTINCT patient_id, mrn
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
        FROM {CLEANED_SLIDE_TABLE} cleaned
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
    FROM {CLEANED_SLIDE_TABLE} cleaned
    INNER JOIN patient_map pm ON cleaned.mrn = pm.mrn
    LEFT JOIN procedure_dates
        ON cleaned.accession_number = procedure_dates.accession_number
    WHERE cleaned.image_id IS NOT NULL
),
block_matches AS (
    SELECT DISTINCT
        d.PATIENT_ID AS patient_id,
        CAST(d.image_id AS STRING) AS image_id,
        d.sample_id,
        d.block_id,
        d.block_label,
        COALESCE(TRY_CAST(d.dop AS DATE), fallback_dates.procedure_date) AS procedure_date,
        inventory_paths.path AS slide_path
    FROM {DEID_TABLE} d
    INNER JOIN patient_cohort pc
        ON d.PATIENT_ID = pc.patient_id
    LEFT JOIN inventory_paths
        ON CAST(d.image_id AS STRING) = inventory_paths.image_id
    LEFT JOIN slide_procedure_dates fallback_dates
        ON fallback_dates.patient_id = d.PATIENT_ID
       AND fallback_dates.image_id = CAST(d.image_id AS STRING)
    WHERE d.image_id IS NOT NULL
),
part_matches AS (
    SELECT DISTINCT
        d.PATIENT_ID_IMPACT AS patient_id,
        CAST(d.image_id AS STRING) AS image_id,
        d.SAMPLE_ID_IMPACT AS sample_id,
        CONCAT(
            'part/',
            COALESCE(CAST(d.PART_NUMBER AS STRING), '?'),
            '-',
            COALESCE(CAST(d.BLOCK_NUMBER AS STRING), d.BLOCK_LABEL, '?')
        ) AS block_id,
        d.block_label,
        COALESCE(TRY_CAST(d.DATE_OF_PROCEDURE_SURGICAL AS DATE), fallback_dates.procedure_date) AS procedure_date,
        COALESCE(inventory_paths.path, d.SLIDE_URL) AS slide_path
    FROM {PART_MATCH_TABLE} d
    INNER JOIN patient_cohort pc
        ON d.PATIENT_ID_IMPACT = pc.patient_id
    LEFT JOIN inventory_paths
        ON CAST(d.image_id AS STRING) = inventory_paths.image_id
    LEFT JOIN slide_procedure_dates fallback_dates
        ON fallback_dates.patient_id = d.PATIENT_ID_IMPACT
       AND fallback_dates.image_id = CAST(d.image_id AS STRING)
    WHERE d.image_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM block_matches b
          WHERE b.patient_id = d.PATIENT_ID_IMPACT
            AND b.sample_id = d.SAMPLE_ID_IMPACT
            AND b.image_id = CAST(d.image_id AS STRING)
      )
),
matched_associations_raw AS (
    SELECT 'BLOCK' AS match_level, patient_id, sample_id, image_id, block_id, block_label, procedure_date, slide_path
    FROM block_matches
    UNION ALL
    SELECT 'PART' AS match_level, patient_id, sample_id, image_id, block_id, block_label, procedure_date, slide_path
    FROM part_matches
),
matched_associations AS (
    SELECT
        match_level,
        patient_id,
        sample_id,
        image_id,
        block_id,
        block_label,
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
        slide_procedure_dates.procedure_date,
        inventory_paths.path AS slide_path
    FROM {CLEANED_SLIDE_TABLE} c
    INNER JOIN patient_map pm ON c.mrn = pm.mrn
    LEFT JOIN inventory_paths
        ON CAST(c.image_id AS STRING) = inventory_paths.image_id
    LEFT JOIN slide_procedure_dates
        ON slide_procedure_dates.patient_id = pm.patient_id
       AND slide_procedure_dates.image_id = CAST(c.image_id AS STRING)
    WHERE c.image_id IS NOT NULL
),
legacy_rows AS (
    SELECT patient_id, image_id
    FROM matched_associations
    UNION ALL
    SELECT su.patient_id, su.image_id
    FROM slide_universe su
    WHERE NOT EXISTS (
        SELECT 1
        FROM matched_associations matched
        WHERE matched.patient_id = su.patient_id
          AND matched.image_id = su.image_id
    )
),
canonical_rows AS (
    SELECT patient_id, image_id
    FROM {CANONICAL_ASSOCIATION_TABLE}
    WHERE patient_id IN (SELECT patient_id FROM patient_cohort)
),
canonical_counts AS (
    SELECT patient_id, COUNT(*) AS canonical_count
    FROM canonical_rows
    GROUP BY patient_id
),
legacy_counts AS (
    SELECT patient_id, COUNT(*) AS legacy_count
    FROM legacy_rows
    GROUP BY patient_id
)
SELECT
    pc.patient_id,
    COALESCE(canonical_counts.canonical_count, 0) AS canonical_count,
    COALESCE(legacy_counts.legacy_count, 0) AS legacy_count,
    CASE
        WHEN COALESCE(canonical_counts.canonical_count, 0) =
             COALESCE(legacy_counts.legacy_count, 0)
            THEN TRUE
        ELSE FALSE
    END AS matches
FROM patient_cohort pc
LEFT JOIN canonical_counts
    ON pc.patient_id = canonical_counts.patient_id
LEFT JOIN legacy_counts
    ON pc.patient_id = legacy_counts.patient_id
ORDER BY pc.patient_id
"""
    diffs_cte = """
diffs AS (
    SELECT
        COALESCE(c.patient_id, l.patient_id) AS patient_id,
        COUNT_IF(c.patient_id IS NULL) AS missing_from_canonical_count,
        COUNT_IF(l.patient_id IS NULL) AS extra_in_canonical_count
    FROM canonical_indexed c
    FULL OUTER JOIN legacy_indexed l
        ON c.patient_id = l.patient_id
       AND c.row_sig = l.row_sig
       AND c.dup_idx = l.dup_idx
    GROUP BY COALESCE(c.patient_id, l.patient_id)
)
"""
    final_select = """
SELECT
    pc.patient_id,
    COALESCE(canonical_counts.canonical_count, 0) AS canonical_count,
    COALESCE(legacy_counts.legacy_count, 0) AS legacy_count,
    COALESCE(diffs.missing_from_canonical_count, 0) AS missing_from_canonical_count,
    COALESCE(diffs.extra_in_canonical_count, 0) AS extra_in_canonical_count,
    CASE
        WHEN COALESCE(diffs.missing_from_canonical_count, 0) = 0
         AND COALESCE(diffs.extra_in_canonical_count, 0) = 0
            THEN TRUE
        ELSE FALSE
    END AS matches
FROM patient_cohort pc
LEFT JOIN canonical_counts
    ON pc.patient_id = canonical_counts.patient_id
LEFT JOIN legacy_counts
    ON pc.patient_id = legacy_counts.patient_id
LEFT JOIN diffs
    ON pc.patient_id = diffs.patient_id
ORDER BY pc.patient_id
"""
    if counts_only:
        diffs_cte = ""
        final_select = """
SELECT
    pc.patient_id,
    COALESCE(canonical_counts.canonical_count, 0) AS canonical_count,
    COALESCE(legacy_counts.legacy_count, 0) AS legacy_count,
    CASE
        WHEN COALESCE(canonical_counts.canonical_count, 0) =
             COALESCE(legacy_counts.legacy_count, 0)
            THEN TRUE
        ELSE FALSE
    END AS matches
FROM patient_cohort pc
LEFT JOIN canonical_counts
    ON pc.patient_id = canonical_counts.patient_id
LEFT JOIN legacy_counts
    ON pc.patient_id = legacy_counts.patient_id
ORDER BY pc.patient_id
"""
    optional_diffs = f",\n{diffs_cte}" if diffs_cte else ""
    return f"""
WITH patient_cohort(patient_id) AS (
    VALUES
    {cohort_values}
),
sample_sequencing AS (
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
        FROM {INVENTORY_TABLE}
        WHERE image_id IS NOT NULL
          AND path IS NOT NULL
    ) ranked_inventory
    WHERE row_num = 1
),
sample_patient_pairs AS (
    SELECT DISTINCT
        d.PATIENT_ID AS patient_id,
        d.sample_id,
        d.mrn
    FROM {DEID_TABLE} d
    INNER JOIN patient_cohort pc
        ON d.PATIENT_ID = pc.patient_id
    WHERE d.sample_id IS NOT NULL
    UNION
    SELECT DISTINCT
        d.PATIENT_ID_IMPACT AS patient_id,
        d.SAMPLE_ID_IMPACT AS sample_id,
        d.mrn
    FROM {PART_MATCH_TABLE} d
    INNER JOIN patient_cohort pc
        ON d.PATIENT_ID_IMPACT = pc.patient_id
    WHERE d.SAMPLE_ID_IMPACT IS NOT NULL
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
    SELECT DISTINCT patient_id, mrn
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
        FROM {CLEANED_SLIDE_TABLE} cleaned
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
    FROM {CLEANED_SLIDE_TABLE} cleaned
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
    FROM {DEID_TABLE} d
    INNER JOIN patient_cohort pc
        ON d.PATIENT_ID = pc.patient_id
    LEFT JOIN inventory_paths
        ON CAST(d.image_id AS STRING) = inventory_paths.image_id
    LEFT JOIN slide_procedure_dates fallback_dates
        ON fallback_dates.patient_id = d.PATIENT_ID
       AND fallback_dates.image_id = CAST(d.image_id AS STRING)
    WHERE d.image_id IS NOT NULL
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
    FROM {PART_MATCH_TABLE} d
    INNER JOIN patient_cohort pc
        ON d.PATIENT_ID_IMPACT = pc.patient_id
    LEFT JOIN inventory_paths
        ON CAST(d.image_id AS STRING) = inventory_paths.image_id
    LEFT JOIN slide_procedure_dates fallback_dates
        ON fallback_dates.patient_id = d.PATIENT_ID_IMPACT
       AND fallback_dates.image_id = CAST(d.image_id AS STRING)
    WHERE d.image_id IS NOT NULL
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
    FROM {CLEANED_SLIDE_TABLE} c
    INNER JOIN patient_map pm ON c.mrn = pm.mrn
    LEFT JOIN inventory_paths
        ON CAST(c.image_id AS STRING) = inventory_paths.image_id
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
legacy_canonical_associations AS (
    SELECT *
    FROM (
        SELECT
            associations_raw.*,
            ROW_NUMBER() OVER (
                PARTITION BY
                    associations_raw.patient_id,
                    COALESCE(associations_raw.sample_id, '__UNMATCHED__'),
                    associations_raw.image_id,
                    associations_raw.match_level,
                    COALESCE(associations_raw.block_id, ''),
                    COALESCE(associations_raw.block_label, '')
                ORDER BY
                    CASE
                        WHEN associations_raw.slide_path LIKE 's3://mskmind-bkt/reef-slides/%' THEN 0
                        WHEN associations_raw.slide_path LIKE 's3://%' THEN 1
                        ELSE 2
                    END,
                    CASE WHEN associations_raw.procedure_date IS NOT NULL THEN 0 ELSE 1 END,
                    CASE WHEN associations_raw.stain_name IS NOT NULL THEN 0 ELSE 1 END,
                    CASE WHEN associations_raw.part_description IS NOT NULL THEN 0 ELSE 1 END,
                    associations_raw.image_id
            ) AS association_row_num
        FROM associations_raw
    ) ranked_associations
    WHERE ranked_associations.association_row_num = 1
),
legacy_rows AS (
    SELECT
        associations.match_level,
        associations.patient_id,
        associations.sample_id,
        associations.image_id,
        associations.block_id,
        associations.block_label,
        associations.part_type,
        associations.part_description,
        associations.path_dx_title,
        associations.stain_name,
        associations.stain_group,
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
    FROM legacy_canonical_associations associations
    LEFT JOIN patient_reference
        ON patient_reference.patient_id = associations.patient_id
),
canonical_rows AS (
    SELECT
        match_level,
        patient_id,
        sample_id,
        image_id,
        block_id,
        block_label,
        part_type,
        part_description,
        path_dx_title,
        stain_name,
        stain_group,
        slide_path,
        procedure_date,
        reference_sample_id,
        reference_sequencing_date,
        slide_timepoint_days,
        slide_timepoint_source
    FROM {CANONICAL_ASSOCIATION_TABLE}
    WHERE patient_id IN (SELECT patient_id FROM patient_cohort)
),
canonical_indexed AS (
    SELECT
        canonical_rows.patient_id,
        {canonical_sig} AS row_sig,
        ROW_NUMBER() OVER (
            PARTITION BY canonical_rows.patient_id, {canonical_sig}
            ORDER BY canonical_rows.patient_id
        ) AS dup_idx
    FROM canonical_rows
),
legacy_indexed AS (
    SELECT
        legacy_rows.patient_id,
        {legacy_sig} AS row_sig,
        ROW_NUMBER() OVER (
            PARTITION BY legacy_rows.patient_id, {legacy_sig}
            ORDER BY legacy_rows.patient_id
        ) AS dup_idx
    FROM legacy_rows
),
canonical_counts AS (
    SELECT patient_id, COUNT(*) AS canonical_count
    FROM canonical_rows
    GROUP BY patient_id
),
legacy_counts AS (
    SELECT patient_id, COUNT(*) AS legacy_count
    FROM legacy_rows
    GROUP BY patient_id
){optional_diffs}
{final_select}
"""


def _run_query(warehouse_id: str, sql: str) -> list[dict[str, object]]:
    wc = WorkspaceClient()
    stmt = wc.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=sql,
        wait_timeout="50s",
    )
    while stmt.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(2)
        stmt = wc.statement_execution.get_statement(stmt.statement_id)
    if stmt.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"Databricks query failed: {stmt.status.error}")
    columns = [col.name for col in stmt.manifest.schema.columns]
    return [dict(zip(columns, row)) for row in (stmt.result.data_array or [])]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Batch-compare canonical and legacy slide associations for a cohort."
    )
    parser.add_argument("patient_ids", nargs="*", help="Patient IDs to validate")
    parser.add_argument(
        "--study-dir",
        help="Validate every patient listed in data_clinical_sample.txt for this study.",
    )
    parser.add_argument(
        "--warehouse-id",
        default=DEFAULT_WAREHOUSE_ID,
        help="Databricks SQL warehouse ID.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=250,
        help="Patients per Databricks query batch.",
    )
    parser.add_argument(
        "--only-mismatches",
        action="store_true",
        help="Only print mismatching patients.",
    )
    parser.add_argument(
        "--counts-only",
        action="store_true",
        help="Only compare per-patient canonical vs legacy row counts.",
    )
    args = parser.parse_args(argv)

    patient_ids = _patient_ids_from_args(args)
    if not patient_ids:
        parser.error("provide at least one patient ID or --study-dir")
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")

    processed = 0
    mismatches = 0
    total = len(patient_ids)

    for batch in _chunk(patient_ids, args.batch_size):
        rows = _run_query(
            args.warehouse_id,
            _build_batch_validation_query(batch, counts_only=args.counts_only),
        )
        for row in rows:
            processed += 1
            matches = bool(row["matches"])
            if not matches:
                mismatches += 1
            if args.only_mismatches and matches:
                continue
            line = (
                f"{row['patient_id']}: matches={matches} "
                f"canonical={row['canonical_count']} legacy={row['legacy_count']}"
            )
            if not args.counts_only:
                line += (
                    f" missing={row['missing_from_canonical_count']}"
                    f" extra={row['extra_in_canonical_count']}"
                )
            print(line)
        print(f"Progress: {processed}/{total}")
        sys.stdout.flush()

    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
