"""Tests for the composable conformance checks."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import (
    ControlStep,
    ExecutionContext,
    StepBindings,
)
from syntara_plugin.testing import (
    ConformanceReport,
    check_invalid_blocks_hook,
    check_paths_equivalent,
    check_step_conformance,
    check_valid_reaches_hook_once,
)
from syntara_plugin.testing.bindings_factory import installed_binding
from syntara_plugin.testing.payloads import ReferenceControlStep


def _binding() -> StepBindings:
    binding = installed_binding(
        ReferenceControlStep(), identity="testing.conformance.reference"
    )
    bindings = StepBindings()
    bindings.bind(binding.descriptor, binding.step)
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


class _EchoContextInput(BaseModel):
    value: str = "x"


class _EchoContextOutput(BaseModel):
    invocation_id: str
    workflow_run_id: str


class _EchoContextStep(ControlStep[_EchoContextInput, _EchoContextOutput]):
    """Echoes both context identifiers for equivalence checks."""

    def run(self, inputs: _EchoContextInput, context: ExecutionContext) -> _EchoContextOutput:
        return _EchoContextOutput(
            invocation_id=context.invocation_id,
            workflow_run_id=context.workflow_run_id or "",
        )


def test_conformance_without_context_compares_identical_contexts() -> None:
    """A step echoing invocation identifiers must not false-fail equivalence."""

    bindings = StepBindings()
    binding = installed_binding(_EchoContextStep(), identity="testing.conformance.echo")
    bindings.bind(binding.descriptor, binding.step)
    report = check_step_conformance(
        bindings.binding("testing.conformance.echo"),
        valid_cases=[{"value": "x"}],
        invalid_cases=[{"value": 12345}],
    )
    assert report.passed, report.failures


class _BoomInput(BaseModel):
    model_config = {"extra": "allow"}
    title: str = "x"


class _BoomOutput(BaseModel):
    value: str = "unreachable"


class _BoomStep(ControlStep[_BoomInput, _BoomOutput]):
    """Fails with the token in its raw exception message."""

    token: str = ""

    def run(self, inputs: _BoomInput, context: ExecutionContext) -> _BoomOutput:
        raise RuntimeError(f"token={self.token}")


def test_conformance_details_never_echo_secret_values() -> None:
    """Failure details carry safe metadata, never raw exception text."""

    from syntara_plugin.testing import secret_sentinel

    token = secret_sentinel()
    bindings = StepBindings()
    step = _BoomStep()
    step.token = token
    binding = installed_binding(step, identity="testing.conformance.boom")
    bindings.bind(binding.descriptor, binding.step)
    report = check_valid_reaches_hook_once(
        bindings.binding("testing.conformance.boom"), valid_cases=[{"title": "x"}]
    )
    assert not report.passed
    for failure in report.failures:
        assert token not in failure.detail
        assert "RuntimeError" in failure.detail
