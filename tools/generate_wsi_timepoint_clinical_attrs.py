#!/usr/bin/env python3
"""
Merge WSI sample timepoint columns into data_clinical_sample.txt.

This replaces the old matched-IMPACT acquisition/sequencing proxy with a
sample-level slide procedure date relative to tumor sequencing. Procedure dates
are backfilled per accession using the same source-priority chain as
backfill_case_breakdown_dop.sql, then the earliest matched relative day is
selected per sample.

Columns added to data_clinical_sample.txt:
  WSI_TIMEPOINT_BIN
  WSI_TIMEPOINT_DAYS
  WSI_TIMEPOINT_SOURCE
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

from app.constants import DEFAULT_WAREHOUSE_ID, DEID_TABLE
from tools.patient_cache_utils import invalidate_study_patient_cache

_ATTRIBUTES = [
    (
        "WSI_TIMEPOINT_BIN",
        "WSI Timepoint",
        "Relative slide procedure-date bin from tumor sequencing for matched WSI sample",
        "STRING",
        "950",
    ),
    (
        "WSI_TIMEPOINT_DAYS",
        "WSI Timepoint Days",
        "Days from tumor sequencing for earliest matched slide procedure date",
        "NUMBER",
        "0",
    ),
    (
        "WSI_TIMEPOINT_SOURCE",
        "WSI Timepoint Source",
        "Backfilled procedure-date source used for the matched WSI sample timepoint",
        "STRING",
        "0",
    ),
]

_SOURCE_LABELS = {
    1: "Procedure date (surgical specimen diagnoses)",
    2: "Procedure date (Epic pathology report)",
    3: "Procedure date (IDB IMPACT pathology report)",
    4: "Procedure date (Epic DDP pathology report)",
    5: "Procedure date (CoPath molecular link)",
    6: "Procedure date (staged pathology report)",
    7: "Procedure date (DOP annotation)",
    8: "Estimated procedure date (DOP annotation)",
}


def _run_query(
    wc: WorkspaceClient, warehouse_id: str, sql: str
) -> list[dict[str, object]]:
    stmt = wc.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=sql,
        wait_timeout="50s",
    )
    while stmt.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(2)
        stmt = wc.statement_execution.get_statement(stmt.statement_id)

    if stmt.status.state != StatementState.SUCCEEDED:
        err = getattr(stmt.status, "error", None)
        raise RuntimeError(f"Databricks query failed: {err}")

    columns = [c.name for c in stmt.manifest.schema.columns]
    return [
        dict(zip(columns, row))
        for row in (stmt.result.data_array or [])
    ]


def _read_clinical_sample(
    path: Path,
) -> tuple[list[list[str]], list[str], list[list[str]]]:
    comment_rows: list[list[str]] = []
    header: list[str] | None = None
    data_rows: list[list[str]] = []

    with path.open(newline="") as fh:
        for raw_line in fh:
            line = raw_line.rstrip("\n")
            if line.startswith("#"):
                comment_rows.append(line[1:].split("\t"))
                continue
            cols = line.split("\t")
            if header is None:
                header = cols
            else:
                data_rows.append(cols)

    if header is None:
        raise ValueError(f"No header row found in {path}")
    if len(comment_rows) < 4:
        raise ValueError(f"Expected 4 comment rows in {path}, found {len(comment_rows)}")
    return comment_rows, header, data_rows


def _read_sample_ids(study_dir: Path) -> list[str]:
    _, header, data_rows = _read_clinical_sample(study_dir / "data_clinical_sample.txt")
    try:
        sample_idx = header.index("SAMPLE_ID")
    except ValueError as exc:
        raise ValueError("SAMPLE_ID column not found in data_clinical_sample.txt") from exc

    sample_ids: list[str] = []
    seen: set[str] = set()
    for row in data_rows:
        if sample_idx >= len(row):
            continue
        sample_id = row[sample_idx].strip()
        if sample_id and sample_id not in seen:
            sample_ids.append(sample_id)
            seen.add(sample_id)
    return sample_ids


def _ensure_columns(
    comment_rows: list[list[str]],
    header: list[str],
) -> dict[str, int]:
    display_names = comment_rows[0]
    descriptions = comment_rows[1]
    dtypes = comment_rows[2]
    priorities = comment_rows[3]

    index_by_attr: dict[str, int] = {}
    for attr_id, display_name, description, dtype, priority in _ATTRIBUTES:
        if attr_id in header:
            idx = header.index(attr_id)
            display_names[idx] = display_name
            descriptions[idx] = description
            dtypes[idx] = dtype
            priorities[idx] = priority
            index_by_attr[attr_id] = idx
            continue

        header.append(attr_id)
        display_names.append(display_name)
        descriptions.append(description)
        dtypes.append(dtype)
        priorities.append(priority)
        index_by_attr[attr_id] = len(header) - 1

    return index_by_attr


def _pad_row(row: list[str], width: int) -> list[str]:
    if len(row) < width:
        row.extend([""] * (width - len(row)))
    return row


def _timepoint_bin(days: int | None) -> str:
    if days is None:
        return "Unknown"
    if days < 0:
        return "Pre-sequencing"
    if days == 0:
        return "Sequencing"
    if days <= 30:
        return "1-30 days"
    if days <= 90:
        return "31-90 days"
    if days <= 365:
        return "91-365 days"
    return ">365 days"


def _chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _sample_cohort_values(sample_ids: list[str]) -> str:
    escaped = [sample_id.replace("'", "\\'") for sample_id in sample_ids]
    return ",\n    ".join(f"('{sample_id}')" for sample_id in escaped)


def _build_timepoint_query(sample_ids: list[str]) -> str:
    values = _sample_cohort_values(sample_ids)
    return f"""
WITH sample_cohort(sample_id) AS (
  VALUES
    {values}
),
specimen AS (
  SELECT
    ACCESSION_NUMBER,
    MAX(PROCEDURE_DATE) AS procedure_date
  FROM cdsi_eng_phi.cdm_eng_pathology_report_segmentation.surgical_specimen_diagnoses_combined
  WHERE PROCEDURE_DATE IS NOT NULL
  GROUP BY ACCESSION_NUMBER
),
epic_all_msk AS (
  SELECT
    ACCESSION_NUMBER,
    MAX(DTE_PATH_PROCEDURE) AS procedure_date
  FROM cdsi_eng_phi.cdm_eng_pathology_report_segmentation.t14_pathology_reports_epic_consolidated
  WHERE DTE_PATH_PROCEDURE IS NOT NULL
  GROUP BY ACCESSION_NUMBER
),
idb_impact AS (
  SELECT
    ACCESSION_NUMBER,
    MAX(DTE_PATH_PROCEDURE) AS procedure_date
  FROM cdsi_prod.cdm_idbw_impact_pipeline_prod.ddp_pathology_reports
  WHERE DTE_PATH_PROCEDURE IS NOT NULL
  GROUP BY ACCESSION_NUMBER
),
epic_ddp AS (
  SELECT
    PRPT_ACCESSION_NO AS ACCESSION_NUMBER,
    MAX(PRPT_PROCEDURE_DTE) AS procedure_date
  FROM cdsi_prod.cdm_impact_pipeline_prod.t14_epic_ddp_pathology_reports
  WHERE PRPT_PROCEDURE_DTE IS NOT NULL
  GROUP BY PRPT_ACCESSION_NO
),
copath_links AS (
  SELECT
    accession_number AS ACCESSION_NUMBER,
    MAX(dop) AS procedure_date
  FROM cdsi_eng_phi.pdm_base_tables_dev.copath_molecular_links_cleaned_v2
  WHERE dop IS NOT NULL
  GROUP BY accession_number
),
stg_dates AS (
  SELECT
    ACCESSION_NUMBER,
    MAX(DTE_PATH_PROCEDURE) AS procedure_date
  FROM cdsi_eng_phi.cdm_eng_pathology_report_segmentation.stg_path_report_dates
  WHERE DTE_PATH_PROCEDURE IS NOT NULL
  GROUP BY ACCESSION_NUMBER
),
dop_anno AS (
  SELECT
    accession_number AS ACCESSION_NUMBER,
    MAX(TRY_CAST(DATE_OF_PROCEDURE_SURGICAL AS DATE)) AS procedure_date_actual,
    MAX(TRY_CAST(DATE_OF_PROCEDURE_SURGICAL_EST AS DATE)) AS procedure_date_est
  FROM (
    SELECT
      SOURCE_ACCESSION_NUMBER_0 AS accession_number,
      DATE_OF_PROCEDURE_SURGICAL,
      DATE_OF_PROCEDURE_SURGICAL_EST
    FROM cdsi_eng_phi.cdm_eng_pathology_report_segmentation.table_pathology_impact_sample_summary_dop_anno_epic_idb_combined
    WHERE SOURCE_ACCESSION_NUMBER_0 IS NOT NULL

    UNION ALL

    SELECT
      SOURCE_ACCESSION_NUMBER_0b AS accession_number,
      DATE_OF_PROCEDURE_SURGICAL,
      DATE_OF_PROCEDURE_SURGICAL_EST
    FROM cdsi_eng_phi.cdm_eng_pathology_report_segmentation.table_pathology_impact_sample_summary_dop_anno_epic_idb_combined
    WHERE SOURCE_ACCESSION_NUMBER_0b IS NOT NULL

    UNION ALL

    SELECT
      ACCESSION_NUMBER_DMP AS accession_number,
      DATE_OF_PROCEDURE_SURGICAL,
      DATE_OF_PROCEDURE_SURGICAL_EST
    FROM cdsi_eng_phi.cdm_eng_pathology_report_segmentation.table_pathology_impact_sample_summary_dop_anno_epic_idb_combined
    WHERE ACCESSION_NUMBER_DMP IS NOT NULL
  ) x
  GROUP BY accession_number
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
candidate_rows AS (
  SELECT DISTINCT
    d.sample_id,
    d.accession_number,
    DATEDIFF(
      COALESCE(
        s.procedure_date,
        e.procedure_date,
        i.procedure_date,
        ed.procedure_date,
        cl.procedure_date,
        sd.procedure_date,
        da.procedure_date_actual,
        da.procedure_date_est
      ),
      ss.sequencing_date
    ) AS relative_days,
    CASE
      WHEN s.procedure_date IS NOT NULL THEN 1
      WHEN e.procedure_date IS NOT NULL THEN 2
      WHEN i.procedure_date IS NOT NULL THEN 3
      WHEN ed.procedure_date IS NOT NULL THEN 4
      WHEN cl.procedure_date IS NOT NULL THEN 5
      WHEN sd.procedure_date IS NOT NULL THEN 6
      WHEN da.procedure_date_actual IS NOT NULL THEN 7
      WHEN da.procedure_date_est IS NOT NULL THEN 8
      ELSE 999
    END AS source_priority
  FROM {DEID_TABLE} d
  INNER JOIN sample_cohort sc
    ON d.sample_id = sc.sample_id
  INNER JOIN sample_sequencing ss
    ON d.sample_id = ss.sample_id
  LEFT JOIN specimen s
    ON d.accession_number = s.ACCESSION_NUMBER
  LEFT JOIN epic_all_msk e
    ON d.accession_number = e.ACCESSION_NUMBER
  LEFT JOIN idb_impact i
    ON d.accession_number = i.ACCESSION_NUMBER
  LEFT JOIN epic_ddp ed
    ON d.accession_number = ed.ACCESSION_NUMBER
  LEFT JOIN copath_links cl
    ON d.accession_number = cl.ACCESSION_NUMBER
  LEFT JOIN stg_dates sd
    ON d.accession_number = sd.ACCESSION_NUMBER
  LEFT JOIN dop_anno da
    ON d.accession_number = da.ACCESSION_NUMBER
  WHERE ss.sequencing_date IS NOT NULL
    AND COALESCE(
      s.procedure_date,
      e.procedure_date,
      i.procedure_date,
      ed.procedure_date,
      cl.procedure_date,
      sd.procedure_date,
      da.procedure_date_actual,
      da.procedure_date_est
    ) IS NOT NULL
),
ranked AS (
  SELECT
    sample_id,
    relative_days,
    source_priority,
    ROW_NUMBER() OVER (
      PARTITION BY sample_id
      ORDER BY relative_days ASC, source_priority ASC, accession_number ASC
    ) AS rn
  FROM candidate_rows
)
SELECT
  sample_id,
  relative_days,
  source_priority
FROM ranked
WHERE rn = 1
ORDER BY sample_id
"""


def _load_timepoints_by_sample(
    wc: WorkspaceClient, warehouse_id: str, sample_ids: list[str]
) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    for batch in _chunk(sample_ids, 500):
        rows = _run_query(wc, warehouse_id, _build_timepoint_query(batch))
        for row in rows:
            sample_id = str(row["sample_id"])
            relative_days = int(row["relative_days"])
            source_priority = int(row["source_priority"])
            result[sample_id] = (
                relative_days,
                _SOURCE_LABELS.get(
                    source_priority, "Procedure date (unknown fallback source)"
                ),
            )
    return result


def _merge_timepoints_into_clinical_sample(
    study_dir: Path,
    sample_timepoints: dict[str, tuple[int, str]],
) -> dict[str, int]:
    clinical_file = study_dir / "data_clinical_sample.txt"
    comment_rows, header, data_rows = _read_clinical_sample(clinical_file)
    sample_idx = header.index("SAMPLE_ID")
    index_by_attr = _ensure_columns(comment_rows, header)
    width = len(header)

    matched = 0
    unknown = 0

    for row in data_rows:
        _pad_row(row, width)
        sample_id = row[sample_idx]
        timepoint = sample_timepoints.get(sample_id)
        if timepoint is None:
            days = None
            source = ""
            unknown += 1
        else:
            days, source = timepoint
            matched += 1

        row[index_by_attr["WSI_TIMEPOINT_BIN"]] = _timepoint_bin(days)
        row[index_by_attr["WSI_TIMEPOINT_DAYS"]] = "" if days is None else str(days)
        row[index_by_attr["WSI_TIMEPOINT_SOURCE"]] = source

    with clinical_file.open("w", newline="") as fh:
        for comment_row in comment_rows[:4]:
            fh.write("#" + "\t".join(_pad_row(comment_row, width)) + "\n")
        fh.write("\t".join(header) + "\n")
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        for row in data_rows:
            writer.writerow(_pad_row(row, width))

    extra_meta = study_dir / "meta_clinical_sample_wsi_timepoint.txt"
    extra_data = study_dir / "data_clinical_sample_wsi_timepoint.txt"
    if extra_meta.exists():
        extra_meta.unlink()
    if extra_data.exists():
        extra_data.unlink()

    return {"matched": matched, "unknown": unknown}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Merge WSI timepoint sample clinical attributes into "
        "data_clinical_sample.txt."
    )
    parser.add_argument(
        "--study-dir",
        required=True,
        help="Path to the cBioPortal study directory (must contain data_clinical_sample.txt).",
    )
    parser.add_argument(
        "--warehouse-id",
        default=DEFAULT_WAREHOUSE_ID,
        help="Databricks SQL warehouse ID.",
    )
    parser.add_argument(
        "--invalidate-patient-cache",
        action="store_true",
        help="Evict tile-server patient cache entries for every patient in this study after writing files.",
    )
    args = parser.parse_args(argv)

    study_dir = Path(args.study_dir).expanduser().resolve()
    if not study_dir.is_dir():
        print(f"ERROR: study directory not found: {study_dir}", file=sys.stderr)
        sys.exit(1)

    clinical_file = study_dir / "data_clinical_sample.txt"
    if not clinical_file.exists():
        print(f"ERROR: missing {clinical_file}", file=sys.stderr)
        sys.exit(1)

    sample_ids = _read_sample_ids(study_dir)
    if not sample_ids:
        print("ERROR: no SAMPLE_ID values found", file=sys.stderr)
        sys.exit(1)

    wc = WorkspaceClient()
    sample_timepoints = _load_timepoints_by_sample(wc, args.warehouse_id, sample_ids)
    counts = _merge_timepoints_into_clinical_sample(study_dir, sample_timepoints)

    source_counts: dict[str, int] = {}
    for _days, source in sample_timepoints.values():
        source_counts[source] = source_counts.get(source, 0) + 1

    print(f"Updated {clinical_file}")
    print(
        "Matched:",
        f"sample_timepoints={counts['matched']}",
        f"unknown={counts['unknown']}",
    )
    if args.invalidate_patient_cache:
        deleted, requested = invalidate_study_patient_cache(study_dir)
        print(
            "Invalidated tile-server patient cache:",
            f"requested={requested}",
            f"deleted={deleted}",
        )
    for source, count in sorted(source_counts.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {source}: {count}")


if __name__ == "__main__":
    main()
