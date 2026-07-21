from pathlib import Path

from tools.generate_wsi_clinical_attrs import (
    _apply_patient_summaries,
    _merge_rows_into_clinical_sample,
    _read_sample_ids,
    _write_data,
)


def test_apply_patient_summaries_includes_unmatched_sample_rows():
    rows = _apply_patient_summaries(
        ["P-0001-T01", "P-0001-T02"],
        {
            "P-0001-T01": "P-0001",
            "P-0001-T02": "P-0001",
        },
        [
            {
                "patient_id": "P-0001",
                "sample_id": "P-0001-T01",
                "servable_slide_count": 14,
                "non_servable_hne_slide_count": 2,
                "non_servable_ihc_slide_count": 1,
                "has_hne": 1,
                "has_ihc": 1,
                "stain_types": "H&E;IHC",
            }
        ],
    )

    assert [row["sample_id"] for row in rows] == [
        "P-0001-T01",
        "P-0001-T02",
    ]
    assert [row["servable_slide_count"] for row in rows] == [14, 14]
    assert [row["non_servable_hne_slide_count"] for row in rows] == [2, 2]


def test_read_sample_ids_uses_sample_id_column(tmp_path: Path):
    study_dir = tmp_path / "study"
    study_dir.mkdir()
    (study_dir / "data_clinical_sample.txt").write_text(
        "# comment\n"
        "SAMPLE_ID\tPATIENT_ID\tOTHER\n"
        "S-0001\tP-0001\tx\n"
        "S-0002\tP-0002\ty\n",
        encoding="utf-8",
    )

    assert _read_sample_ids(study_dir) == ["S-0001", "S-0002"]


def test_write_data_includes_non_servable_counts(tmp_path: Path):
    study_dir = tmp_path / "study"
    study_dir.mkdir()

    _write_data(
        study_dir,
        [
            {
                "patient_id": "P-0001",
                "sample_id": "S-0001",
                "servable_slide_count": 2,
                "has_hne": 1,
                "has_ihc": 0,
                "non_servable_hne_slide_count": 3,
                "non_servable_ihc_slide_count": 4,
                "stain_types": "H&E",
            }
        ],
    )

    lines = (study_dir / "data_clinical_sample_wsi.txt").read_text().splitlines()
    assert "WSI_NON_SERVABLE_HNE_SLIDE_COUNT" in lines[4]
    assert "WSI_NON_SERVABLE_IHC_SLIDE_COUNT" in lines[4]
    assert lines[5].endswith("\t3\t4\tH&E")


def test_merge_rows_into_clinical_sample_adds_wsi_columns(tmp_path: Path):
    study_dir = tmp_path / "study"
    study_dir.mkdir()
    (study_dir / "data_clinical_sample.txt").write_text(
        "#Sample Identifier\tPatient Identifier\n"
        "#Sample identifier\tPatient identifier\n"
        "#STRING\tSTRING\n"
        "#1\t1\n"
        "SAMPLE_ID\tPATIENT_ID\n"
        "S-0001\tP-0001\n",
        encoding="utf-8",
    )

    _merge_rows_into_clinical_sample(
        study_dir,
        [
            {
                "patient_id": "P-0001",
                "sample_id": "S-0001",
                "servable_slide_count": 2,
                "has_hne": 1,
                "has_ihc": 0,
                "non_servable_hne_slide_count": 3,
                "non_servable_ihc_slide_count": 4,
                "stain_types": "H&E",
            }
        ],
    )

    lines = (study_dir / "data_clinical_sample.txt").read_text().splitlines()
    assert "WSI_NON_SERVABLE_HNE_SLIDE_COUNT" in lines[4]
    assert "WSI_NON_SERVABLE_IHC_SLIDE_COUNT" in lines[4]
    assert lines[5].endswith("\tTRUE\t2\tTRUE\tFALSE\t3\t4\tH&E")
