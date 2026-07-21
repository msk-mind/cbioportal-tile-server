#!/usr/bin/env python3
"""
Generate cBioPortal clinical sample attribute files for WSI slide availability.

Reads WSI slide metadata from Databricks and produces two files for cBioPortal
study import:

  meta_clinical_sample_wsi.txt
  data_clinical_sample_wsi.txt

These files add filterable Study View attributes per sample:
  HAS_WSI_SLIDE    — Yes / No (pie chart)
  WSI_SLIDE_COUNT  — integer count (bar chart)
  WSI_HNE_SLIDE    — Yes / No (pie chart)
  WSI_IHC_SLIDE    — Yes / No (pie chart)
  WSI_NON_SERVABLE_HNE_SLIDE_COUNT  — integer count
  WSI_NON_SERVABLE_IHC_SLIDE_COUNT  — integer count
  WSI_STAIN_TYPES  — semicolon-joined stain names (table)

Usage:
  python tools/generate_wsi_clinical_attrs.py \\
      --study-dir /path/to/private/automation_tool_datasets/coad_msk_2025

  # Query the live slide metadata tables:
  python tools/generate_wsi_clinical_attrs.py \\
      --study-dir /path/to/study \\
      --live

  # Override the Databricks warehouse:
  python tools/generate_wsi_clinical_attrs.py \\
      --study-dir /path/to/study \\
      --warehouse-id 0b49b7d78734ad5c

Credentials:
  Databricks: reads ~/.databrickscfg (DEFAULT profile) or env vars
              DATABRICKS_HOST + DATABRICKS_TOKEN.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

# Allow importing from the app package when running from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.constants import DEFAULT_WAREHOUSE_ID as _DEFAULT_WAREHOUSE  # noqa: E402
from app.meta import get_live_sample_slide_summary, get_sample_slide_summary  # noqa: E402
from tools.patient_cache_utils import invalidate_study_patient_cache  # noqa: E402

# ---------------------------------------------------------------------------
# cBioPortal file format constants
# ---------------------------------------------------------------------------

_ATTRIBUTES = [
    # (column_id, display_name, description, datatype, priority)
    ("HAS_WSI_SLIDE", "Has WSI Slide", "Any viewable WSI tile for this patient", "BOOLEAN", "1"),
    ("WSI_SLIDE_COUNT", "WSI Slide Count (Viewable)", "Patient-wide viewable slide count", "NUMBER", "1"),
    ("WSI_HNE_SLIDE", "Has H&E Slide", "H&E slide available for this patient", "BOOLEAN", "1"),
    ("WSI_IHC_SLIDE", "Has IHC Slide", "IHC slide available for this patient", "BOOLEAN", "1"),
    (
        "WSI_NON_SERVABLE_HNE_SLIDE_COUNT",
        "Non-viewable H&E Slide Count",
        "Patient-wide diagnostic H&E slides not currently viewable",
        "NUMBER",
        "1",
    ),
    (
        "WSI_NON_SERVABLE_IHC_SLIDE_COUNT",
        "Non-viewable IHC Slide Count",
        "Patient-wide diagnostic IHC slides not currently viewable",
        "NUMBER",
        "1",
    ),
    ("WSI_STAIN_TYPES", "WSI Stain Types", "Patient-wide semicolon-separated stains", "STRING", "1"),
]

_META_TEMPLATE = """\
cancer_study_identifier: {study_id}
genetic_alteration_type: CLINICAL
datatype: SAMPLE_ATTRIBUTES
data_filename: data_clinical_sample_wsi.txt
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_sample_ids(study_dir: Path) -> list[str]:
    """Read SAMPLE_IDs from data_clinical_sample.txt in the study directory."""
    clinical_file = study_dir / "data_clinical_sample.txt"
    if not clinical_file.exists():
        raise FileNotFoundError(
            f"data_clinical_sample.txt not found in {study_dir}. "
            "Run with --study-dir pointing to the study root."
        )
    sample_ids: list[str] = []
    with clinical_file.open(newline="") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if cols[0] == "SAMPLE_ID":
                continue
            if cols and cols[0]:
                sample_ids.append(cols[0])
    return sample_ids


def _infer_study_id(study_dir: Path) -> str:
    """Read cancer_study_identifier from meta_study.txt, or fall back to dir name."""
    meta_study = study_dir / "meta_study.txt"
    if meta_study.exists():
        for line in meta_study.read_text().splitlines():
            if line.startswith("cancer_study_identifier:"):
                return line.split(":", 1)[1].strip()
    return study_dir.name


def _write_meta(study_dir: Path, study_id: str) -> None:
    path = study_dir / "meta_clinical_sample_wsi.txt"
    path.write_text(_META_TEMPLATE.format(study_id=study_id))
    print(f"  Wrote {path}")


def _write_data(study_dir: Path, rows: list[dict]) -> None:
    path = study_dir / "data_clinical_sample_wsi.txt"

    attr_ids   = [a[0] for a in _ATTRIBUTES]
    disp_names = [a[1] for a in _ATTRIBUTES]
    descs      = [a[2] for a in _ATTRIBUTES]
    dtypes     = [a[3] for a in _ATTRIBUTES]
    priorities = [a[4] for a in _ATTRIBUTES]

    with path.open("w", newline="") as fh:
        # cBioPortal 5-line header
        fh.write("#" + "\t".join(["Patient Identifier", "Sample Identifier"] + disp_names) + "\n")
        fh.write("#" + "\t".join(["Patient identifier", "Sample identifier"] + descs) + "\n")
        fh.write("#" + "\t".join(["STRING", "STRING"] + dtypes) + "\n")
        fh.write("#" + "\t".join(["1", "1"] + priorities) + "\n")
        # Column header row
        fh.write("\t".join(["PATIENT_ID", "SAMPLE_ID"] + attr_ids) + "\n")

        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        for row in rows:
            count = int(row.get("servable_slide_count") or 0)
            has_slide = "TRUE" if count > 0 else "FALSE"
            has_hne   = "TRUE" if str(row.get("has_hne") or "0") == "1" else "FALSE"
            has_ihc   = "TRUE" if str(row.get("has_ihc") or "0") == "1" else "FALSE"
            non_servable_hne_count = int(
                row.get("non_servable_hne_slide_count") or 0
            )
            non_servable_ihc_count = int(
                row.get("non_servable_ihc_slide_count") or 0
            )
            stains    = row.get("stain_types") or ""
            writer.writerow([
                row["patient_id"],
                row["sample_id"],
                has_slide,
                str(count),
                has_hne,
                has_ihc,
                str(non_servable_hne_count),
                str(non_servable_ihc_count),
                stains,
            ])

    print(f"  Wrote {path}  ({len(rows)} rows)")


def _read_clinical_sample(
    study_dir: Path,
) -> tuple[list[list[str]], list[str], list[list[str]]]:
    clinical_file = study_dir / "data_clinical_sample.txt"
    comment_rows: list[list[str]] = []
    header: list[str] | None = None
    data_rows: list[list[str]] = []

    with clinical_file.open(newline="") as fh:
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
        raise ValueError(f"No header row found in {clinical_file}")
    if len(comment_rows) < 4:
        raise ValueError(f"Expected 4 comment rows in {clinical_file}, found {len(comment_rows)}")
    return comment_rows, header, data_rows


def _pad_row(row: list[str], width: int) -> list[str]:
    if len(row) < width:
        row.extend([""] * (width - len(row)))
    return row


def _apply_patient_summaries(
    sample_ids: list[str],
    patient_by_sample: dict[str, str],
    summary_rows: list[dict],
) -> list[dict]:
    by_patient = {
        row["patient_id"]: row for row in summary_rows if row.get("patient_id")
    }
    output_rows: list[dict] = []
    for sample_id in sample_ids:
        patient_id = patient_by_sample.get(sample_id, "")
        patient_summary = by_patient.get(patient_id)
        if patient_summary:
            output_rows.append(
                {
                    **patient_summary,
                    "sample_id": sample_id,
                    "patient_id": patient_id,
                }
            )
            continue

        output_rows.append(
            {
                "sample_id": sample_id,
                "patient_id": patient_id,
                "servable_slide_count": 0,
                "non_servable_hne_slide_count": 0,
                "non_servable_ihc_slide_count": 0,
                "has_hne": 0,
                "has_ihc": 0,
                "stain_types": "",
            }
        )
    return output_rows


def _merge_rows_into_clinical_sample(study_dir: Path, rows: list[dict]) -> None:
    comment_rows, header, data_rows = _read_clinical_sample(study_dir)
    sample_idx = header.index("SAMPLE_ID")
    width = len(header)
    index_by_attr: dict[str, int] = {}

    display_names = comment_rows[0]
    descriptions = comment_rows[1]
    dtypes = comment_rows[2]
    priorities = comment_rows[3]

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

    width = len(header)
    rows_by_sample = {row["sample_id"]: row for row in rows}

    for row in data_rows:
        _pad_row(row, width)
        sample_id = row[sample_idx]
        sample_attrs = rows_by_sample.get(sample_id)
        if not sample_attrs:
            continue

        servable_slide_count = int(sample_attrs.get("servable_slide_count") or 0)
        row[index_by_attr["HAS_WSI_SLIDE"]] = (
            "TRUE" if servable_slide_count > 0 else "FALSE"
        )
        row[index_by_attr["WSI_SLIDE_COUNT"]] = str(servable_slide_count)
        row[index_by_attr["WSI_HNE_SLIDE"]] = (
            "TRUE" if str(sample_attrs.get("has_hne") or "0") == "1" else "FALSE"
        )
        row[index_by_attr["WSI_IHC_SLIDE"]] = (
            "TRUE" if str(sample_attrs.get("has_ihc") or "0") == "1" else "FALSE"
        )
        row[index_by_attr["WSI_NON_SERVABLE_HNE_SLIDE_COUNT"]] = str(
            int(sample_attrs.get("non_servable_hne_slide_count") or 0)
        )
        row[index_by_attr["WSI_NON_SERVABLE_IHC_SLIDE_COUNT"]] = str(
            int(sample_attrs.get("non_servable_ihc_slide_count") or 0)
        )
        row[index_by_attr["WSI_STAIN_TYPES"]] = sample_attrs.get("stain_types") or ""

    clinical_file = study_dir / "data_clinical_sample.txt"
    with clinical_file.open("w", newline="") as fh:
        for comment_row in comment_rows[:4]:
            fh.write("#" + "\t".join(_pad_row(comment_row, width)) + "\n")
        fh.write("\t".join(header) + "\n")
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        for row in data_rows:
            writer.writerow(_pad_row(row, width))

    print(f"  Updated {clinical_file}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Generate cBioPortal WSI clinical attribute files from the "
                    "sample_wsi_summary Delta table.",
    )
    parser.add_argument(
        "--study-dir", required=True,
        help="Path to the cBioPortal study directory (must contain "
             "data_clinical_sample.txt).",
    )
    parser.add_argument(
        "--warehouse-id", default=os.getenv("DATABRICKS_WAREHOUSE_ID", _DEFAULT_WAREHOUSE),
        help="Databricks SQL warehouse ID (default: %(default)s).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Compute attributes from the live slide metadata tables instead of "
             "the nightly sample_wsi_summary table.",
    )
    parser.add_argument(
        "--merge-data-clinical-sample",
        action="store_true",
        help="Also merge the WSI attributes into data_clinical_sample.txt.",
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

    study_id   = _infer_study_id(study_dir)
    sample_ids = _read_sample_ids(study_dir)
    if not sample_ids:
        print("ERROR: no sample IDs found in data_clinical_sample.txt", file=sys.stderr)
        sys.exit(1)

    print(f"Study:     {study_id}")
    print(f"Samples:   {len(sample_ids)}")
    print(f"Warehouse: {args.warehouse_id}")
    if args.live:
        print("Querying live slide metadata…")
        summary_rows = get_live_sample_slide_summary(sample_ids, args.warehouse_id)
    else:
        print("Querying sample_wsi_summary…")
        summary_rows = get_sample_slide_summary(sample_ids, args.warehouse_id)

    # We need patient_id too — read from the clinical file
    patient_by_sample: dict[str, str] = {}
    clinical_file = study_dir / "data_clinical_sample.txt"
    with clinical_file.open(newline="") as fh:
        header_line = None
        for line in fh:
            if line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if header_line is None:
                header_line = cols
                continue
            row_dict = dict(zip(header_line, cols))
            patient_by_sample[row_dict.get("SAMPLE_ID", "")] = row_dict.get("PATIENT_ID", "")

    output_rows = _apply_patient_summaries(
        sample_ids,
        patient_by_sample,
        summary_rows,
    )

    _write_meta(study_dir, study_id)
    _write_data(study_dir, output_rows)
    if args.merge_data_clinical_sample:
        _merge_rows_into_clinical_sample(study_dir, output_rows)
    if args.invalidate_patient_cache:
        deleted, requested = invalidate_study_patient_cache(study_dir)
        print(
            "Invalidated tile-server patient cache:",
            f"requested={requested}",
            f"deleted={deleted}",
        )
    print("Done.")


if __name__ == "__main__":
    main()
