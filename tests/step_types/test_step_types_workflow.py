"""Tests for the workflow's own contracts (currently: the base-pin resolver)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

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
    assert any(step.get("id") == "compat" for step in job["steps"])
    assert not WORKFLOW.with_name("step-types-compat.yml").exists()
