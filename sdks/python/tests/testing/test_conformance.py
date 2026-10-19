"""Tests for the composable conformance checks."""

from __future__ import annotations

from syntara_plugin.runtime import StepBindings, StepDescriptor
from syntara_plugin.testing import (
    ConformanceReport,
    check_invalid_blocks_hook,
    check_paths_equivalent,
    check_step_conformance,
    check_valid_reaches_hook_once,
)
from syntara_plugin.testing.payloads import ReferenceTaskStep


def _binding() -> StepBindings:
    step = ReferenceTaskStep()
    descriptor = StepDescriptor.for_step("testing.conformance.reference", step)
    bindings = StepBindings()
    bindings.bind(descriptor, step)
    return bindings


def test_conformance_passes_for_the_reference_step() -> None:
    report = check_step_conformance(
        _binding().binding("testing.conformance.reference"),
        valid_cases=[{"title": "deploy"}, {"title": "build", "priority": "high"}],
        invalid_cases=[{}, {"title": "t", "priority": "urgent"}],
    )
    assert report.passed, report.failures


def test_invalid_blocks_hook_reports_the_failing_case() -> None:
    report = check_invalid_blocks_hook(
        _binding().binding("testing.conformance.reference"),
        invalid_cases=[{"title": "actually-valid"}],
    )
    assert not report.passed
    failure = report.failures[0]
    assert failure.check == "invalid_blocks_hook"
    assert "actually-valid" in failure.case or failure.case.startswith("case-")


def test_valid_reaches_hook_once_reports_wrong_run_counts() -> None:
    report = check_valid_reaches_hook_once(
        _binding().binding("testing.conformance.reference"),
        valid_cases=[{"priority": "urgent"}],
    )
    assert not report.passed
    assert report.failures[0].check == "valid_reaches_hook_once"


def test_paths_equivalent_passes_for_valid_cases() -> None:
    report = check_paths_equivalent(
        _binding().binding("testing.conformance.reference"),
        cases=[{"title": "deploy"}],
    )
    assert report.passed, report.failures


def test_conformance_can_skip_container_equivalence() -> None:
    report = check_step_conformance(
        _binding().binding("testing.conformance.reference"),
        valid_cases=[{"title": "deploy"}],
        invalid_cases=[{}],
        include_equivalence=False,
    )
    assert report.passed, report.failures


def test_empty_conformance_run_passes_without_cases() -> None:
    assert ConformanceReport().passed
    report = check_step_conformance(_binding().binding("testing.conformance.reference"))
    assert report.passed
