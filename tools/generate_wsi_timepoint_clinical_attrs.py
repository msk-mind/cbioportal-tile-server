#!/usr/bin/env python3
"""
Merge WSI sample timepoint columns into data_clinical_sample.txt.

This uses the matched IMPACT sample timeline as a proxy for H&E slide banking
time. For each sample we prefer:
  1. Sample acquisition day
  2. Sequencing day

Columns added to data_clinical_sample.txt:
  WSI_TIMEPOINT_BIN
  WSI_TIMEPOINT_DAYS
  WSI_TIMEPOINT_SOURCE
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

_ATTRIBUTES = [
    (
        'WSI_TIMEPOINT_BIN',
        'WSI Timepoint',
        'Proxy H&E slide timepoint from matched IMPACT sample timeline',
        'STRING',
        '950',
    ),
    (
        'WSI_TIMEPOINT_DAYS',
        'WSI Timepoint Days',
        'Days since diagnosis for matched IMPACT sample timeline proxy',
        'NUMBER',
        '0',
    ),
    (
        'WSI_TIMEPOINT_SOURCE',
        'WSI Timepoint Source',
        'Timeline event used for matched IMPACT sample timepoint proxy',
        'STRING',
        '0',
    ),
]


def _read_timeline_days(path: Path, event_filter: str) -> dict[str, int]:
    days_by_sample: dict[str, int] = {}
    if not path.exists():
        return days_by_sample

    with path.open(newline='') as fh:
        reader = csv.DictReader(fh, delimiter='\t')
        for row in reader:
            sample_id = (row.get('SAMPLE_ID') or '').strip()
            event_type = (row.get('EVENT_TYPE') or '').strip().lower()
            if not sample_id or event_filter not in event_type:
                continue
            start_date = (row.get('START_DATE') or '').strip()
            if not start_date:
                continue
            try:
                days = int(float(start_date))
            except ValueError:
                continue
            prev = days_by_sample.get(sample_id)
            if prev is None or days < prev:
                days_by_sample[sample_id] = days
    return days_by_sample


def _timepoint_bin(days: int | None) -> str:
    if days is None:
        return 'Unknown'
    if days < 0:
        return 'Pre-diagnosis'
    if days == 0:
        return 'Diagnosis'
    if days <= 30:
        return '1-30 days'
    if days <= 90:
        return '31-90 days'
    if days <= 365:
        return '91-365 days'
    return '>365 days'


def _read_clinical_sample(path: Path) -> tuple[list[list[str]], list[str], list[list[str]]]:
    comment_rows: list[list[str]] = []
    header: list[str] | None = None
    data_rows: list[list[str]] = []

    with path.open(newline='') as fh:
        for raw_line in fh:
            line = raw_line.rstrip('\n')
            if line.startswith('#'):
                comment_rows.append(line[1:].split('\t'))
                continue
            cols = line.split('\t')
            if header is None:
                header = cols
            else:
                data_rows.append(cols)

    if header is None:
        raise ValueError(f'No header row found in {path}')
    if len(comment_rows) < 4:
        raise ValueError(f'Expected 4 comment rows in {path}, found {len(comment_rows)}')
    return comment_rows, header, data_rows


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
        row.extend([''] * (width - len(row)))
    return row


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description='Merge WSI timepoint sample clinical attributes into '
        'data_clinical_sample.txt.'
    )
    parser.add_argument(
        '--study-dir',
        required=True,
        help='Path to the cBioPortal study directory (must contain data_clinical_sample.txt).',
    )
    args = parser.parse_args(argv)

    study_dir = Path(args.study_dir).expanduser().resolve()
    if not study_dir.is_dir():
        print(f'ERROR: study directory not found: {study_dir}', file=sys.stderr)
        sys.exit(1)

    clinical_file = study_dir / 'data_clinical_sample.txt'
    if not clinical_file.exists():
        print(f'ERROR: missing {clinical_file}', file=sys.stderr)
        sys.exit(1)

    comment_rows, header, data_rows = _read_clinical_sample(clinical_file)
    sample_idx = header.index('SAMPLE_ID')
    index_by_attr = _ensure_columns(comment_rows, header)
    width = len(header)

    acquisition_days = _read_timeline_days(
        study_dir / 'data_timeline_specimen_surgery.txt',
        'sample acquisition',
    )
    sequencing_days = _read_timeline_days(
        study_dir / 'data_timeline_specimen.txt',
        'sequencing',
    )

    matched_acquisition = 0
    matched_sequencing = 0
    unknown = 0

    for row in data_rows:
        _pad_row(row, width)
        sample_id = row[sample_idx]
        acquisition = acquisition_days.get(sample_id)
        sequencing = sequencing_days.get(sample_id)

        if acquisition is not None:
            days = acquisition
            source = 'Sample acquisition'
            matched_acquisition += 1
        elif sequencing is not None:
            days = sequencing
            source = 'Sequencing'
            matched_sequencing += 1
        else:
            days = None
            source = ''
            unknown += 1

        row[index_by_attr['WSI_TIMEPOINT_BIN']] = _timepoint_bin(days)
        row[index_by_attr['WSI_TIMEPOINT_DAYS']] = '' if days is None else str(days)
        row[index_by_attr['WSI_TIMEPOINT_SOURCE']] = source

    with clinical_file.open('w', newline='') as fh:
        for comment_row in comment_rows[:4]:
            fh.write('#' + '\t'.join(_pad_row(comment_row, width)) + '\n')
        fh.write('\t'.join(header) + '\n')
        writer = csv.writer(fh, delimiter='\t', lineterminator='\n')
        for row in data_rows:
            writer.writerow(_pad_row(row, width))

    extra_meta = study_dir / 'meta_clinical_sample_wsi_timepoint.txt'
    extra_data = study_dir / 'data_clinical_sample_wsi_timepoint.txt'
    if extra_meta.exists():
        extra_meta.unlink()
    if extra_data.exists():
        extra_data.unlink()

    print(f'Updated {clinical_file}')
    print(
        'Matched:',
        f'acquisition={matched_acquisition}',
        f'sequencing={matched_sequencing}',
        f'unknown={unknown}',
    )


if __name__ == '__main__':
    main()
