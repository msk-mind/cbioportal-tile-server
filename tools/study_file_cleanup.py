from __future__ import annotations

import csv
from pathlib import Path

LEGACY_WSI_SAMPLE_ATTRIBUTE_IDS = [
    "HAS_WSI_SLIDE",
    "WSI_SLIDE_COUNT",
    "WSI_HNE_SLIDE",
    "WSI_IHC_SLIDE",
    "WSI_NON_SERVABLE_HNE_SLIDE_COUNT",
    "WSI_NON_SERVABLE_IHC_SLIDE_COUNT",
    "WSI_STAIN_TYPES",
]

LEGACY_WSI_TIMEPOINT_ATTRIBUTE_IDS = [
    "WSI_TIMEPOINT_BIN",
    "WSI_TIMEPOINT_DAYS",
    "WSI_TIMEPOINT_SOURCE",
]


def remove_sample_attributes(
    study_dir: Path,
    attribute_ids: list[str],
    extra_files: list[str] | None = None,
) -> dict[str, object]:
    clinical_path = study_dir / "data_clinical_sample.txt"
    removed_attributes: list[str] = []
    removed_files: list[str] = []

    if clinical_path.exists():
        comment_rows, header, data_rows = _read_clinical_sample(clinical_path)
        removed_indexes = [
            index for index, column_id in enumerate(header) if column_id in attribute_ids
        ]
        if removed_indexes:
            removed_attributes = [header[index] for index in removed_indexes]
            for index in sorted(removed_indexes, reverse=True):
                del header[index]
                for comment_row in comment_rows:
                    if index < len(comment_row):
                        del comment_row[index]
                for row in data_rows:
                    if index < len(row):
                        del row[index]
            _write_clinical_sample(clinical_path, comment_rows, header, data_rows)

    for relative_path in extra_files or []:
        path = study_dir / relative_path
        if path.exists():
            path.unlink()
            removed_files.append(relative_path)

    return {
        "removed_attributes": removed_attributes,
        "removed_files": removed_files,
    }


def _read_clinical_sample(
    path: Path,
) -> tuple[list[list[str]], list[str], list[list[str]]]:
    comment_rows: list[list[str]] = []
    header: list[str] | None = None
    data_rows: list[list[str]] = []

    with path.open(newline="", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            if line.startswith("#"):
                comment_rows.append(line[1:].split("\t"))
                continue
            columns = line.split("\t")
            if header is None:
                header = columns
            else:
                data_rows.append(columns)

    if header is None:
        raise ValueError(f"No header row found in {path}")
    if len(comment_rows) < 4:
        raise ValueError(f"Expected 4 comment rows in {path}, found {len(comment_rows)}")

    width = len(header)
    for comment_row in comment_rows:
        _pad_row(comment_row, width)
    for row in data_rows:
        _pad_row(row, width)

    return comment_rows[:4], header, data_rows


def _write_clinical_sample(
    path: Path,
    comment_rows: list[list[str]],
    header: list[str],
    data_rows: list[list[str]],
) -> None:
    width = len(header)
    with path.open("w", newline="", encoding="utf-8") as handle:
        for comment_row in comment_rows[:4]:
            handle.write("#" + "\t".join(_pad_row(comment_row, width)) + "\n")
        handle.write("\t".join(header) + "\n")
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        for row in data_rows:
            writer.writerow(_pad_row(row, width))


def _pad_row(row: list[str], width: int) -> list[str]:
    if len(row) < width:
        row.extend([""] * (width - len(row)))
    return row
