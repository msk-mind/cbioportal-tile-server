from pathlib import Path

from tools.generate_wsi_clinical_attrs import main


def test_cleanup_removes_legacy_wsi_sample_attributes_and_sidecar_files(
    tmp_path: Path,
):
    study_dir = tmp_path / "study"
    study_dir.mkdir()
    clinical_file = study_dir / "data_clinical_sample.txt"
    clinical_file.write_text(
        "#Sample Identifier\tPatient Identifier\tHas WSI Slide\tWSI Slide Count\tOther\n"
        "#Sample identifier\tPatient identifier\tLegacy\tLegacy\tOther\n"
        "#STRING\tSTRING\tSTRING\tNUMBER\tSTRING\n"
        "#1\t1\t1\t1\t1\n"
        "SAMPLE_ID\tPATIENT_ID\tHAS_WSI_SLIDE\tWSI_SLIDE_COUNT\tOTHER\n"
        "S-0001\tP-0001\tYes\t4\tkeep\n",
        encoding="utf-8",
    )
    (study_dir / "meta_clinical_sample_wsi.txt").write_text("legacy", encoding="utf-8")
    (study_dir / "data_clinical_sample_wsi.txt").write_text("legacy", encoding="utf-8")

    exit_code = main(["--study-dir", str(study_dir)])

    assert exit_code == 0
    contents = clinical_file.read_text(encoding="utf-8")
    assert "HAS_WSI_SLIDE" not in contents
    assert "WSI_SLIDE_COUNT" not in contents
    assert "OTHER" in contents
    assert not (study_dir / "meta_clinical_sample_wsi.txt").exists()
    assert not (study_dir / "data_clinical_sample_wsi.txt").exists()
