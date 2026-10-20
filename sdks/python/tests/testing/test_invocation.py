"""Tests for the direct invocation path through the shared StepExecutor."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, ExecutionContext, StepExecutionError, TaskStep
from syntara_plugin.runtime.container import ContainerInvocationError
from syntara_plugin.testing import (
    FIXED_SECRET_SENTINEL,
    INVALID_INPUT_CASES,
    HarnessUsageError,
    assert_no_secrets,
    build_execution_context,
    invoke_step,
)
from syntara_plugin.testing.payloads import (
    FailingReferenceStep,
    ReferenceTaskStep,
    schema_violating_binding,
)


class CaptureInput(BaseModel):
    value: str = "x"


class CaptureOutput(BaseModel):
    invocation_id: str
    workflow_run_id: str
    cancelled: bool


class CaptureContextTask(TaskStep[CaptureInput, CaptureOutput]):
    def run(self, inputs: CaptureInput, context: ExecutionContext) -> CaptureOutput:
        return CaptureOutput(
            invocation_id=context.invocation_id,
            workflow_run_id=context.workflow_run_id or "",
            cancelled=context.cancelled,
        )


class LocalActionStep(ActionStep[CaptureInput, CaptureOutput]):
    def run(self, inputs: CaptureInput, context: ExecutionContext) -> CaptureOutput:
        return CaptureOutput(
            invocation_id=context.invocation_id,
            workflow_run_id="", cancelled=False
        )


class CrashingTask(TaskStep[CaptureInput, CaptureOutput]):
    def run(self, inputs: CaptureInput, context: ExecutionContext) -> CaptureOutput:
        raise RuntimeError("the business logic crashed")


def test_valid_input_reaches_the_hook_once() -> None:
    outcome = invoke_step(ReferenceTaskStep(), {"title": "deploy"})
    assert outcome.ok
    assert outcome.status == 0
    assert outcome.status_message == "Completed"
    assert outcome.run_calls == 1
    assert outcome.raised is None
    assert outcome.output == {"summary": "processed deploy", "priority": "low"}
    assert outcome.transport == "direct"
    assert outcome.raw is not None


def test_default_context_flows_into_the_step() -> None:
    outcome = invoke_step(CaptureContextTask(), {"value": "x"})
    assert outcome.ok
    assert outcome.output is not None
    assert outcome.output["invocation_id"].startswith("test-invocation-")
    # Both execution paths substitute a deterministic run id when none is set,
    # so the container ABI's required identifier never diverges from the
    # direct path.
    assert outcome.output["workflow_run_id"] == f"{outcome.output['invocation_id']}-run"
    assert outcome.output["cancelled"] is False


def test_custom_context_flows_into_the_step() -> None:
    context = build_execution_context(
        invocation_id="ctx-1", workflow_run_id="run-9", cancelled=True
    )
    outcome = invoke_step(CaptureContextTask(), {}, context=context)
    assert outcome.output == {
        "invocation_id": "ctx-1",
        "workflow_run_id": "run-9",
        "cancelled": True,
    }


@pytest.mark.parametrize("case", INVALID_INPUT_CASES, ids=lambda case: case.name)
def test_invalid_input_blocks_the_business_hook(case: Any) -> None:
    outcome = invoke_step(ReferenceTaskStep(), case.inputs)
    assert not outcome.ok
    assert outcome.status == 1
    assert outcome.run_calls == 0
    assert outcome.raised is None
    assert outcome.error is not None
    assert outcome.error.code == "INVALID_INPUT"
    assert outcome.error.message
    assert outcome.output is None


def test_invalid_input_message_never_echoes_the_rejected_value() -> None:
    outcome = invoke_step(ReferenceTaskStep(), {"title": "t", "count": "two"})
    assert outcome.error is not None
    assert "two" not in outcome.error.message


def test_deliberate_failure_is_captured_with_its_code() -> None:
    outcome = invoke_step(FailingReferenceStep(), {"title": "deploy"})
    assert not outcome.ok
    assert outcome.run_calls == 1
    assert isinstance(outcome.raised, ContainerInvocationError)
    assert outcome.raised.code.value == "UPSTREAM_TRANSIENT"
    assert outcome.raised.retryable is True


def test_schema_invalid_output_is_captured_as_execution_error() -> None:
    outcome = invoke_step(schema_violating_binding(), {"title": "a-title-longer-than-three"})
    assert not outcome.ok
    assert outcome.run_calls == 1
    assert isinstance(outcome.raised, StepExecutionError)


def test_business_crash_is_captured_without_swallowing() -> None:
    outcome = invoke_step(CrashingTask(), {})
    assert not outcome.ok
    assert outcome.run_calls == 1
    assert isinstance(outcome.raised, RuntimeError)


def test_entry_point_string_target_resolves_and_executes() -> None:
    outcome = invoke_step(f"{__name__}:CaptureContextTask", {"value": "x"})
    assert outcome.ok
    assert outcome.run_calls == 1


def test_action_without_installed_manifest_is_rejected_with_guidance() -> None:
    with pytest.raises(HarnessUsageError, match="bind_action"):
        invoke_step(LocalActionStep, {})


def test_credential_reference_input_does_not_leak_into_the_outcome() -> None:
    outcome = invoke_step(
        ReferenceTaskStep(), {"title": "deploy", "api_token": FIXED_SECRET_SENTINEL}
    )
    assert outcome.ok
    assert_no_secrets(outcome, [FIXED_SECRET_SENTINEL])


def test_raw_contexts_are_held_to_the_same_identity_rules() -> None:
    """Directly constructed ExecutionContext values are validated on both paths.

    The builder validates its inputs, but the invocation helpers also accept
    raw runtime contexts; the shared normalization rejects identifiers the
    container ABI would refuse before either path runs.
    """

    from syntara_plugin.testing import invoke_step_in_container

    step = ReferenceTaskStep()
    invalid_run_id = ExecutionContext(
        invocation_id="raw-probe", workflow_run_id="not valid"
    )
    with pytest.raises(HarnessUsageError, match="workflow_run_id"):
        invoke_step_in_container(step, {"title": "x"}, context=invalid_run_id)

    oversized_invocation = ExecutionContext(invocation_id="x" * 300)
    with pytest.raises(HarnessUsageError, match="invocation_id"):
        invoke_step(step, {"title": "x"}, context=oversized_invocation)
    with pytest.raises(HarnessUsageError, match="invocation_id"):
        invoke_step_in_container(step, {"title": "x"}, context=oversized_invocation)


def test_non_transportable_invocations_are_rejected_on_both_paths() -> None:
    """Inputs the container ABI cannot encode fail up front, on both paths."""

    from syntara_plugin.testing import invoke_step_in_container

    step = ReferenceTaskStep()
    with pytest.raises(HarnessUsageError, match="JSON object"):
        invoke_step(step, ["not", "a", "mapping"])
    with pytest.raises(HarnessUsageError, match="JSON object"):
        invoke_step_in_container(step, ["not", "a", "mapping"])

    oversized = {"title": "x", "blob": "x" * 1_200_000}
    with pytest.raises(HarnessUsageError, match="too large"):
        invoke_step(step, oversized)
    with pytest.raises(HarnessUsageError, match="too large"):
        invoke_step_in_container(step, oversized)

    context = build_execution_context(
        invocation_id="json-probe", runtime_context={"probe": object()}
    )
    with pytest.raises(HarnessUsageError, match="JSON-compatible"):
        invoke_step(step, {"title": "x"}, context=context)
    with pytest.raises(HarnessUsageError, match="JSON-compatible"):
        invoke_step_in_container(step, {"title": "x"}, context=context)


def test_both_paths_deliver_json_normalized_inputs() -> None:
    """Tuples and non-string keys arrive as JSON-native values on both paths."""

    from syntara_plugin.testing import assert_outcomes_equivalent, invoke_step_in_container

    class LooseInput(BaseModel):
        model_config = {"extra": "allow"}
        title: str = "x"

    class SeenOutput(BaseModel):
        kind: str

    class ObserveTask(TaskStep[LooseInput, SeenOutput]):
        def run(self, inputs: LooseInput, context: ExecutionContext) -> SeenOutput:
            extra = inputs.model_extra or {}
            first = next(iter(extra.values()), None)
            return SeenOutput(kind=type(first).__name__)

    step = ObserveTask()
    context = build_execution_context(invocation_id="json-parity")
    inputs = {"title": "x", "value": (1, 2)}
    direct = invoke_step(step, inputs, context=context)
    container = invoke_step_in_container(step, inputs, context=context)
    assert direct.output == {"kind": "list"}
    assert_outcomes_equivalent(direct, container)


def test_runtime_context_mutation_does_not_leak_between_paths() -> None:
    """Each path receives its own copy of the runtime context data."""

    from syntara_plugin.testing import invoke_step_in_container

    class LooseInput(BaseModel):
        model_config = {"extra": "allow"}
        title: str = "x"

    class ItemsOutput(BaseModel):
        items: list

    class MutateTask(TaskStep[LooseInput, ItemsOutput]):
        def run(self, inputs: LooseInput, context: ExecutionContext) -> ItemsOutput:
            context.runtime_context["items"].append("mutated-by-step")
            return ItemsOutput(items=list(context.runtime_context["items"]))

    context = build_execution_context(
        invocation_id="ctx-isolation", runtime_context={"items": ["original"]}
    )
    direct = invoke_step(MutateTask(), {}, context=context)
    assert direct.output == {"items": ["original", "mutated-by-step"]}
    assert context.runtime_context["items"] == ["original"]
    container = invoke_step_in_container(MutateTask(), {}, context=context)
    assert container.output == {"items": ["original", "mutated-by-step"]}


def test_system_exit_is_normalized_on_both_paths() -> None:
    """A step calling sys.exit becomes a raised outcome on each path."""

    from syntara_plugin.testing import assert_outcomes_equivalent, invoke_step_in_container

    class LooseInput(BaseModel):
        model_config = {"extra": "allow"}
        title: str = "x"

    class NeverOutput(BaseModel):
        value: str = "unreachable"

    class ExitingTask(TaskStep[LooseInput, NeverOutput]):
        def run(self, inputs: LooseInput, context: ExecutionContext) -> NeverOutput:
            raise SystemExit(3)

    step = ExitingTask()
    context = build_execution_context(invocation_id="sysexit-parity")
    direct = invoke_step(step, {}, context=context)
    container = invoke_step_in_container(step, {}, context=context)
    assert isinstance(direct.raised, SystemExit)
    assert not direct.ok
    assert_outcomes_equivalent(direct, container)
