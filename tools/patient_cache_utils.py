from __future__ import annotations

import asyncio
import csv
from pathlib import Path

from app import cache


def read_patient_ids_from_clinical_sample(study_dir: Path) -> list[str]:
    clinical_file = study_dir / "data_clinical_sample.txt"
    if not clinical_file.exists():
        raise FileNotFoundError(f"data_clinical_sample.txt not found in {study_dir}")

    patient_ids: list[str] = []
    seen: set[str] = set()
    with clinical_file.open(newline="") as fh:
        reader = csv.DictReader(
            (line for line in fh if not line.startswith("#")),
            delimiter="\t",
        )
        for row in reader:
            patient_id = row.get("PATIENT_ID", "").strip()
            if patient_id and patient_id not in seen:
                patient_ids.append(patient_id)
                seen.add(patient_id)

    return patient_ids


async def _invalidate_patient_cache(patient_ids: list[str]) -> int:
    await cache.init_cache()
    try:
        deleted = 0
        for patient_id in patient_ids:
            if await cache.delete_patient(patient_id):
                deleted += 1
        return deleted
    finally:
        await cache.close_cache()


def invalidate_study_patient_cache(study_dir: Path) -> tuple[int, int]:
    patient_ids = read_patient_ids_from_clinical_sample(study_dir)
    deleted = asyncio.run(_invalidate_patient_cache(patient_ids))
    return deleted, len(patient_ids)
