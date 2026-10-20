"""Tests for the composable conformance checks."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import (
    ExecutionContext,
    StepBindings,
    StepDescriptor,
    TaskStep,
)
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


def test_conformance_without_context_compares_identical_contexts() -> None:
    """A step echoing invocation identifiers must not false-fail equivalence."""

    class EchoInput(BaseModel):
        value: str = "x"

    class EchoOutput(BaseModel):
        invocation_id: str
        workflow_run_id: str

    class EchoContextTask(TaskStep[EchoInput, EchoOutput]):
        def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
            return EchoOutput(
                invocation_id=context.invocation_id,
                workflow_run_id=context.workflow_run_id or "",
            )

    bindings = StepBindings()
    step = EchoContextTask()
    bindings.bind(StepDescriptor.for_step("testing.conformance.echo", step), step)
    report = check_step_conformance(
        bindings.binding("testing.conformance.echo"),
        valid_cases=[{"value": "x"}],
        invalid_cases=[{"value": 12345}],
    )
    assert report.passed, report.failures


def test_conformance_details_never_echo_secret_values() -> None:
    """Failure details carry safe metadata, never raw exception text."""

    from syntara_plugin.testing import secret_sentinel

    token = secret_sentinel()

    class LooseInput(BaseModel):
        model_config = {"extra": "allow"}
        title: str = "x"

    class NeverOutput(BaseModel):
        value: str = "unreachable"

    class BoomTask(TaskStep[LooseInput, NeverOutput]):
        def run(self, inputs: LooseInput, context: ExecutionContext) -> NeverOutput:
            raise RuntimeError(f"token={token}")

    bindings = StepBindings()
    step = BoomTask()
    bindings.bind(StepDescriptor.for_step("testing.conformance.boom", step), step)
    report = check_valid_reaches_hook_once(
        bindings.binding("testing.conformance.boom"), valid_cases=[{"title": "x"}]
    )
    assert not report.passed
    for failure in report.failures:
        assert token not in failure.detail
        assert "RuntimeError" in failure.detail
