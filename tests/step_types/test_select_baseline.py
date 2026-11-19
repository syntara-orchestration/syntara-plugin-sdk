"""A baseline is the devel push, never a manually overridden run."""

from __future__ import annotations

import pytest
import select_baseline

BASE = "a" * 40


def run(**overrides: object) -> dict[str, object]:
    return {
        "id": 42, "name": "CI", "event": "push", "head_branch": "devel",
        "head_sha": BASE, "status": "completed", "conclusion": "failure", **overrides,
    }


@pytest.mark.parametrize("override", [
    {"event": "workflow_dispatch"}, {"head_branch": "feature"},
    {"head_sha": "b" * 40}, {"status": "in_progress"},
    {"conclusion": "cancelled"}, {"name": "unrelated"},
])
def test_untrusted_or_incomplete_runs_are_not_baselines(override: dict[str, object]) -> None:
    assert select_baseline.select_run({"workflow_runs": [run(**override)]}, BASE) is None


def test_selects_legacy_push_before_ci_during_migration() -> None:
    manual = run(id=1, event="workflow_dispatch")
    ci = run(id=2)
    legacy = run(id=3, name="Step types compatibility")
    assert select_baseline.select_run({"workflow_runs": [manual, ci, legacy]}, BASE) == 3


def test_missing_workflow_list_is_an_error() -> None:
    with pytest.raises(ValueError):
        select_baseline.select_run({}, BASE)
