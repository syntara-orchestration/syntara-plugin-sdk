"""Tests for the container-adapter invocation path and path equivalence."""

from __future__ import annotations

import time
from typing import Any

import pytest
from pydantic import BaseModel
from syntara_plugin.runtime import ControlStep, ExecutionContext
from syntara_plugin.testing import (
    INVALID_INPUT_CASES,
    assert_outcomes_equivalent,
    build_execution_context,
    invoke_step,
    invoke_step_in_container,
)
from syntara_plugin.testing.payloads import (
    FailingReferenceStep,
    ReferenceControlStep,
    schema_violating_binding,
)


class CrashingStep(ReferenceControlStep):
    def run(self, inputs: Any, context: Any) -> Any:
        raise RuntimeError("the business logic crashed")


class _EchoInput(BaseModel):
    value: str = "x"


class _InvocationEchoOutput(BaseModel):
    invocation_id: str


class _InvocationEchoStep(ControlStep[_EchoInput, _InvocationEchoOutput]):
    """Echoes the invocation id."""

    def run(self, inputs: _EchoInput, context: ExecutionContext) -> _InvocationEchoOutput:
        return _InvocationEchoOutput(invocation_id=context.invocation_id)


class _RunIdEchoOutput(BaseModel):
    invocation_id: str
    workflow_run_id: str


class _RunIdEchoStep(ControlStep[_EchoInput, _RunIdEchoOutput]):
    """Echoes both context identifiers."""

    def run(self, inputs: _EchoInput, context: ExecutionContext) -> _RunIdEchoOutput:
        return _RunIdEchoOutput(
            invocation_id=context.invocation_id,
            workflow_run_id=context.workflow_run_id or "",
        )


class _RunIdOutput(BaseModel):
    workflow_run_id: str


class _RunIdStep(ControlStep[_EchoInput, _RunIdOutput]):
    """Echoes the workflow-run id."""

    def run(self, inputs: _EchoInput, context: ExecutionContext) -> _RunIdOutput:
        return _RunIdOutput(workflow_run_id=context.workflow_run_id or "")


class _SleepyOutput(BaseModel):
    value: str = "done"


class _SleepyStep(ControlStep[_EchoInput, _SleepyOutput]):
    """Ignores cancellation and sleeps past the invocation timeout."""

    def run(self, inputs: _EchoInput, context: ExecutionContext) -> _SleepyOutput:
        time.sleep(30)
        return _SleepyOutput(value="done")


class _ExitingStep(ControlStep[_EchoInput, _SleepyOutput]):
    """Calls sys.exit from the business hook."""

    def run(self, inputs: _EchoInput, context: ExecutionContext) -> _SleepyOutput:
        raise SystemExit(3)


class _BigOutput(BaseModel):
    blob: str


class _BigResultStep(ControlStep[_EchoInput, _BigOutput]):
    """Returns a business result above the container inline bound."""

    def run(self, inputs: _EchoInput, context: ExecutionContext) -> _BigOutput:
        return _BigOutput(blob="x" * 1_200_000)


def test_success_over_the_container_abi() -> None:
    outcome = invoke_step_in_container(ReferenceControlStep(), {"title": "deploy"})
    assert outcome.ok
    assert outcome.status == 0
    assert outcome.run_calls == 1
    assert outcome.output == {"summary": "processed deploy", "priority": "low"}
    assert outcome.transport == "container"
    assert outcome.raw is not None


def test_invalid_input_fails_before_the_hook_over_the_container_abi() -> None:
    outcome = invoke_step_in_container(ReferenceControlStep(), {"priority": "high"})
    assert not outcome.ok
    assert outcome.status == 1
    assert outcome.run_calls == 0
    assert outcome.error is not None
    assert outcome.error.code == "INVALID_INPUT"


@pytest.mark.parametrize("case", INVALID_INPUT_CASES, ids=lambda case: case.name)
def test_invalid_cases_are_equivalent_across_paths(case: Any) -> None:
    context = build_execution_context(invocation_id=f"equiv-{case.name}")
    direct = invoke_step(ReferenceControlStep(), case.inputs, context=context)
    container = invoke_step_in_container(ReferenceControlStep(), case.inputs, context=context)
    assert_outcomes_equivalent(direct, container)


def test_valid_outcomes_are_equivalent_across_paths() -> None:
    context = build_execution_context(invocation_id="equiv-valid")
    inputs = {"title": "deploy", "priority": "high", "count": 2}
    direct = invoke_step(ReferenceControlStep(), inputs, context=context)
    container = invoke_step_in_container(ReferenceControlStep(), inputs, context=context)
    assert_outcomes_equivalent(direct, container)


def test_deliberate_failures_are_equivalent_with_matching_codes() -> None:
    context = build_execution_context(invocation_id="equiv-failure")
    direct = invoke_step(FailingReferenceStep(), {"title": "x"}, context=context)
    container = invoke_step_in_container(FailingReferenceStep(), {"title": "x"}, context=context)
    assert_outcomes_equivalent(direct, container)
    assert direct.raised is not None
    assert container.raised is not None
    assert direct.raised.code == container.raised.code  # type: ignore[attr-defined]


def test_schema_invalid_outputs_are_equivalent_as_raised_failures() -> None:
    binding = schema_violating_binding()
    context = build_execution_context(invocation_id="equiv-bad-output")
    direct = invoke_step(binding, {"title": "a-title-longer-than-three"}, context=context)
    container = invoke_step_in_container(
        binding, {"title": "a-title-longer-than-three"}, context=context
    )
    assert_outcomes_equivalent(direct, container)
    assert direct.raised is not None
    assert container.raised is not None


def test_business_crashes_are_equivalent_as_raised_failures() -> None:
    context = build_execution_context(invocation_id="equiv-crash")
    direct = invoke_step(CrashingStep(), {"title": "x"}, context=context)
    container = invoke_step_in_container(CrashingStep(), {"title": "x"}, context=context)
    assert_outcomes_equivalent(direct, container)


def test_run_id_echoing_step_is_equivalent_under_a_shared_context() -> None:
    """Both paths must present the same run id when the context carries none."""

    context = build_execution_context(invocation_id="equiv-run-id")
    direct = invoke_step(_RunIdEchoStep(), {}, context=context)
    container = invoke_step_in_container(_RunIdEchoStep(), {}, context=context)
    assert_outcomes_equivalent(direct, container)
    assert direct.output == container.output
    assert direct.output["workflow_run_id"] == "equiv-run-id-run"


def test_a_deadlocked_step_returns_its_timeout_without_hanging_the_session() -> None:
    """A step that ignores cancellation cannot block beyond its timeout."""

    started = time.monotonic()
    outcome = invoke_step_in_container(
        _SleepyStep(), {}, context=build_execution_context(invocation_id="timeout-probe"),
        timeout_seconds=1,
    )
    elapsed = time.monotonic() - started
    assert not outcome.ok
    assert outcome.raised is not None
    assert outcome.raised.code.value == "TIMEOUT"
    assert elapsed < 10, f"cleanup was not bounded: {elapsed:.1f}s"


def test_system_exit_on_the_container_path_fails_promptly() -> None:
    """A BaseException from run() must not stall until the RPC deadline."""

    started = time.monotonic()
    outcome = invoke_step_in_container(
        _ExitingStep(),
        {},
        context=build_execution_context(invocation_id="sysexit-probe"),
        timeout_seconds=30,
    )
    elapsed = time.monotonic() - started
    assert not outcome.ok
    assert outcome.raised is not None
    assert outcome.raised.code.value == "OUTCOME_UNKNOWN"
    assert elapsed < 10, f"SystemExit was not converted promptly: {elapsed:.1f}s"


def test_boundary_length_invocation_ids_work_on_both_paths() -> None:
    """A 252-character id plus the derived run id fits the container ABI."""

    invocation_id = "x" * 252
    context = build_execution_context(invocation_id=invocation_id)
    direct = invoke_step(_InvocationEchoStep(), {}, context=context)
    container = invoke_step_in_container(_InvocationEchoStep(), {}, context=context)
    assert_outcomes_equivalent(direct, container)
    assert container.output == {"invocation_id": invocation_id}


def test_supplied_workflow_run_ids_work_on_both_paths() -> None:
    """A caller-supplied run id must be presented identically by both paths."""

    context = build_execution_context(
        invocation_id="run-id-probe", workflow_run_id="team.aap:run-42"
    )
    direct = invoke_step(_RunIdStep(), {}, context=context)
    container = invoke_step_in_container(_RunIdStep(), {}, context=context)
    assert_outcomes_equivalent(direct, container)
    assert container.output == {"workflow_run_id": "team.aap:run-42"}


def test_raw_contexts_with_supplied_run_ids_use_the_full_abi_maximum() -> None:
    """With a supplied run id nothing is derived, so 254 characters are valid.

    The 252-character cap exists only to leave room for the derived
    ``-run`` suffix; a raw context that already carries a run id may use the
    container ABI's full 256-character maximum for its invocation id.
    """

    from syntara_plugin.runtime import ExecutionContext

    context = ExecutionContext(invocation_id="x" * 254, workflow_run_id="run-1")
    direct = invoke_step(ReferenceControlStep(), {"title": "x"}, context=context)
    container = invoke_step_in_container(
        ReferenceControlStep(), {"title": "x"}, context=context
    )
    assert_outcomes_equivalent(direct, container)


def test_equivalence_compares_failure_categories() -> None:
    """Raised outcomes must agree on category, not merely both be raised."""

    from syntara_plugin.runtime import FailureCode
    from syntara_plugin.runtime.container import ContainerRemoteError
    from syntara_plugin.testing import StepOutcome

    def raised(transport: str, error: BaseException) -> StepOutcome:
        return StepOutcome(
            ok=False, status=None, status_message="", output=None, error=None,
            run_calls=1, raised=error, transport=transport, raw=None,  # type: ignore[arg-type]
        )

    crash = RuntimeError("the business logic crashed")
    timeout = ContainerRemoteError(
        FailureCode.TIMEOUT, retryable=True, outcome="unknown", source="transport"
    )
    unknown = ContainerRemoteError(
        FailureCode.OUTCOME_UNKNOWN, retryable=False, outcome="unknown", source="workload"
    )
    # A direct business crash maps to OUTCOME_UNKNOWN, so an unrelated TIMEOUT
    # on the container path is a genuine inequivalence.
    mismatched_direct = raised("direct", crash)
    mismatched_container = raised("container", timeout)
    with pytest.raises(AssertionError, match="failure category"):
        assert_outcomes_equivalent(mismatched_direct, mismatched_container)
    assert_outcomes_equivalent(raised("direct", crash), raised("container", unknown))


def test_equivalence_compares_status_messages() -> None:
    from syntara_plugin.testing import StepOutcome

    def success(transport: str, status_message: str) -> StepOutcome:
        return StepOutcome(
            ok=True, status=0, status_message=status_message, output={"value": "x"},
            error=None, run_calls=1, raised=None, transport=transport, raw=None,  # type: ignore[arg-type]
        )

    differing_direct = success("direct", "Completed")
    differing_container = success("container", "Something else")
    with pytest.raises(AssertionError, match="status_message"):
        assert_outcomes_equivalent(differing_direct, differing_container)
    assert_outcomes_equivalent(success("direct", "Completed"), success("container", "Completed"))


def test_oversized_results_document_the_inline_bound_asymmetry() -> None:
    """Above the container inline bound, equivalence intentionally does not hold.

    The harness wires no ResultObjectStore, so a business result larger than
    the container ABI's 1 MiB inline bound succeeds on the direct path and
    fails with ABI_VIOLATION on the container path; large outputs are a
    direct-path concern, as the invoke_step_in_container docstring and
    docs/testing.md state.
    """

    context = build_execution_context(invocation_id="inline-bound")
    direct = invoke_step(_BigResultStep(), {}, context=context)
    container = invoke_step_in_container(_BigResultStep(), {}, context=context)
    assert direct.ok
    assert not container.ok
    assert container.raised is not None
    assert container.raised.code.value == "ABI_VIOLATION"
    with pytest.raises(AssertionError, match="raised"):
        assert_outcomes_equivalent(direct, container)


def test_the_same_invocation_id_can_be_reused_sequentially() -> None:
    context = build_execution_context(invocation_id="reused-id")
    first = invoke_step_in_container(ReferenceControlStep(), {"title": "a"}, context=context)
    second = invoke_step_in_container(ReferenceControlStep(), {"title": "b"}, context=context)
    assert first.ok
    assert second.ok
    assert first.output != second.output


def test_equivalence_assertion_requires_direct_first() -> None:
    direct = invoke_step(ReferenceControlStep(), {"title": "x"})
    container = invoke_step_in_container(ReferenceControlStep(), {"title": "x"})
    with pytest.raises(AssertionError, match="direct outcome first"):
        assert_outcomes_equivalent(container, direct)


def test_equivalence_assertion_detects_different_outcomes() -> None:
    direct = invoke_step(ReferenceControlStep(), {"title": "x"})
    container = invoke_step_in_container(ReferenceControlStep(), {"priority": "urgent"})
    with pytest.raises(AssertionError, match="outcomes differ"):
        assert_outcomes_equivalent(direct, container)


def test_equivalence_failures_do_not_echo_secret_values() -> None:
    """Differing secret-bearing fields are named, never echoed."""

    from syntara_plugin.testing import StepOutcome, secret_sentinel

    token = secret_sentinel()
    direct = StepOutcome(
        ok=True, status=0, status_message="Completed", output={"token": token},
        error=None, run_calls=1, raised=None, transport="direct", raw=None,
    )
    container = StepOutcome(
        ok=True, status=0, status_message="Completed", output={"token": "different"},
        error=None, run_calls=1, raised=None, transport="container", raw=None,
    )
    with pytest.raises(AssertionError, match="output differs") as raised:
        assert_outcomes_equivalent(direct, container)
    message = str(raised.value)
    assert token not in message
    assert "dict with 1 keys" in message


def test_invalid_timeout_values_are_rejected_with_guidance() -> None:
    from syntara_plugin.testing import HarnessUsageError

    step = ReferenceControlStep()
    context = build_execution_context(invocation_id="timeout-validation")
    with pytest.raises(HarnessUsageError, match="timeout_seconds"):
        invoke_step_in_container(step, {"title": "x"}, context=context, timeout_seconds=0)
    with pytest.raises(HarnessUsageError, match="timeout_seconds"):
        invoke_step_in_container(
            step, {"title": "x"}, context=context, timeout_seconds=100_000
        )
