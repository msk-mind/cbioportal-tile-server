"""Invalidate cached /patient payloads for one or more patient IDs."""

import argparse
import asyncio
from pathlib import Path

from app import cache
from tools.patient_cache_utils import read_patient_ids_from_clinical_sample


async def _run(patient_ids: list[str]) -> int:
    await cache.init_cache()
    try:
        deleted = 0
        for patient_id in patient_ids:
            if await cache.delete_patient(patient_id):
                deleted += 1
        return deleted
    finally:
        await cache.close_cache()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete cached tile-server patient payloads from Redis."
    )
    parser.add_argument("patient_ids", nargs="*", help="Patient IDs to evict")
    parser.add_argument(
        "--study-dir",
        help="Evict every patient listed in data_clinical_sample.txt for this study.",
    )
    args = parser.parse_args()

    patient_ids = list(args.patient_ids)
    if args.study_dir:
        patient_ids.extend(
            read_patient_ids_from_clinical_sample(
                Path(args.study_dir).expanduser().resolve()
            )
        )

    patient_ids = list(dict.fromkeys(patient_ids))
    if not patient_ids:
        parser.error("provide at least one patient ID or --study-dir")

    deleted = asyncio.run(_run(patient_ids))
    print(f"Deleted {deleted} cached patient payload(s) out of {len(patient_ids)} requested")


if __name__ == "__main__":
    main()
