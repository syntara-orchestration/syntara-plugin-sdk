"""Tests for the aggregate gate's exit policy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from report import main

PLUGINS = '["http-request", "script-bash"]'


def write(
    directory: Path,
    plugin: str,
    result: str,
    report_only: bool = False,
    confidence: str = "deterministic",
) -> None:
    directory.mkdir(exist_ok=True)
    mode = "report-only:dispatch" if report_only else "enforcing"
    document = {
        "plugin": plugin,
        "result": result,
        "report_only": report_only,
        "mode": mode,
        "pin": {},
    }
    if result == "fail":
        document["failure"] = {
            "stage": "validate",
            "index": 3,
            "category": "misconfiguration",
            "confidence": confidence,
        }
    (directory / f"{plugin}.json").write_text(json.dumps(document))


def run(tmp_path: Path, **overrides: str) -> int:
    options = {
        "changes": "success",
        "run-compat": "true",
        "discover": "success",
        "tooling": "success",
        "compat": "success",
        "plugins": PLUGINS,
    }
    options.update(overrides)
    argv = ["--reports-dir", str(tmp_path / "reports")]
    for key, value in options.items():
        argv += [f"--{key}", value]
    return main(argv)


@pytest.fixture(autouse=True)
def no_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def test_all_compatible_passes(tmp_path: Path) -> None:
    write(tmp_path / "reports", "http-request", "pass")
    write(tmp_path / "reports", "script-bash", "pass")
    assert run(tmp_path) == 0


def test_docs_only_change_passes_by_design(tmp_path: Path) -> None:
    assert run(tmp_path, **{"run-compat": "false", "discover": "skipped", "compat": "skipped"}) == 0


def test_failed_change_detection_is_not_a_skip(tmp_path: Path) -> None:
    assert run(tmp_path, changes="failure", **{"run-compat": ""}) == 1


def test_failed_discovery_fails_even_though_legs_skipped(tmp_path: Path) -> None:
    assert run(tmp_path, discover="failure", compat="skipped", plugins="") == 1


def test_failed_tooling_tests_fail(tmp_path: Path) -> None:
    write(tmp_path / "reports", "http-request", "pass")
    write(tmp_path / "reports", "script-bash", "pass")
    assert run(tmp_path, tooling="failure") == 1


def test_missing_report_fails(tmp_path: Path) -> None:
    write(tmp_path / "reports", "http-request", "pass")
    assert run(tmp_path) == 1


def test_empty_plugin_list_after_successful_discovery_fails(tmp_path: Path) -> None:
    assert run(tmp_path, plugins="[]") == 1


def test_incompatible_leg_fails(tmp_path: Path) -> None:
    write(tmp_path / "reports", "http-request", "fail")
    write(tmp_path / "reports", "script-bash", "pass")
    assert run(tmp_path, compat="failure") == 1


def test_report_only_failure_passes(tmp_path: Path) -> None:
    write(tmp_path / "reports", "http-request", "fail", report_only=True)
    write(tmp_path / "reports", "script-bash", "pass")
    assert run(tmp_path) == 0


def test_label_read_at_report_time_downgrades_failed_legs(tmp_path: Path) -> None:
    # "Re-run failed jobs" keeps the legs enforcing; the gate re-reads the label.
    write(tmp_path / "reports", "http-request", "fail")
    write(tmp_path / "reports", "script-bash", "pass")
    assert run(tmp_path, compat="failure", **{"label-report-only": "true"}) == 0


def test_label_does_not_excuse_a_missing_report(tmp_path: Path) -> None:
    write(tmp_path / "reports", "http-request", "fail")
    assert run(tmp_path, compat="failure", **{"label-report-only": "true"}) == 1


def test_failed_leg_with_only_passing_reports_fails(tmp_path: Path) -> None:
    write(tmp_path / "reports", "http-request", "pass")
    write(tmp_path / "reports", "script-bash", "pass")
    assert run(tmp_path, compat="failure", **{"label-report-only": "true"}) == 1


def test_report_only_summary_does_not_claim_compatibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    write(tmp_path / "reports", "http-request", "fail", report_only=True)
    write(tmp_path / "reports", "script-bash", "pass")
    assert run(tmp_path) == 0
    text = summary.read_text()
    assert "every step type is compatible" not in text
    assert "report-only, not failing" in text
    assert "WARNING: step-types compat (report-only)" in capsys.readouterr().out


def test_label_mode_recorded_by_legs_is_not_trusted(tmp_path: Path) -> None:
    # The legs ran while the label was set; it was removed before the gate ran.
    reports = tmp_path / "reports"
    reports.mkdir()
    stale = {
        "plugin": "http-request",
        "result": "fail",
        "report_only": True,
        "mode": "report-only:label",
        "pin": {},
        "failure": {"stage": "validate", "index": 3, "category": "misconfiguration"},
    }
    (reports / "http-request.json").write_text(json.dumps(stale))
    write(reports, "script-bash", "pass")
    assert run(tmp_path, **{"label-report-only": "false"}) == 1


def test_inconsistent_leg_and_report_fails(tmp_path: Path) -> None:
    write(tmp_path / "reports", "http-request", "fail", report_only=False)
    write(tmp_path / "reports", "script-bash", "pass")
    assert run(tmp_path, compat="success") == 1


def test_summary_is_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    write(tmp_path / "reports", "http-request", "pass")
    write(tmp_path / "reports", "script-bash", "pass")
    run(tmp_path)
    text = summary.read_text()
    assert "## Step types compatibility" in text
    assert "| http-request | compatible |" in text


def test_summary_reports_the_analysis_mix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    reports = tmp_path / "reports"
    write(reports, "http-request", "fail", confidence="deterministic")
    write(reports, "script-bash", "fail", confidence="unclassified")
    write(reports, "script-python", "pass")
    assert run(tmp_path, compat="failure") == 1
    output = capsys.readouterr().out
    assert "failure analysis: 1 deterministic, 1 unclassified" in output
    assert "failures without a rule" in output
    assert "failure analysis: 1 deterministic, 1 unclassified" in summary.read_text()


def test_no_analysis_mix_when_everything_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    write(tmp_path / "reports", "http-request", "pass")
    write(tmp_path / "reports", "script-bash", "pass")
    assert run(tmp_path) == 0
    assert "failure analysis" not in capsys.readouterr().out
