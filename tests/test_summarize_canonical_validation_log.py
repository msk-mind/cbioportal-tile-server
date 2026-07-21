from pathlib import Path

from tools.summarize_canonical_validation_log import summarize_log


def test_summarize_log_reports_progress_and_mismatches(tmp_path: Path):
    log_path = tmp_path / "validation.log"
    log_path.write_text(
        "P-0002438: matches=True canonical=16 legacy=16\n"
        "Progress: 1/2780\n"
        "P-0048660: matches=False canonical=110 legacy=111\n"
        "Progress: 2/2780\n"
        "Resume mode: skipped=2 remaining=2778 total=2780\n",
        encoding="utf-8",
    )

    summary = summarize_log(log_path)

    assert summary["completed"] == 2
    assert summary["mismatches"] == 1
    assert summary["last_progress"] == 2
    assert summary["last_progress_total"] == 2780
    assert summary["skipped"] == 2
    assert summary["remaining"] == 2778
    assert summary["total"] == 2780
    assert summary["cumulative_completed"] == 4
    assert summary["last_patient"] == "P-0048660"


def test_summarize_log_supports_legacy_resume_line(tmp_path: Path):
    log_path = tmp_path / "validation.log"
    log_path.write_text(
        "P-0002438: matches=True canonical=16 legacy=16\n"
        "Progress: 206/2780\n"
        "Resume mode: skipped=206 remaining=2574\n"
        "P-0048660: matches=True canonical=111 legacy=111\n"
        "Progress: 1/2574\n",
        encoding="utf-8",
    )

    summary = summarize_log(log_path)

    assert summary["skipped"] == 206
    assert summary["remaining"] == 2574
    assert summary["total"] == 2780
    assert summary["cumulative_completed"] == 207
