from pathlib import Path
from unittest.mock import AsyncMock, patch

from tools.patient_cache_utils import (
    invalidate_study_patient_cache,
    read_patient_ids_from_clinical_sample,
)


def test_read_patient_ids_from_clinical_sample_deduplicates(tmp_path: Path):
    study_dir = tmp_path / "study"
    study_dir.mkdir()
    (study_dir / "data_clinical_sample.txt").write_text(
        "#Sample Identifier\tPatient Identifier\n"
        "#Sample identifier\tPatient identifier\n"
        "#STRING\tSTRING\n"
        "#1\t1\n"
        "SAMPLE_ID\tPATIENT_ID\n"
        "S-0001\tP-0001\n"
        "S-0002\tP-0001\n"
        "S-0003\tP-0002\n",
        encoding="utf-8",
    )

    assert read_patient_ids_from_clinical_sample(study_dir) == [
        "P-0001",
        "P-0002",
    ]


def test_invalidate_study_patient_cache_returns_deleted_and_requested_counts(
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

    with (
        patch("tools.patient_cache_utils.cache.init_cache", new=AsyncMock()),
        patch("tools.patient_cache_utils.cache.close_cache", new=AsyncMock()),
        patch(
            "tools.patient_cache_utils.cache.delete_patient",
            new=AsyncMock(side_effect=[True, False]),
        ) as delete_patient,
    ):
        deleted, requested = invalidate_study_patient_cache(study_dir)

    assert (deleted, requested) == (1, 2)
    assert [call.args[0] for call in delete_patient.await_args_list] == [
        "P-0001",
        "P-0002",
    ]
