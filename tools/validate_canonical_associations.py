#!/usr/bin/env python3
"""Compare canonical-slide-association rows against the legacy inline query."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import re

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.constants import DEFAULT_WAREHOUSE_ID  # noqa: E402
from app.meta_store import get_patient_association_rows  # noqa: E402
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


def _normalize_value(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _normalize_row(row: dict[str, object]) -> tuple[str, ...]:
    return tuple(_normalize_value(row.get(field)) for field in _COMPARE_FIELDS)


def _counter(rows: list[dict[str, object]]) -> Counter[tuple[str, ...]]:
    return Counter(_normalize_row(row) for row in rows)


def compare_rows(
    canonical_rows: list[dict[str, object]],
    legacy_rows: list[dict[str, object]],
) -> dict[str, object]:
    canonical = _counter(canonical_rows)
    legacy = _counter(legacy_rows)

    missing_from_canonical = sorted(list((legacy - canonical).elements()))
    extra_in_canonical = sorted(list((canonical - legacy).elements()))

    return {
        "canonical_count": len(canonical_rows),
        "legacy_count": len(legacy_rows),
        "missing_from_canonical": missing_from_canonical,
        "extra_in_canonical": extra_in_canonical,
        "matches": not missing_from_canonical and not extra_in_canonical,
    }


def _patient_ids_from_args(args: argparse.Namespace) -> list[str]:
    patient_ids = list(args.patient_ids)
    if args.study_dir:
        patient_ids.extend(
            read_patient_ids_from_clinical_sample(
                Path(args.study_dir).expanduser().resolve()
            )
        )
    return list(dict.fromkeys(patient_ids))


def _completed_patients_from_log(log_path: Path) -> set[str]:
    completed: set[str] = set()
    pattern = re.compile(r"^(P-\d+): matches=(True|False) ")
    with log_path.open() as fh:
        for line in fh:
            match = pattern.match(line.strip())
            if match:
                completed.add(match.group(1))
    return completed


def _print_summary(
    patient_id: str,
    diff: dict[str, object],
    max_examples: int,
) -> None:
    print(
        f"{patient_id}: matches={diff['matches']} "
        f"canonical={diff['canonical_count']} legacy={diff['legacy_count']}"
    )
    missing = diff["missing_from_canonical"]
    extra = diff["extra_in_canonical"]
    if missing:
        print("  Missing from canonical:")
        for row in missing[:max_examples]:
            print(f"    {json.dumps(row)}")
    if extra:
        print("  Extra in canonical:")
        for row in extra[:max_examples]:
            print(f"    {json.dumps(row)}")
    sys.stdout.flush()


def _validate_patient(
    patient_id: str, warehouse_id: str
) -> tuple[str, dict[str, object]]:
    canonical_rows = get_patient_association_rows(
        patient_id,
        warehouse_id,
        mode="canonical",
    )
    legacy_rows = get_patient_association_rows(
        patient_id,
        warehouse_id,
        mode="legacy",
    )
    return patient_id, compare_rows(canonical_rows, legacy_rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare canonical Databricks slide associations against the legacy inline query."
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
        "--max-examples",
        type=int,
        default=5,
        help="Maximum differing rows to print from each side per patient.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of patients to validate concurrently.",
    )
    parser.add_argument(
        "--resume-log",
        help="Skip patients already present in an existing validation log.",
    )
    args = parser.parse_args(argv)

    patient_ids = _patient_ids_from_args(args)
    original_total = len(patient_ids)
    if not patient_ids:
        parser.error("provide at least one patient ID or --study-dir")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    completed_count = 0
    if args.resume_log:
        completed = _completed_patients_from_log(
            Path(args.resume_log).expanduser().resolve()
        )
        completed_count = len(completed)
        patient_ids = [patient_id for patient_id in patient_ids if patient_id not in completed]
        print(
            f"Resume mode: skipped={completed_count} remaining={len(patient_ids)} total={original_total}",
        )
        sys.stdout.flush()
        if not patient_ids:
            print("No remaining patients to validate.")
            return 0

    mismatch_count = 0
    processed = 0
    total = len(patient_ids)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_validate_patient, patient_id, args.warehouse_id): patient_id
            for patient_id in patient_ids
        }
        for future in as_completed(futures):
            patient_id, diff = future.result()
            processed += 1
            _print_summary(patient_id, diff, args.max_examples)
            print(
                f"Progress: {completed_count + processed}/{completed_count + total}"
            )
            sys.stdout.flush()
            if not diff["matches"]:
                mismatch_count += 1

    return 1 if mismatch_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
