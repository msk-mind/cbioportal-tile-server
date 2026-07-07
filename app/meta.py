"""
Databricks metadata layer — patient / slide hierarchy.

Table names are defined in app/constants.py (DEID_TABLE, INVENTORY_TABLE).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from . import meta_store

logger = logging.getLogger(__name__)
_run_query = meta_store.run_query
_param = meta_store.param


def _infer_stain_flags(stain_group: str | None, stain_name: str | None) -> tuple[bool, bool]:
    group = (stain_group or "").lower()
    name = (stain_name or "").lower()
    haystack = f"{group} {name}"
    is_hne = "h&e" in haystack
    is_ihc = "ihc" in group or (not is_hne and any(token in haystack for token in ("pd-l1", "her2", "ki-67", "er", "pr")))
    return is_hne, is_ihc


def _derive_block_fields(block_id: str | None, block_label: str | None) -> tuple[int | None, str, str | None]:
    part_number: int | None = None
    block_number = ""
    part_designator: str | None = None
    source = block_id or ""
    if source:
        match = re.search(r"/(\d+)-([^/]+)$", source)
        if match:
            part_number = int(match.group(1))
            raw_block = match.group(2).strip()
            label = (block_label or raw_block).strip()
            block_number_match = re.match(r"^(\d+)", raw_block)
            block_number = block_number_match.group(1) if block_number_match else raw_block
            part_designator = str(part_number)
            return part_number, block_number, label
    label = (block_label or "").strip()
    block_number_match = re.match(r"^(\d+)", label)
    block_number = block_number_match.group(1) if block_number_match else label
    return part_number, block_number, label or None


def _coerce(v: Any) -> Any:
    """Convert non-JSON-safe types (Decimal, etc.) to native Python."""
    if v is None:
        return None
    try:
        from decimal import Decimal  # noqa: PLC0415
        if isinstance(v, Decimal):
            return float(v)
    except ImportError:
        pass
    return v


def _slide_sort_key(sl: dict) -> tuple:
    stain_order = {
        "h&e (initial)": 0,
        "h&e (other)": 1,
        "ihc": 2,
    }
    group = (sl.get("stain_group") or "").lower()
    order = stain_order.get(group, 3)
    return (order, (sl.get("stain_name") or "").lower())


def _new_sample(row: dict, sample_id: str) -> dict:
    return {
        "sample_id": sample_id,
        "cancer_type": row.get("CANCER_TYPE"),
        "cancer_type_detailed": row.get("CANCER_TYPE_DETAILED"),
        "oncotree_code": row.get("ONCOTREE_CODE"),
        "primary_site": row.get("PRIMARY_SITE"),
        "sample_type": row.get("SAMPLE_TYPE"),
        "metastatic_site": row.get("METASTATIC_SITE"),
        "tumor_purity": _coerce(row.get("TUMOR_PURITY")),
        "oncogenic_mutations": row.get("ONCOGENIC_MUTATIONS"),
        "num_oncogenic_mutations": _coerce(row.get("NUM_ONCOGENIC_MUTATIONS")),
        "tmb_score": _coerce(row.get("CVR_TMB_SCORE")),
        "msi_type": row.get("MSI_TYPE"),
        "parts": {},
    }


def _new_part(row: dict, part_number: int | None) -> dict:
    return {
        "part_number": part_number,
        "part_designator": str(part_number) if part_number is not None else None,
        "part_type": row.get("part_type"),
        "part_description": row.get("part_description"),
        "subspecialty": None,
        "path_dx_title": row.get("part_description"),
        "blocks": {},
    }


def _new_slide(row: dict, block_label: str | None, block_number: str, is_hne: bool, is_ihc: bool, can_serve: bool) -> dict:
    return {
        "image_id": str(row.get("image_id")),
        "stain_name": row.get("stain_name"),
        "stain_group": row.get("stain_group"),
        "is_hne": is_hne,
        "is_ihc": is_ihc,
        "magnification": row.get("magnification"),
        "file_size_bytes": _coerce(row.get("file_size_bytes")),
        "can_serve_tiles": can_serve,
        "barcode": None,
        "block_label": block_label,
        "block_number": block_number,
        "part_description": row.get("part_description"),
        "path_dx_title": row.get("part_description"),
    }


def _assemble_patient_hierarchy(rows: list[dict[str, Any]], patient_id: str) -> dict:
    samples: dict[str, dict] = {}

    for row in rows:
        sample_id = row.get("sample_id") or ""
        part_number, derived_block_number, derived_block_label = _derive_block_fields(
            row.get("block_id"),
            row.get("block_label"),
        )
        block_number = str(derived_block_number or "")
        part_key = str(part_number) if part_number is not None else "?"
        slide_url = row.get("slide_path") or ""
        can_serve = slide_url.startswith("s3://")
        is_hne, is_ihc = _infer_stain_flags(row.get("stain_group"), row.get("stain_name"))

        sample = samples.setdefault(sample_id, _new_sample(row, sample_id))
        part = sample["parts"].setdefault(part_key, _new_part(row, part_number))
        block = part["blocks"].setdefault(
            block_number,
            {
                "block_number": block_number,
                "block_label": derived_block_label,
                "slides": [],
            },
        )
        block["slides"].append(
            _new_slide(row, derived_block_label, block_number, is_hne, is_ihc, can_serve)
        )

    result: list[dict] = []
    for sample in samples.values():
        parts_list = []
        for part in sample["parts"].values():
            blocks_list = []
            for block in part["blocks"].values():
                block["slides"].sort(key=_slide_sort_key)
                blocks_list.append(block)
            blocks_list.sort(key=lambda block: (0 if not (block.get("block_label") or "").strip() else 1))
            part_out = {k: v for k, v in part.items() if k != "blocks"}
            part_out["blocks"] = blocks_list
            parts_list.append(part_out)
        sample_out = {k: v for k, v in sample.items() if k != "parts"}
        sample_out["parts"] = parts_list
        result.append(sample_out)

    return {"patient_id": patient_id, "samples": result}


def _format_patient_suggestions(rows: list[dict[str, Any]]) -> list[dict]:
    return [
        {
            "type": "patient",
            "id": row["patient_id"],
            "label": row["patient_id"],
            "sublabel": f"{row.get('cancer_type') or ''} · {row.get('slide_count', '')} slides".strip(" ·"),
        }
        for row in rows
    ]


def _format_sample_suggestions(rows: list[dict[str, Any]]) -> list[dict]:
    return [
        {
            "type": "sample",
            "id": row["sample_id"],
            "label": row["sample_id"],
            "sublabel": row.get("cancer_type") or "",
        }
        for row in rows
    ]


def _format_slide_suggestions(rows: list[dict[str, Any]]) -> list[dict]:
    return [
        {
            "type": "slide",
            "id": str(row["image_id"]),
            "label": str(row["image_id"]),
            "sublabel": f"{row.get('patient_id') or ''} · {row.get('stain_name') or ''}".strip(" ·"),
        }
        for row in rows
    ]


def get_patient_hierarchy(patient_id: str, warehouse_id: str) -> dict | None:
    """
    Return a nested hierarchy dict:
      { patient_id, samples: [{ sample_id, cancer_type, …, parts: [{ part_number, …,
        blocks: [{ block_number, …, slides: [{ image_id, stain_name, … }] }] }] }] }
    Returns None if patient is not found in the table.
    """
    rows = _run_query(meta_store.PATIENT_SQL, warehouse_id, [_param("patient_id", patient_id)])
    if not rows:
        return None
    return _assemble_patient_hierarchy(rows, patient_id)


def get_slide_dbmeta(image_id: str, warehouse_id: str) -> dict | None:
    """Return flat Databricks metadata row for one slide."""
    rows = _run_query(meta_store.SLIDE_SQL, warehouse_id, [_param("image_id", str(image_id))])
    if not rows:
        return None
    return {k: _coerce(v) for k, v in rows[0].items()}


def get_slide_path(image_id: str, warehouse_id: str) -> str | None:
    """Return the S3 URI for a slide given its image_id, or None if not found."""
    rows = _run_query(meta_store.SLIDE_PATH_SQL, warehouse_id, [_param("image_id", str(image_id))])
    if not rows:
        return None
    return rows[0].get("path") or None


def search_suggestions(query: str, warehouse_id: str) -> list[dict]:
    """
    Return up to 8 autocomplete suggestions for the given query string.

    Detects query type by pattern:
      P-<digits>            → patient suggestions
      P-<digits>-T<digits>  → sample suggestions
      <digits>              → slide image_id suggestions

    Each result has: { type, id, label, sublabel }
    """
    import re  # noqa: PLC0415

    q = query.strip()
    if not q:
        return []

    prefix = q.replace("%", r"\%").replace("_", r"\_") + "%"

    # Sample ID pattern: P-digits-T
    if re.match(r"^P-\d.*-T", q, re.IGNORECASE):
        rows = _run_query(meta_store.SEARCH_SAMPLE_SQL, warehouse_id, [_param("prefix", prefix)])
        return _format_sample_suggestions(rows)

    # Patient ID pattern: starts with P-
    if re.match(r"^P-", q, re.IGNORECASE):
        rows = _run_query(meta_store.SEARCH_PATIENT_SQL, warehouse_id, [_param("prefix", prefix)])
        return _format_patient_suggestions(rows)

    # Numeric → slide image_id
    if re.match(r"^\d", q):
        rows = _run_query(meta_store.SEARCH_SLIDE_SQL, warehouse_id, [_param("prefix", prefix)])
        return _format_slide_suggestions(rows)

    return []


# ---------------------------------------------------------------------------
# Slide summary (Phase 7)
# ---------------------------------------------------------------------------

def get_sample_slide_summary(
    sample_ids: list[str],
    warehouse_id: str,
) -> list[dict]:
    """
    Return pre-computed slide availability stats for the given sample IDs.

    Reads from the ``sample_wsi_summary`` Delta table, which is populated
    nightly by the Databricks Asset Bundle job (``wsi-summary-pipeline``).

    Each result dict has:
      sample_id, patient_id, servable_slide_count, has_hne, has_ihc, stain_types

    Samples not present in the summary table are silently omitted — the caller
    (generate_wsi_clinical_attrs.py) fills in zero-count rows for them.
    """
    if not sample_ids:
        return []
    placeholders = ", ".join(f"'{sid.replace(chr(39), '')}'" for sid in sample_ids)
    rows = _run_query(
        f"""
SELECT
    sample_id,
    patient_id,
    servable_slide_count,
    has_hne,
    has_ihc,
    stain_types
FROM {meta_store._SUMMARY}
WHERE sample_id IN ({placeholders})
ORDER BY sample_id
""",
        warehouse_id,
    )
    return [
        {
            "sample_id":            r.get("sample_id"),
            "patient_id":           r.get("patient_id"),
            "servable_slide_count": int(r.get("servable_slide_count") or 0),
            "has_hne":              int(r.get("has_hne") or 0),
            "has_ihc":              int(r.get("has_ihc") or 0),
            "stain_types":          r.get("stain_types") or "",
        }
        for r in rows
    ]


def get_live_sample_slide_summary(
    sample_ids: list[str],
    warehouse_id: str,
) -> list[dict]:
    """
    Return current slide availability stats for the given sample IDs.

    Unlike ``get_sample_slide_summary()``, this computes counts directly from
    the live de-identified slide metadata table joined to slide_inventory.
    This avoids stale results when the nightly summary table is out of date.
    """
    if not sample_ids:
        return []
    placeholders = ", ".join(f"'{sid.replace(chr(39), '')}'" for sid in sample_ids)
    rows = _run_query(
        f"""
SELECT
    d.sample_id AS sample_id,
    d.PATIENT_ID AS patient_id,
    COUNT(DISTINCT CASE
        WHEN s.path LIKE 's3://%'
         AND (
            LOWER(COALESCE(d.stain_group, d.stain_name, '')) LIKE '%h&e%'
            OR LOWER(COALESCE(d.stain_group, '')) LIKE '%ihc%'
         )
        THEN d.image_id
        ELSE NULL
    END) AS servable_slide_count,
    MAX(CASE
        WHEN LOWER(COALESCE(d.stain_group, d.stain_name, '')) LIKE '%h&e%'
         AND s.path LIKE 's3://%'
        THEN 1 ELSE 0
    END) AS has_hne,
    MAX(CASE
        WHEN LOWER(COALESCE(d.stain_group, '')) LIKE '%ihc%'
         AND s.path LIKE 's3://%'
        THEN 1 ELSE 0
    END) AS has_ihc,
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
    ) AS stain_types
FROM {meta_store._TABLE} d
LEFT JOIN {meta_store._INVENTORY} s ON d.image_id = s.image_id
WHERE d.sample_id IN ({placeholders})
GROUP BY d.sample_id, d.PATIENT_ID
ORDER BY d.sample_id
""",
        warehouse_id,
    )
    return [
        {
            "sample_id":            r.get("sample_id"),
            "patient_id":           r.get("patient_id"),
            "servable_slide_count": int(r.get("servable_slide_count") or 0),
            "has_hne":              int(r.get("has_hne") or 0),
            "has_ihc":              int(r.get("has_ihc") or 0),
            "stain_types":          r.get("stain_types") or "",
        }
        for r in rows
    ]
