"""Tests for the typed Python step execution API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from syntara_plugin.runtime import (
    ActionStep,
    BaseNode,
    ExecutionContext,
    TaskStep,
    TriggerStep,
    WorkflowStep,
)


class EchoInput(BaseModel):
    message: str


class EchoOutput(BaseModel):
    message: str
    invocation_id: str
    cancelled: bool


class ActionEchoStep(ActionStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
        return EchoOutput(
            message=inputs.message,
            invocation_id=context.invocation_id,
            cancelled=context.cancelled,
        )


class TaskEchoStep(TaskStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
        return EchoOutput(
            message=inputs.message,
            invocation_id=context.invocation_id,
            cancelled=context.cancelled,
        )


class WorkflowEchoStep(WorkflowStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
        return EchoOutput(
            message=inputs.message,
            invocation_id=context.invocation_id,
            cancelled=context.cancelled,
        )


class TriggerEchoStep(TriggerStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
        return EchoOutput(
            message=inputs.message,
            invocation_id=context.invocation_id,
            cancelled=context.cancelled,
        )


def test_public_api_exports_the_typed_step_classes() -> None:
    assert issubclass(ActionStep, BaseNode)
    assert issubclass(TaskStep, BaseNode)
    assert issubclass(WorkflowStep, BaseNode)
    assert issubclass(TriggerStep, BaseNode)


def test_execution_context_rejects_an_empty_invocation_id() -> None:
    try:
        ExecutionContext(invocation_id="")
    except ValueError as error:
        assert "invocation_id" in str(error)
    else:
        raise AssertionError("expected a ValueError for an empty invocation_id")


def test_execution_context_reports_cooperative_cancellation() -> None:
    context = ExecutionContext(invocation_id="invoke-1")

    assert not context.cancelled
    context.cancellation.set()
    assert context.cancelled


def test_all_taxonomy_classes_resolve_their_pydantic_models() -> None:
    for step_type in (ActionEchoStep, TaskEchoStep, WorkflowEchoStep, TriggerEchoStep):
        assert step_type.input_model() is EchoInput
        assert step_type.output_model() is EchoOutput


def test_direct_execution_delegates_and_propagates_context() -> None:
    context = ExecutionContext(
        invocation_id="invoke-1",
        workflow_run_id="run-1",
        runtime_context={"environment": "test"},
    )

    output = ActionEchoStep().execute(EchoInput(message="hello"), context)

    assert output == EchoOutput(
        message="hello",
        invocation_id="invoke-1",
        cancelled=False,
    )


def test_direct_execution_does_not_require_a_container_invocation() -> None:
    """Direct SDK execution exercises the typed API, not the container transport."""

    context = ExecutionContext(invocation_id="invoke-2")
    context.cancellation.set()

    output = TriggerEchoStep().execute(EchoInput(message="event"), context)

    assert output.cancelled is True
    assert output.invocation_id == "invoke-2"


def test_non_concrete_specialization_is_rejected() -> None:
    class InvalidStep(ActionStep[Any, Any]):
        def run(self, inputs: Any, context: ExecutionContext) -> Any:
            return inputs

    try:
        InvalidStep.input_model()
    except TypeError as error:
        assert "concrete Pydantic" in str(error)
    else:
        raise AssertionError("expected a TypeError for a non-concrete specialization")
