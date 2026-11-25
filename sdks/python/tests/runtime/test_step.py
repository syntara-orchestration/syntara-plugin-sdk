"""Tests for the typed Python step type execution API."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel
from syntara_plugin.runtime import (
    ActionStep,
    BaseStep,
    ControlStep,
    ExecutionContext,
    TriggerStep,
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


class ControlEchoStep(ControlStep[EchoInput, EchoOutput]):
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
    assert issubclass(ActionStep, BaseStep)
    assert issubclass(ControlStep, BaseStep)
    assert issubclass(TriggerStep, BaseStep)


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


def test_partial_result_context_method_emits_ordered_snapshots() -> None:
    from syntara_plugin.runtime.container import _new_partial_result_reporter
    from syntara_plugin.runtime.protocol import container_pb2

    events: list[container_pb2.ExecutionEvent] = []
    context = ExecutionContext(
        invocation_id="invoke-1",
        _partial_result_reporter=_new_partial_result_reporter("invoke-1", events.append),
    )
    payload = {"job_id": "123", "details": {"url": "https://aap.example/jobs/123"}}

    reporter_context = context._with_credentials(())
    reporter_context.report_partial_result(payload)
    reporter_context.report_partial_result({}, is_last=True)

    assert len(events) == 2
    assert events[0].progress.sequence == 1
    assert events[0].progress.state == "progress"
    assert events[0].progress.partial_result.is_last is False
    assert json.loads(events[0].progress.partial_result.data_json) == payload
    assert events[1].progress.sequence == 2
    assert events[1].progress.state == "progress"
    assert events[1].progress.partial_result.is_last is True
    assert events[1].progress.partial_result.data_json == b"{}"
    reporter = context._partial_result_reporter
    assert reporter is not None
    reporter.close()
    reporter_context.report_partial_result({"job_id": "123"})
    assert len(events) == 2
    with pytest.raises(ValueError, match="16384-byte"):
        _new_partial_result_reporter("oversized", lambda _event: None)(
            {"value": "x" * 16_385}
        )
    with pytest.raises(ValueError, match="32-property"):
        _new_partial_result_reporter("too-many", lambda _event: None)(
            {f"key-{index}": index for index in range(33)}
        )

    final_reporter = _new_partial_result_reporter("final", lambda _event: None)
    final_reporter({}, True)
    with pytest.raises(ValueError, match="after is_last=True"):
        final_reporter({"late": True})


def test_partial_result_is_unavailable_outside_container_execution() -> None:
    context = ExecutionContext(invocation_id="direct")

    with pytest.raises(RuntimeError, match="unavailable"):
        context.report_partial_result({"job_id": "123"})


def test_all_category_classes_resolve_their_pydantic_models() -> None:
    for step_type in (ActionEchoStep, ControlEchoStep, TriggerEchoStep):
        assert step_type.input_model() is EchoInput
        assert step_type.output_model() is EchoOutput


def test_generic_intermediate_base_class_resolves_models_transitively() -> None:
    """A shared generic base must not leave the models as unresolved TypeVars."""

    class GenericBase[I: BaseModel, O: BaseModel](ActionStep[I, O]):
        """Shared helpers for a family of steps."""

    class ConcreteStep(GenericBase[EchoInput, EchoOutput]):
        def run(self, inputs: EchoInput, context: Any) -> EchoOutput:
            return EchoOutput(
                message=inputs.message,
                invocation_id=context.invocation_id,
                cancelled=context.cancelled,
            )

    assert ConcreteStep.input_model() is EchoInput
    assert ConcreteStep.output_model() is EchoOutput


@pytest.mark.parametrize(
    "step_type",
    [ActionEchoStep, ControlEchoStep, TriggerEchoStep],
)
def test_direct_execution_delegates_and_propagates_context(
    step_type: type[ActionEchoStep] | type[ControlEchoStep] | type[TriggerEchoStep],
) -> None:
    context = ExecutionContext(
        invocation_id="invoke-1",

        runtime_context={"execution_id": "run-1", "environment": "test"},
    )

    output = step_type().execute(EchoInput(message="hello"), context)

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
