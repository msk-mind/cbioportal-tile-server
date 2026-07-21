#!/usr/bin/env python3
"""
Remove legacy WSI sample timepoint attributes from a cBioPortal study directory.

The patient and slide pathology timelines now derive from the shared pathology
ETL rather than sample-level proxy columns in `data_clinical_sample.txt`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tools.study_file_cleanup import (
    LEGACY_WSI_TIMEPOINT_ATTRIBUTE_IDS,
    remove_sample_attributes,
)

_LEGACY_FILES = [
    "meta_clinical_sample_wsi_timepoint.txt",
    "data_clinical_sample_wsi_timepoint.txt",
]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--study-dir",
        required=True,
        help="Path to the cBioPortal study directory.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    study_dir = Path(args.study_dir).expanduser().resolve()
    if not study_dir.is_dir():
        print(f"ERROR: study directory not found: {study_dir}", file=sys.stderr)
        return 1

    result = remove_sample_attributes(
        study_dir,
        LEGACY_WSI_TIMEPOINT_ATTRIBUTE_IDS,
        extra_files=_LEGACY_FILES,
    )
    removed_attributes = result["removed_attributes"]
    removed_files = result["removed_files"]

    print(f"Study dir: {study_dir}")
    print(
        "Removed sample timepoint attributes:",
        ", ".join(removed_attributes) if removed_attributes else "none",
    )
    print("Removed files:", ", ".join(removed_files) if removed_files else "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
