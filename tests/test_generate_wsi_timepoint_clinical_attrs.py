from pathlib import Path

from tools.generate_wsi_timepoint_clinical_attrs import main


def test_cleanup_removes_legacy_wsi_timepoint_attributes_and_sidecar_files(
    tmp_path: Path,
):
    study_dir = tmp_path / "study"
    study_dir.mkdir()
    clinical_file = study_dir / "data_clinical_sample.txt"
    clinical_file.write_text(
        "#Sample Identifier\tPatient Identifier\tWSI Timepoint\tWSI Timepoint Days\tWSI Timepoint Source\n"
        "#Sample identifier\tPatient identifier\tLegacy\tLegacy\tLegacy\n"
        "#STRING\tSTRING\tSTRING\tNUMBER\tSTRING\n"
        "#1\t1\t1\t1\t1\n"
        "SAMPLE_ID\tPATIENT_ID\tWSI_TIMEPOINT_BIN\tWSI_TIMEPOINT_DAYS\tWSI_TIMEPOINT_SOURCE\n"
        "S-0001\tP-0001\tPre-sequencing\t-20\tProcedure date\n",
        encoding="utf-8",
    )
    (study_dir / "meta_clinical_sample_wsi_timepoint.txt").write_text(
        "legacy", encoding="utf-8"
    )
    (study_dir / "data_clinical_sample_wsi_timepoint.txt").write_text(
        "legacy", encoding="utf-8"
    )

    exit_code = main(["--study-dir", str(study_dir)])

    assert exit_code == 0
    contents = clinical_file.read_text(encoding="utf-8")
    assert "WSI_TIMEPOINT_BIN" not in contents
    assert "WSI_TIMEPOINT_DAYS" not in contents
    assert "WSI_TIMEPOINT_SOURCE" not in contents
    assert not (study_dir / "meta_clinical_sample_wsi_timepoint.txt").exists()
    assert not (study_dir / "data_clinical_sample_wsi_timepoint.txt").exists()
