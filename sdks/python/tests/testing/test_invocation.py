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
    assert outcome.output["workflow_run_id"] == ""
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
