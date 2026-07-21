from pathlib import Path

from tools.validate_canonical_associations import compare_rows
from tools.validate_canonical_associations import _completed_patients_from_log


def _row(**overrides):
    row = {
        "match_level": "BLOCK",
        "patient_id": "P-0001",
        "sample_id": "P-0001-T01-IM6",
        "image_id": "1",
        "block_id": "specimen/1-A1",
        "block_label": "A1",
        "part_type": "Primary",
        "part_description": "Colon",
        "path_dx_title": "Colon resection",
        "stain_name": "H&E",
        "stain_group": "H&E (Initial)",
        "slide_path": "s3://bucket/1.svs",
        "procedure_date": "2024-01-01",
        "reference_sample_id": "P-0001-T01-IM6",
        "reference_sequencing_date": "2024-01-20",
        "slide_timepoint_days": "-19",
        "slide_timepoint_source": "Procedure date relative to tumor sequencing",
    }
    row.update(overrides)
    return row


def test_compare_rows_matches_when_normalized_rows_equal():
    diff = compare_rows([_row()], [_row()])

    assert diff["matches"] is True
    assert diff["missing_from_canonical"] == []
    assert diff["extra_in_canonical"] == []


def test_compare_rows_reports_missing_rows():
    diff = compare_rows([], [_row()])

    assert diff["matches"] is False
    assert len(diff["missing_from_canonical"]) == 1
    assert diff["extra_in_canonical"] == []


def test_compare_rows_reports_extra_rows():
    diff = compare_rows([_row()], [])

    assert diff["matches"] is False
    assert diff["missing_from_canonical"] == []
    assert len(diff["extra_in_canonical"]) == 1


def test_compare_rows_treats_none_and_empty_string_as_equal():
    diff = compare_rows(
        [_row(reference_sample_id=None, reference_sequencing_date=None)],
        [_row(reference_sample_id="", reference_sequencing_date="")],
    )

    assert diff["matches"] is True


def test_completed_patients_from_log_extracts_patient_ids(tmp_path: Path):
    log_path = tmp_path / "validation.log"
    log_path.write_text(
        "P-0002438: matches=True canonical=16 legacy=16\n"
        "Progress: 1/2780\n"
        "P-0048660: matches=False canonical=110 legacy=111\n",
        encoding="utf-8",
    )

    assert _completed_patients_from_log(log_path) == {
        "P-0002438",
        "P-0048660",
    }
