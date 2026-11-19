"""Tests for the compatibility workflow and the CI gate."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
COMPAT = WORKFLOWS / "step-types-compat.yml"
RESOLVER = r"/^  STEP_TYPES_REF: /{s/.*'\([^']*\)'.*/\1/p;q;}"


def test_the_base_pin_resolver_reads_the_declaration_not_itself() -> None:
    text = COMPAT.read_text(encoding="utf-8")
    assert 'sed -n "' + RESOLVER + '"' in text
    extracted = subprocess.run(
        ["sed", "-n", RESOLVER, str(COMPAT)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    declared = re.search(r"^  STEP_TYPES_REF: .*'([^'\n]+)'", text, re.MULTILINE)
    assert declared
    assert extracted == declared.group(1)


def test_ci_calls_grouped_compatibility_workflow_with_a_final_gate() -> None:
    ci = yaml.safe_load(CI.read_text(encoding="utf-8"))
    call = ci["jobs"]["step-types-compat"]
    assert call["uses"] == "$/.github/workflows/step-types-compat.yml"
    assert call["with"]["step_types_ref"] == "${{ inputs.step_types_ref || '' }}"
    assert call["with"]["report_only"] == "${{ inputs.report_only || false }}"
    workflow = yaml.safe_load(COMPAT.read_text(encoding="utf-8"))
    job = workflow["jobs"]["step-types-compat"]
    assert "matrix" not in job.get("strategy", {})
    assert job["steps"][-1]["name"] == "compat-report"
    compat = next(step for step in job["steps"] if step.get("id") == "compat")
    assert compat["env"]["PLUGINS"] == "${{ steps.discover.outputs.plugins }}"
    assert '--plugins "$PLUGINS"' in compat["run"]
    assert job["steps"][-1]["env"]["COMPAT"] == "${{ steps.compat.outcome }}"
    assert not any(step.get("id") == "compat-other" for step in job["steps"])


def test_new_pr_events_cancel_the_entire_older_run() -> None:
    ci = yaml.safe_load(CI.read_text(encoding="utf-8"))
    concurrency = ci["concurrency"]
    assert "github.event.pull_request.number" in concurrency["group"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert "needs" not in ci["jobs"]["required-checks"]


def test_candidate_runs_despite_baseline_or_tooling_failure() -> None:
    workflow = yaml.safe_load(COMPAT.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["step-types-compat"]["steps"]
    by_id = {step["id"]: step for step in steps if "id" in step}
    for name in (
        "tooling",
        "baseline-check",
        "base-checkout",
        "base-pin",
        "base-step-types",
        "baseline-recompute",
    ):
        assert by_id[name]["continue-on-error"] is True
    assert "!cancelled()" in by_id["compat"]["if"]
    assert "steps.discover.outcome == 'success'" in by_id["compat"]["if"]
    assert "steps.candidate-wheels.outcome == 'success'" in by_id["compat"]["if"]


@pytest.mark.parametrize(
    ("candidate", "tooling", "label", "expected"),
    [
        ("pass", "success", "false", 0),
        ("fail", "success", "false", 1),
        ("fail", "success", "true", 0),
        ("pass", "failure", "true", 1),
    ],
)
def test_report_handles_failed_recompute_and_partial_baseline(
    tmp_path: Path, candidate: str, tooling: str, label: str, expected: int
) -> None:
    workflow = yaml.safe_load(COMPAT.read_text(encoding="utf-8"))
    final = workflow["jobs"]["step-types-compat"]["steps"][-1]
    (tmp_path / "sdk").symlink_to(WORKFLOWS.parents[1], target_is_directory=True)
    reports = tmp_path / "reports"
    reports.mkdir()
    failure = {
        "stage": "validate",
        "index": 3,
        "category": "misconfiguration",
        "confidence": "deterministic",
        "rule_id": "MC001",
        "facts": {"label": "build", "keys": ["workload"]},
    }
    document = {
        "schema": 1,
        "plugin": "http-request",
        "result": candidate,
        "mode": "enforcing",
        "pin": {"sha": "a" * 40},
        "failure": failure if candidate == "fail" else None,
        "failures": [failure] if candidate == "fail" else [],
    }
    (reports / "http-request.json").write_text(json.dumps(document))
    # A failed recomputation may leave partial reports: they cannot excuse the candidate.
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "http-request.json").write_text(json.dumps(document))
    env = os.environ | {
        "PLUGINS": '["http-request"]',
        "CHANGES": "success",
        "RUN_COMPAT": "true",
        "DISCOVER": "success",
        "TOOLING": tooling,
        "COMPAT": "success",
        "UPLOAD": "success",
        "LABEL_OUTCOME": "success",
        "LABEL_PRESENT": label,
        "BASE_SHA": "b" * 40,
        "BASE_RUN_ID": "",
        "BASE_AVAILABLE": "false",
        "BASE_RECOMPUTE": "failure",
        "EVENT_NAME": "pull_request",
    }
    env.pop("GITHUB_STEP_SUMMARY", None)
    process = subprocess.run(
        ["bash", "-e", "-c", final["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == expected, process.stdout + process.stderr
    assert "preparing the devel baseline failed" in process.stdout


def test_grouped_tooling_sync_never_builds_dependencies() -> None:
    workflow = yaml.safe_load(COMPAT.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["step-types-compat"]["steps"]
    tooling = next(step for step in steps if step.get("id") == "tooling")
    assert "uv sync --locked --all-groups --no-build --no-install-workspace" in tooling["run"]
    assert "uv pip install --no-deps --no-build" in tooling["run"]


def test_required_checks_gate_starts_immediately_and_covers_every_job() -> None:
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    gate = jobs["required-checks"]
    assert "needs" not in gate
    assert gate["permissions"]["actions"] == "read"
    required = set(json.loads(gate["env"]["REQUIRED_JOBS"]))
    expected = {
        jobs[name]["name"]
        for name in jobs
        if name not in {"test", "step-types-compat", "required-checks"}
    }
    expected.update(
        f"Test (Python {version})"
        for version in jobs["test"]["strategy"]["matrix"]["python-version"]
    )
    called = yaml.safe_load(COMPAT.read_text(encoding="utf-8"))
    expected.add(
        f"{jobs['step-types-compat']['name']} / {called['jobs']['step-types-compat']['name']}"
    )
    assert required == expected
