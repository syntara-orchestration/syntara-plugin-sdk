"""Tests for the compatibility workflow and the CI gate."""

from __future__ import annotations

import json
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
    assert call["uses"] == "./.github/workflows/step-types-compat.yml"
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


def test_grouped_tooling_sync_never_builds_dependencies() -> None:
    workflow = yaml.safe_load(COMPAT.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["step-types-compat"]["steps"]
    tooling = next(step for step in steps if step.get("id") == "tooling")
    assert "uv sync --locked --all-groups --no-build --no-install-workspace" in tooling["run"]
    assert "uv pip install --no-deps --no-build" in tooling["run"]


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped"])
def test_required_checks_gate_covers_every_job_and_only_passes_success(result: str) -> None:
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    gate = jobs["required-checks"]
    assert set(gate["needs"]) == set(jobs) - {"required-checks"}
    assert gate["if"] == "always()"
    step = gate["steps"][0]
    assert step["env"]["NEEDS"] == "${{ toJSON(needs) }}"
    results = {name: {"result": "success"} for name in gate["needs"]}
    results["step-types-compat"]["result"] = result
    process = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env={"NEEDS": json.dumps(results)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (process.returncode == 0) == (result == "success"), process.stdout + process.stderr
