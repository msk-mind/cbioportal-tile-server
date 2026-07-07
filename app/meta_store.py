"""
Databricks SQL transport and raw query definitions for slide metadata.
"""

from __future__ import annotations

from typing import Any

from .constants import DEID_TABLE as _TABLE, INVENTORY_TABLE as _INVENTORY, SUMMARY_TABLE as _SUMMARY

PATIENT_SQL = f"""
SELECT
    d.image_id, d.PATIENT_ID, d.sample_id,
    d.block_id, d.block_label, d.part_type, d.part_description,
    d.PATH_DX_SPEC_TITLE AS path_dx_title,
    d.stain_name, d.stain_group,
    d.CANCER_TYPE, d.CANCER_TYPE_DETAILED, d.ONCOTREE_CODE,
    d.PRIMARY_SITE, d.SAMPLE_TYPE, d.METASTATIC_SITE, d.TUMOR_PURITY,
    d.ONCOGENIC_MUTATIONS, d.`#ONCOGENIC_MUTATIONS` AS NUM_ONCOGENIC_MUTATIONS,
    d.CVR_TMB_SCORE, d.MSI_TYPE, d.magnification,
    d.file_size_bytes,
    s.path AS slide_path
FROM {_TABLE} d
LEFT JOIN {_INVENTORY} s ON d.image_id = s.image_id
WHERE d.PATIENT_ID = :patient_id
ORDER BY d.sample_id, d.block_id, d.image_id
"""

SLIDE_SQL = f"""
SELECT *
FROM {_TABLE}
WHERE image_id = :image_id
LIMIT 1
"""

SLIDE_PATH_SQL = f"""
SELECT path FROM {_INVENTORY}
WHERE image_id = :image_id
LIMIT 1
"""

SEARCH_PATIENT_SQL = f"""
SELECT DISTINCT PATIENT_ID AS patient_id,
       MAX(CANCER_TYPE) AS cancer_type,
       COUNT(*) AS slide_count
FROM {_TABLE}
WHERE PATIENT_ID LIKE :prefix
GROUP BY patient_id
ORDER BY patient_id
LIMIT 8
"""

SEARCH_SLIDE_SQL = f"""
SELECT image_id, PATIENT_ID AS patient_id, stain_name
FROM {_TABLE}
WHERE CAST(image_id AS STRING) LIKE :prefix
ORDER BY image_id
LIMIT 8
"""

SEARCH_SAMPLE_SQL = f"""
SELECT DISTINCT sample_id, PATIENT_ID AS patient_id,
       MAX(CANCER_TYPE) AS cancer_type
FROM {_TABLE}
WHERE sample_id LIKE :prefix
GROUP BY sample_id, patient_id
ORDER BY sample_id
LIMIT 8
"""


def param(name: str, value: str, ptype: str = "STRING"):
    from databricks.sdk.service.sql import StatementParameterListItem  # noqa: PLC0415

    return StatementParameterListItem(name=name, value=value, type=ptype)


def client():
    from databricks.sdk import WorkspaceClient  # noqa: PLC0415

    if not hasattr(client, "_inst"):
        client._inst = WorkspaceClient()  # type: ignore[attr-defined]
    return client._inst  # type: ignore[attr-defined]


def run_query(sql: str, warehouse_id: str, params: list | None = None) -> list[dict[str, Any]]:
    import time  # noqa: PLC0415

    from databricks.sdk.service.sql import StatementState  # noqa: PLC0415

    stmt = client().statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=sql,
        parameters=params or [],
        wait_timeout="50s",
    )

    poll_interval = 2
    max_poll = 120
    elapsed = 0
    while stmt.status.state in (StatementState.RUNNING, StatementState.PENDING):
        if elapsed >= max_poll:
            try:
                client().statement_execution.cancel_execution(stmt.statement_id)
            except Exception:
                pass
            raise RuntimeError("Databricks query timed out")
        time.sleep(poll_interval)
        elapsed += poll_interval
        stmt = client().statement_execution.get_statement(stmt.statement_id)

    if stmt.status.state != StatementState.SUCCEEDED:
        err = getattr(stmt.status, "error", None)
        raise RuntimeError(f"Databricks query failed: {err}")

    columns = [c.name for c in stmt.manifest.schema.columns]
    return [dict(zip(columns, row)) for row in (stmt.result.data_array or [])]


def get_patient_rows(patient_id: str, warehouse_id: str) -> list[dict[str, Any]]:
    return run_query(PATIENT_SQL, warehouse_id, [param("patient_id", patient_id)])


def get_slide_row(image_id: str, warehouse_id: str) -> dict[str, Any] | None:
    rows = run_query(SLIDE_SQL, warehouse_id, [param("image_id", str(image_id))])
    return rows[0] if rows else None


def get_slide_path_row(image_id: str, warehouse_id: str) -> dict[str, Any] | None:
    rows = run_query(SLIDE_PATH_SQL, warehouse_id, [param("image_id", str(image_id))])
    return rows[0] if rows else None


def search_patient_rows(prefix: str, warehouse_id: str) -> list[dict[str, Any]]:
    return run_query(SEARCH_PATIENT_SQL, warehouse_id, [param("prefix", prefix)])


def search_sample_rows(prefix: str, warehouse_id: str) -> list[dict[str, Any]]:
    return run_query(SEARCH_SAMPLE_SQL, warehouse_id, [param("prefix", prefix)])


def search_slide_rows(prefix: str, warehouse_id: str) -> list[dict[str, Any]]:
    return run_query(SEARCH_SLIDE_SQL, warehouse_id, [param("prefix", prefix)])


def get_sample_summary_rows(sample_ids: list[str], warehouse_id: str) -> list[dict[str, Any]]:
    placeholders = ", ".join(f"'{sid.replace(chr(39), '')}'" for sid in sample_ids)
    sql = f"""
SELECT
    sample_id,
    patient_id,
    servable_slide_count,
    has_hne,
    has_ihc,
    stain_types
FROM {_SUMMARY}
WHERE sample_id IN ({placeholders})
ORDER BY sample_id
"""
    return run_query(sql, warehouse_id)
