"""Tests for the container-adapter invocation path and path equivalence."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel
from syntara_plugin.runtime import ExecutionContext, TaskStep
from syntara_plugin.testing import (
    INVALID_INPUT_CASES,
    assert_outcomes_equivalent,
    build_execution_context,
    invoke_step,
    invoke_step_in_container,
)
from syntara_plugin.testing.payloads import (
    FailingReferenceStep,
    ReferenceTaskStep,
    schema_violating_binding,
)


class CrashingTask(ReferenceTaskStep):
    def run(self, inputs: Any, context: Any) -> Any:
        raise RuntimeError("the business logic crashed")


def test_success_over_the_container_abi() -> None:
    outcome = invoke_step_in_container(ReferenceTaskStep(), {"title": "deploy"})
    assert outcome.ok
    assert outcome.status == 0
    assert outcome.run_calls == 1
    assert outcome.output == {"summary": "processed deploy", "priority": "low"}
    assert outcome.transport == "container"
    assert outcome.raw is not None


def test_invalid_input_fails_before_the_hook_over_the_container_abi() -> None:
    outcome = invoke_step_in_container(ReferenceTaskStep(), {"priority": "high"})
    assert not outcome.ok
    assert outcome.status == 1
    assert outcome.run_calls == 0
    assert outcome.error is not None
    assert outcome.error.code == "INVALID_INPUT"


@pytest.mark.parametrize("case", INVALID_INPUT_CASES, ids=lambda case: case.name)
def test_invalid_cases_are_equivalent_across_paths(case: Any) -> None:
    context = build_execution_context(invocation_id=f"equiv-{case.name}")
    direct = invoke_step(ReferenceTaskStep(), case.inputs, context=context)
    container = invoke_step_in_container(ReferenceTaskStep(), case.inputs, context=context)
    assert_outcomes_equivalent(direct, container)


def test_valid_outcomes_are_equivalent_across_paths() -> None:
    context = build_execution_context(invocation_id="equiv-valid")
    inputs = {"title": "deploy", "priority": "high", "count": 2}
    direct = invoke_step(ReferenceTaskStep(), inputs, context=context)
    container = invoke_step_in_container(ReferenceTaskStep(), inputs, context=context)
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
    direct = invoke_step(CrashingTask(), {"title": "x"}, context=context)
    container = invoke_step_in_container(CrashingTask(), {"title": "x"}, context=context)
    assert_outcomes_equivalent(direct, container)


def test_run_id_echoing_step_is_equivalent_under_a_shared_context() -> None:
    """Both paths must present the same run id when the context carries none."""

    class RunIdInput(BaseModel):
        value: str = "x"

    class RunIdOutput(BaseModel):
        invocation_id: str
        workflow_run_id: str

    class RunIdEchoTask(TaskStep[RunIdInput, RunIdOutput]):
        def run(self, inputs: RunIdInput, context: ExecutionContext) -> RunIdOutput:
            return RunIdOutput(
                invocation_id=context.invocation_id,
                workflow_run_id=context.workflow_run_id or "",
            )

    context = build_execution_context(invocation_id="equiv-run-id")
    direct = invoke_step(RunIdEchoTask(), {}, context=context)
    container = invoke_step_in_container(RunIdEchoTask(), {}, context=context)
    assert_outcomes_equivalent(direct, container)
    assert direct.output == container.output
    assert direct.output["workflow_run_id"] == "equiv-run-id-run"


def test_a_deadlocked_step_returns_its_timeout_without_hanging_the_session() -> None:
    """A step that ignores cancellation cannot block beyond its timeout."""

    import time

    class SleepyInput(BaseModel):
        value: str = "x"

    class SleepyOutput(BaseModel):
        value: str = "done"

    class SleepyTask(TaskStep[SleepyInput, SleepyOutput]):
        def run(self, inputs: SleepyInput, context: ExecutionContext) -> SleepyOutput:
            time.sleep(30)
            return SleepyOutput(value="done")

    started = time.monotonic()
    outcome = invoke_step_in_container(
        SleepyTask(), {}, context=build_execution_context(invocation_id="timeout-probe"),
        timeout_seconds=1,
    )
    elapsed = time.monotonic() - started
    assert not outcome.ok
    assert outcome.raised is not None
    assert outcome.raised.code.value == "TIMEOUT"
    assert elapsed < 10, f"cleanup was not bounded: {elapsed:.1f}s"


def test_the_same_invocation_id_can_be_reused_sequentially() -> None:
    context = build_execution_context(invocation_id="reused-id")
    first = invoke_step_in_container(ReferenceTaskStep(), {"title": "a"}, context=context)
    second = invoke_step_in_container(ReferenceTaskStep(), {"title": "b"}, context=context)
    assert first.ok
    assert second.ok
    assert first.output != second.output


def test_equivalence_assertion_requires_direct_first() -> None:
    direct = invoke_step(ReferenceTaskStep(), {"title": "x"})
    container = invoke_step_in_container(ReferenceTaskStep(), {"title": "x"})
    with pytest.raises(AssertionError, match="direct outcome first"):
        assert_outcomes_equivalent(container, direct)


def test_equivalence_assertion_detects_different_outcomes() -> None:
    direct = invoke_step(ReferenceTaskStep(), {"title": "x"})
    container = invoke_step_in_container(ReferenceTaskStep(), {"priority": "urgent"})
    with pytest.raises(AssertionError, match="outcomes differ"):
        assert_outcomes_equivalent(direct, container)
