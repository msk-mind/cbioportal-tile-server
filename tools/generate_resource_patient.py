#!/usr/bin/env python3
"""
Generate cBioPortal PATIENT-level resource files for the WSI tile server.

Replaces the old DSA-based data_resource_sample.txt workflow.  For each
patient in the study that has at least one servable slide (present in
cdsi_eng_phi.pdm_base_tables.slide_inventory), one row is written to
data_resource_patient.txt pointing to the tile server.

Outputs written to --study-dir:
  data_resource_definition.txt   (PATIENT type, replaces old SAMPLE version)
  meta_resource_definition.txt
  data_resource_patient.txt      (one row per patient with a servable slide)
  meta_resource_patient.txt

Usage:
  python tools/generate_resource_patient.py \\
      --study-dir /path/to/private/automation_tool_datasets/coad_msk_2025 \\
      --base-url https://cbioportal.mskcc.org/wsi

Credentials:
  Databricks: reads ~/.databrickscfg (DEFAULT profile) or env vars
              DATABRICKS_HOST + DATABRICKS_TOKEN.
  Warehouse:  DATABRICKS_WAREHOUSE_ID env var or --warehouse-id flag
              (default: 0b49b7d78734ad5c).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

# Allow importing from the app package when running from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.constants import (  # noqa: E402
    DEID_TABLE as _DEID_TABLE,
    INVENTORY_TABLE as _INVENTORY,
    DEFAULT_WAREHOUSE_ID as _DEFAULT_WAREHOUSE,
)
from tools.patient_cache_utils import invalidate_study_patient_cache  # noqa: E402

# Patients with ≥1 slide in slide_inventory for the given patient list.
_PATIENT_QUERY = """
SELECT DISTINCT d.{patient_column} AS patient_id
FROM {deid} d
INNER JOIN {inventory} s ON d.image_id = s.image_id
WHERE {patient_column} IN ({placeholders})
ORDER BY {patient_column}
"""


def _run_query(wc, warehouse_id: str, sql: str) -> list[dict]:
    import time
    from databricks.sdk.service.sql import StatementState

    stmt = wc.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=sql,
        wait_timeout="50s",  # Databricks max; poll below if still running
    )
    while stmt.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(3)
        stmt = wc.statement_execution.get_statement(stmt.statement_id)

    if stmt.status.state != StatementState.SUCCEEDED:
        err = getattr(stmt.status, "error", None)
        raise RuntimeError(f"Databricks query failed: {err}")
    columns = [c.name for c in stmt.manifest.schema.columns]
    return [dict(zip(columns, row)) for row in (stmt.result.data_array or [])]


def _split_table_name(fully_qualified_name: str) -> tuple[str, str, str]:
    parts = fully_qualified_name.split(".")
    if len(parts) != 3:
        raise ValueError(
            f"Expected catalog.schema.table for Databricks table, got {fully_qualified_name!r}"
        )
    return tuple(parts)


def _resolve_patient_column(wc, warehouse_id: str) -> str:
    catalog, schema, table = _split_table_name(_DEID_TABLE)
    sql = f"""
SELECT column_name
FROM {catalog}.information_schema.columns
WHERE table_schema = '{schema}'
  AND table_name = '{table}'
  AND column_name IN ('PATIENT_ID_IMPACT', 'PATIENT_ID')
"""
    rows = _run_query(wc, warehouse_id, sql)
    available = {row["column_name"].upper(): row["column_name"].lower() for row in rows}
    if "PATIENT_ID_IMPACT" in available:
        return available["PATIENT_ID_IMPACT"]
    if "PATIENT_ID" in available:
        return available["PATIENT_ID"]
    raise RuntimeError(
        f"Could not find PATIENT_ID_IMPACT or PATIENT_ID in {_DEID_TABLE}"
    )


def _read_patient_ids(study_dir: Path) -> list[str]:
    """Extract PATIENT_ID column from data_clinical_sample.txt."""
    clinical = study_dir / "data_clinical_sample.txt"
    if not clinical.exists():
        raise FileNotFoundError(f"data_clinical_sample.txt not found in {study_dir}")

    patients: list[str] = []
    seen: set[str] = set()
    with clinical.open() as f:
        reader = csv.DictReader(
            (line for line in f if not line.startswith("#")),
            delimiter="\t",
        )
        for row in reader:
            pid = row.get("PATIENT_ID", "").strip()
            if pid and pid not in seen:
                patients.append(pid)
                seen.add(pid)
    if not patients:
        raise ValueError("No PATIENT_ID values found in data_clinical_sample.txt")
    return patients


def _read_study_identifier(study_dir: Path) -> str:
    meta = study_dir / "meta_study.txt"
    if not meta.exists():
        return study_dir.name
    for line in meta.read_text().splitlines():
        if line.startswith("cancer_study_identifier:"):
            return line.split(":", 1)[1].strip()
    return study_dir.name


def _chunk(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--study-dir", required=True, type=Path,
                        help="Path to the cBioPortal study directory")
    parser.add_argument("--base-url", default="https://cbioportal.mskcc.org/wsi",
                        help="Base URL of the WSI namespace")
    parser.add_argument("--cbio-url", default="https://www.cbioportal.org",
                        help="Base URL of the cBioPortal instance for backlinks "
                             "(default: https://www.cbioportal.org)")
    parser.add_argument("--warehouse-id",
                        default=os.environ.get("DATABRICKS_WAREHOUSE_ID", _DEFAULT_WAREHOUSE),
                        help="Databricks SQL warehouse ID")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print summary but do not write files")
    parser.add_argument(
        "--invalidate-patient-cache",
        action="store_true",
        help="Evict tile-server patient cache entries for every patient in this study after writing files.",
    )
    args = parser.parse_args(argv)

    study_dir: Path = args.study_dir.resolve()
    if not study_dir.is_dir():
        print(f"ERROR: study-dir {study_dir} does not exist", file=sys.stderr)
        return 1

    print(f"Study dir   : {study_dir}")
    print(f"Base URL    : {args.base_url}")
    print(f"cBioPortal  : {args.cbio_url}")
    print(f"Warehouse   : {args.warehouse_id}")

    # ── 1. Read patients from clinical data ──────────────────────────────────
    patient_ids = _read_patient_ids(study_dir)
    study_id = _read_study_identifier(study_dir)
    print(f"Study ID    : {study_id}")
    print(f"Patients in clinical data: {len(patient_ids)}")

    # ── 2. Query Databricks for patients with servable slides ─────────────────
    from databricks.sdk import WorkspaceClient
    wc = WorkspaceClient()

    patient_column = _resolve_patient_column(wc, args.warehouse_id)
    print(f"Patient column: {patient_column}")

    servable: set[str] = set()
    chunk_size = 500  # IN clause limit safe for Databricks SQL
    for batch in _chunk(patient_ids, chunk_size):
        # Escape single quotes before interpolation — patient IDs from a
        # clinical file are operator-controlled, not user input, but we still
        # sanitize defensively.  Databricks SQL doesn't support array binding
        # for IN clauses, so string interpolation is unavoidable here.
        escaped = [p.replace("'", "\\'") for p in batch]
        placeholders = ", ".join(f"'{p}'" for p in escaped)
        sql = _PATIENT_QUERY.format(
            deid=_DEID_TABLE,
            inventory=_INVENTORY,
            patient_column=patient_column,
            placeholders=placeholders,
        )
        rows = _run_query(wc, args.warehouse_id, sql)
        for row in rows:
            servable.add(row["patient_id"])

    servable_list = [p for p in patient_ids if p in servable]
    missing = [p for p in patient_ids if p not in servable]
    print(f"Patients with servable slides : {len(servable_list)}")
    print(f"Patients without slides       : {len(missing)}")
    if missing:
        print("  (no slides for: " + ", ".join(missing[:10])
              + (" …" if len(missing) > 10 else "") + ")")

    if args.dry_run:
        print("Dry run — no files written.")
        return 0

    base_url  = args.base_url.rstrip("/")
    cbio_url  = args.cbio_url.rstrip("/")

    # ── 3. Write data_resource_definition.txt ────────────────────────────────
    def_file = study_dir / "data_resource_definition.txt"
    def_file.write_text(
        "RESOURCE_ID\tDISPLAY_NAME\tRESOURCE_TYPE\tDESCRIPTION\tOPEN_BY_DEFAULT\tPRIORITY\n"
        "HE\tH&E Slide\tPATIENT\tH&E Slide\tTRUE\t1\n"
    )
    print(f"Written: {def_file}")

    # ── 4. Write meta_resource_definition.txt ────────────────────────────────
    meta_def = study_dir / "meta_resource_definition.txt"
    meta_def.write_text(
        f"cancer_study_identifier: {study_id}\n"
        "resource_type: DEFINITION\n"
        "data_filename: data_resource_definition.txt\n"
    )
    print(f"Written: {meta_def}")

    # ── 5. Write data_resource_patient.txt ───────────────────────────────────
    patient_file = study_dir / "data_resource_patient.txt"
    with patient_file.open("w", newline="") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(["PATIENT_ID", "RESOURCE_ID", "URL"])
        for pid in servable_list:
            viewer_url = (
                f"{base_url}/?patient={pid}"
                f"&studyId={study_id}"
                f"&cbioUrl={cbio_url}"
            )
            writer.writerow([pid, "HE", viewer_url])
    print(f"Written: {patient_file}  ({len(servable_list)} rows)")

    # ── 6. Write meta_resource_patient.txt ───────────────────────────────────
    meta_patient = study_dir / "meta_resource_patient.txt"
    meta_patient.write_text(
        f"cancer_study_identifier: {study_id}\n"
        "resource_type: PATIENT\n"
        "data_filename: data_resource_patient.txt\n"
    )
    print(f"Written: {meta_patient}")

    if args.invalidate_patient_cache:
        deleted, requested = invalidate_study_patient_cache(study_dir)
        print(
            "Invalidated tile-server patient cache:",
            f"requested={requested}",
            f"deleted={deleted}",
        )

    print("\nDone. Next steps:")
    print("  1. Remove data_resource_sample.txt and meta_resource_sample.txt if present.")
    print("  2. Reload the study into cBioPortal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
