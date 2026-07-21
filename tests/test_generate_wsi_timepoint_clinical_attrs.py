from pathlib import Path

from tools.generate_wsi_timepoint_clinical_attrs import (
    _merge_timepoints_into_clinical_sample,
    _read_sample_ids,
    _timepoint_bin,
)


def test_read_sample_ids_reads_sample_id_column(tmp_path: Path):
    study_dir = tmp_path / "study"
    study_dir.mkdir()
    (study_dir / "data_clinical_sample.txt").write_text(
        "#Sample Identifier\tPatient Identifier\n"
        "#Sample identifier\tPatient identifier\n"
        "#STRING\tSTRING\n"
        "#1\t1\n"
        "SAMPLE_ID\tPATIENT_ID\n"
        "S-0001\tP-0001\n"
        "S-0002\tP-0002\n",
        encoding="utf-8",
    )

    assert _read_sample_ids(study_dir) == ["S-0001", "S-0002"]


def test_timepoint_bin_uses_sequencing_labels():
    assert _timepoint_bin(None) == "Unknown"
    assert _timepoint_bin(-5) == "Pre-sequencing"
    assert _timepoint_bin(0) == "Sequencing"
    assert _timepoint_bin(15) == "1-30 days"


def test_merge_timepoints_into_clinical_sample_adds_wsi_timepoint_columns(
    tmp_path: Path,
):
    study_dir = tmp_path / "study"
    study_dir.mkdir()
    (study_dir / "data_clinical_sample.txt").write_text(
        "#Sample Identifier\tPatient Identifier\n"
        "#Sample identifier\tPatient identifier\n"
        "#STRING\tSTRING\n"
        "#1\t1\n"
        "SAMPLE_ID\tPATIENT_ID\n"
        "S-0001\tP-0001\n"
        "S-0002\tP-0002\n",
        encoding="utf-8",
    )

    counts = _merge_timepoints_into_clinical_sample(
        study_dir,
        {
            "S-0001": (
                -174,
                "Procedure date (surgical specimen diagnoses)",
            )
        },
    )

    assert counts == {"matched": 1, "unknown": 1}

    lines = (study_dir / "data_clinical_sample.txt").read_text().splitlines()
    assert "WSI_TIMEPOINT_DAYS" in lines[4]
    assert "WSI_TIMEPOINT_SOURCE" in lines[4]
    assert lines[5].endswith(
        "\tPre-sequencing\t-174\tProcedure date (surgical specimen diagnoses)"
    )
    assert lines[6].endswith("\tUnknown\t\t")
