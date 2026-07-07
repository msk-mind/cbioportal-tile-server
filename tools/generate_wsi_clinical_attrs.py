#!/usr/bin/env python3
"""
Generate cBioPortal clinical sample attribute files for WSI slide availability.

Reads WSI slide metadata from Databricks and produces two files for cBioPortal
study import:

  meta_clinical_sample_wsi.txt
  data_clinical_sample_wsi.txt

These files add five filterable Study View attributes per sample:
  HAS_WSI_SLIDE    — Yes / No (pie chart)
  WSI_SLIDE_COUNT  — integer count (bar chart)
  WSI_HNE_SLIDE    — Yes / No (pie chart)
  WSI_IHC_SLIDE    — Yes / No (pie chart)
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

# ---------------------------------------------------------------------------
# cBioPortal file format constants
# ---------------------------------------------------------------------------

_ATTRIBUTES = [
    # (column_id, display_name, description, datatype, priority)
    ("HAS_WSI_SLIDE",   "Has WSI Slide",     "Any servable WSI tile",     "BINARY",  "1"),
    ("WSI_SLIDE_COUNT", "WSI Slide Count",   "Servable slide count",      "NUMBER",  "1"),
    ("WSI_HNE_SLIDE",   "Has H&E Slide",     "H&E slide available",       "BINARY",  "1"),
    ("WSI_IHC_SLIDE",   "Has IHC Slide",     "IHC slide available",       "BINARY",  "1"),
    ("WSI_STAIN_TYPES", "WSI Stain Types",   "Semicolon-separated stains","STRING",  "1"),
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
            has_slide = "Yes" if count > 0 else "No"
            has_hne   = "Yes" if str(row.get("has_hne") or "0") == "1" else "No"
            has_ihc   = "Yes" if str(row.get("has_ihc") or "0") == "1" else "No"
            stains    = row.get("stain_types") or ""
            writer.writerow([
                row["patient_id"],
                row["sample_id"],
                has_slide,
                str(count),
                has_hne,
                has_ihc,
                stains,
            ])

    print(f"  Wrote {path}  ({len(rows)} rows)")


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

    # Build a lookup keyed by sample_id so we can emit a row for every sample
    # (including those with 0 servable slides).
    by_sample: dict[str, dict] = {r["sample_id"]: r for r in summary_rows}

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

    # Build final rows
    output_rows: list[dict] = []
    for sid in sample_ids:
        row = by_sample.get(sid)
        if row:
            output_rows.append(row)
        else:
            # Sample exists in the study but has no slides in the summary
            output_rows.append({
                "sample_id":           sid,
                "patient_id":          patient_by_sample.get(sid, ""),
                "servable_slide_count": 0,
                "has_hne":             0,
                "has_ihc":             0,
                "stain_types":         "",
            })

    _write_meta(study_dir, study_id)
    _write_data(study_dir, output_rows)
    print("Done.")


if __name__ == "__main__":
    main()
