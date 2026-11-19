"""Tests for the workflow's own contracts (currently: the base-pin resolver)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
)

# The resolver the baseline-recompute job runs on the base checkout. The
# workflow file also contains this very command line (it mentions
# STEP_TYPES_REF too), so the pattern is anchored on the two-space env
# declaration and quits at the first match. Kept in sync with the workflow.
RESOLVER = r"/^  STEP_TYPES_REF: /{s/.*'\([^']*\)'.*/\1/p;q;}"


def test_the_base_pin_resolver_reads_the_declaration_not_itself() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'sed -n "' + RESOLVER + '"' in text, (
        "the base-pin resolver in the workflow changed; update this test to match it"
    )
    extracted = subprocess.run(
        ["sed", "-n", RESOLVER, str(WORKFLOW)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # Whatever the declaration pins (a SHA today; the workflow also accepts
    # branch and tag names), the resolver must extract exactly that. The
    # group stays on the declaration line, like the sed it mirrors.
    declared = re.search(r"^  STEP_TYPES_REF: .*'([^'\n]+)'", text, re.MULTILINE)
    assert declared, "the STEP_TYPES_REF declaration was not found"
    assert extracted == declared.group(1), (
        "the resolver must extract the environment declaration's pin, not match "
        "its own command line elsewhere in the file"
    )


def test_ci_has_one_grouped_compatibility_job_with_a_final_gate() -> None:
    import yaml

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["step-types-compat"]
    assert "matrix" not in job.get("strategy", {})
    assert job["steps"][-1]["name"] == "compat-report"
    names = {step.get("name") for step in job["steps"]}
    assert {"HTTP Request", "Script Bash", "Script Python", "Additional step types"} <= names
    gate_env = job["steps"][-1]["env"]
    for variable, step_id in (("HTTP_OUTCOME", "compat-http"), ("BASH_OUTCOME", "compat-bash"),
                              ("PYTHON_OUTCOME", "compat-python"), ("OTHER_OUTCOME", "compat-other")):
        assert gate_env[variable] == f"${{{{ steps.{step_id}.outcome }}}}"
    assert not WORKFLOW.with_name("step-types-compat.yml").exists()


@pytest.mark.parametrize(("name", "plugin"), [
    ("HTTP Request", "http-request"), ("Script Bash", "script-bash"),
    ("Script Python", "script-python"),
])
def test_named_steps_run_only_the_matching_discovered_type(name: str, plugin: str) -> None:
    import yaml

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["step-types-compat"]["steps"]
    step = next(item for item in steps if item.get("name") == name)
    assert step["continue-on-error"] is True
    assert f"'{plugin}'" in step["if"]
    assert f"--plugins '[\"{plugin}\"]'" in step["run"]


def test_newly_discovered_types_are_scheduled(tmp_path: Path) -> None:
    import yaml

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["step-types-compat"]["steps"]
    planner = next(step for step in steps if step.get("id") == "compat-plan")
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-c", planner["run"]],
        env={**os.environ, "PLUGINS": '["http-request","script-bash","new-type"]',
             "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text().strip() == 'other=["new-type"]'
    additional = next(step for step in steps if step.get("id") == "compat-other")
    assert additional["continue-on-error"] is True
    assert "steps.compat-plan.outputs.other != '[]'" in additional["if"]
    assert additional["env"]["PLUGINS"] == "${{ steps.compat-plan.outputs.other }}"
    assert '--plugins "$PLUGINS"' in additional["run"]


def test_grouped_tooling_sync_never_builds_dependencies() -> None:
    import yaml

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["step-types-compat"]["steps"]
    tooling = next(step for step in steps if step.get("id") == "tooling")
    assert "uv sync --locked --all-groups --no-build --no-install-workspace" in tooling["run"]
    assert "uv pip install --no-deps --no-build" in tooling["run"]


@pytest.mark.parametrize("failed", ["HTTP_OUTCOME", "BASH_OUTCOME", "PYTHON_OUTCOME", "OTHER_OUTCOME"])
def test_final_gate_rejects_unexplained_step_failure(tmp_path: Path, failed: str) -> None:
    import yaml

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    gate = workflow["jobs"]["step-types-compat"]["steps"][-1]
    (tmp_path / "sdk").symlink_to(WORKFLOW.parents[2], target_is_directory=True)
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "http-request.json").write_text(json.dumps({"schema": 1, "result": "pass"}))
    environment = {
        **os.environ,
        "PLUGINS": '["http-request"]', "CHANGES": "success", "RUN_COMPAT": "true",
        "DISCOVER": "success", "TOOLING": "success", "UPLOAD": "success",
        "LABEL_OUTCOME": "success", "LABEL_PRESENT": "false", "EVENT_NAME": "push",
        "BASE_SHA": "", "BASE_RUN_ID": "", "BASE_AVAILABLE": "", "BASE_RECOMPUTE": "",
        "HTTP_OUTCOME": "success", "BASH_OUTCOME": "skipped",
        "PYTHON_OUTCOME": "skipped", "OTHER_OUTCOME": "skipped",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }
    environment[failed] = "failure"
    result = subprocess.run(["bash", "-c", gate["run"]], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "grouped step-type run did not complete successfully" in result.stdout
