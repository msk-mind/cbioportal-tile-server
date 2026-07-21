#!/usr/bin/env python3
"""Summarize a canonical-association validation log."""

from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import Path


def summarize_log(path: Path) -> dict[str, object]:
    progress_pattern = re.compile(r"^Progress: (\d+)/(\d+)$")
    result_pattern = re.compile(
        r"^(P-\d+): matches=(True|False) canonical=(\d+) legacy=(\d+)$"
    )
    resume_pattern = re.compile(
        r"^Resume mode: skipped=(\d+) remaining=(\d+) total=(\d+)$"
    )
    legacy_resume_pattern = re.compile(
        r"^Resume mode: skipped=(\d+) remaining=(\d+)$"
    )

    completed = 0
    mismatches = 0
    last_progress = 0
    total = None
    last_progress_total = None
    last_patient = None
    resume_line = None
    skipped = 0
    remaining = None

    with path.open() as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue

            resume_match = resume_pattern.match(line)
            if resume_match:
                resume_line = line
                skipped = int(resume_match.group(1))
                remaining = int(resume_match.group(2))
                total = int(resume_match.group(3))
                continue

            legacy_resume_match = legacy_resume_pattern.match(line)
            if legacy_resume_match:
                resume_line = line
                skipped = int(legacy_resume_match.group(1))
                remaining = int(legacy_resume_match.group(2))
                if total is None:
                    total = skipped + remaining
                continue

            progress_match = progress_pattern.match(line)
            if progress_match:
                last_progress = int(progress_match.group(1))
                last_progress_total = int(progress_match.group(2))
                continue

            result_match = result_pattern.match(line)
            if result_match:
                completed += 1
                last_patient = result_match.group(1)
                if result_match.group(2) == "False":
                    mismatches += 1
                continue

    stat = path.stat()
    return {
        "path": str(path),
        "completed": completed,
        "mismatches": mismatches,
        "last_progress": last_progress,
        "last_progress_total": last_progress_total,
        "skipped": skipped,
        "remaining": remaining,
        "total": total,
        "cumulative_completed": completed if total is None else skipped + last_progress,
        "last_patient": last_patient,
        "resume": resume_line,
        "size_bytes": stat.st_size,
        "modified_at": time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize a canonical-association validation log."
    )
    parser.add_argument("log_path", help="Path to the validation log file.")
    args = parser.parse_args(argv)

    path = Path(args.log_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)

    summary = summarize_log(path)
    for key, value in summary.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
