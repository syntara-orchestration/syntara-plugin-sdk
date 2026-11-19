"""Tests for recording unexplained compatibility runner failures."""

from __future__ import annotations

import json
from pathlib import Path

from record_runner_failure import record_unexpected_runner_failure


def write_report(path: Path, *, result: str, report_only: bool = False) -> None:
    path.write_text(
        json.dumps({"result": result, "report_only": report_only}), encoding="utf-8"
    )


def test_marks_a_nonzero_exit_after_a_passing_report(tmp_path: Path) -> None:
    report_path = tmp_path / "http-request.json"
    write_report(report_path, result="pass")

    assert record_unexpected_runner_failure(report_path, "3")
    assert json.loads(report_path.read_text(encoding="utf-8"))["runner_failure"] is True


def test_does_not_mark_an_enforced_compatibility_failure(tmp_path: Path) -> None:
    report_path = tmp_path / "http-request.json"
    write_report(report_path, result="fail")

    assert not record_unexpected_runner_failure(report_path, "1")
    assert "runner_failure" not in json.loads(report_path.read_text(encoding="utf-8"))


def test_marks_a_report_only_failure_with_a_nonzero_exit(tmp_path: Path) -> None:
    report_path = tmp_path / "http-request.json"
    write_report(report_path, result="fail", report_only=True)

    assert record_unexpected_runner_failure(report_path, "3")
    assert json.loads(report_path.read_text(encoding="utf-8"))["runner_failure"] is True


def test_missing_or_invalid_report_is_left_for_the_gate_to_reject(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.json"
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text("{", encoding="utf-8")

    assert not record_unexpected_runner_failure(missing_path, "3")
    assert not record_unexpected_runner_failure(invalid_path, "3")
