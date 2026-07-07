from pathlib import Path

from tools.generate_wsi_clinical_attrs import _read_sample_ids


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
