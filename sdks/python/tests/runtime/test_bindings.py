"""Tests for the transport-independent typed step bindings."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel
from syntara_plugin.runtime import (
    ActionStep,
    BaseStep,
    ExecutionContext,
    StepBindingError,
    StepBindings,
    StepDescriptor,
    StepTaxonomy,
    TaskStep,
    TriggerStep,
    WorkflowStep,
)


class EchoInput(BaseModel):
    message: str


class EchoOutput(BaseModel):
    message: str
    invocation_id: str


def _make_step(base: type[BaseStep[Any, Any]]) -> BaseStep[EchoInput, EchoOutput]:
    class Step(base[EchoInput, EchoOutput]):  # type: ignore[valid-type]
        def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
            return EchoOutput(message=inputs.message, invocation_id=context.invocation_id)

    return Step()


@pytest.mark.parametrize(
    ("base", "taxonomy"),
    [
        (ActionStep, StepTaxonomy.ACTION),
        (TaskStep, StepTaxonomy.TASK),
        (WorkflowStep, StepTaxonomy.WORKFLOW),
        (TriggerStep, StepTaxonomy.TRIGGER),
    ],
)
def test_bind_and_resolve_all_four_taxonomies(
    base: type[BaseStep[Any, Any]], taxonomy: StepTaxonomy
) -> None:
    bindings = StepBindings()
    step = _make_step(base)
    descriptor = StepDescriptor.for_step("acme.plugin.echo", step)

    assert descriptor.taxonomy is taxonomy
    bindings.bind(descriptor, step)
    assert bindings.resolve("acme.plugin.echo") is step
    assert bindings.step_identities == ("acme.plugin.echo",)


def test_descriptor_for_step_derives_pydantic_schemas() -> None:
    step = _make_step(ActionStep)
    descriptor = StepDescriptor.for_step("acme.plugin.echo", step)

    assert descriptor.input_schema == EchoInput.model_json_schema()
    assert descriptor.output_schema == EchoOutput.model_json_schema()


def test_resolved_step_executes_directly_without_container_invocation() -> None:
    bindings = StepBindings()
    step = _make_step(TaskStep)
    bindings.bind(StepDescriptor.for_step("acme.plugin.echo", step), step)

    context = ExecutionContext(invocation_id="invoke-1")
    output = bindings.resolve("acme.plugin.echo").execute(EchoInput(message="hello"), context)

    assert output == EchoOutput(message="hello", invocation_id="invoke-1")


def test_duplicate_identity_is_rejected() -> None:
    bindings = StepBindings()
    step = _make_step(ActionStep)
    descriptor = StepDescriptor.for_step("acme.plugin.echo", step)
    bindings.bind(descriptor, step)

    with pytest.raises(StepBindingError, match="already bound"):
        bindings.bind(descriptor, step)


def test_malformed_identity_is_rejected() -> None:
    with pytest.raises(StepBindingError, match="step_identity"):
        StepDescriptor(
            step_identity="not a valid identity",
            taxonomy=StepTaxonomy.ACTION,
            input_schema=EchoInput.model_json_schema(),
            output_schema=EchoOutput.model_json_schema(),
        )


def test_taxonomy_mismatch_is_rejected() -> None:
    bindings = StepBindings()
    step = _make_step(TaskStep)
    descriptor = StepDescriptor(
        step_identity="acme.plugin.echo",
        taxonomy=StepTaxonomy.ACTION,
        input_schema=EchoInput.model_json_schema(),
        output_schema=EchoOutput.model_json_schema(),
    )

    with pytest.raises(StepBindingError, match="does not match"):
        bindings.bind(descriptor, step)


def test_taxonomy_strings_are_normalized() -> None:
    descriptor = StepDescriptor(
        step_identity="acme.plugin.echo",
        taxonomy="task",
        input_schema=EchoInput.model_json_schema(),
        output_schema=EchoOutput.model_json_schema(),
    )

    assert descriptor.taxonomy is StepTaxonomy.TASK


def test_invalid_taxonomy_is_rejected() -> None:
    with pytest.raises(StepBindingError, match="valid step taxonomy"):
        StepDescriptor(
            step_identity="acme.plugin.echo",
            taxonomy="not-a-taxonomy",
            input_schema=EchoInput.model_json_schema(),
            output_schema=EchoOutput.model_json_schema(),
        )


def test_platform_managed_driver_is_rejected_as_a_python_implementation() -> None:
    bindings = StepBindings()
    descriptor = StepDescriptor(
        step_identity="acme.plugin.echo",
        taxonomy=StepTaxonomy.ACTION,
        input_schema=EchoInput.model_json_schema(),
        output_schema=EchoOutput.model_json_schema(),
    )

    def http_driver(_invocation: Any) -> Any:
        return None

    with pytest.raises(StepBindingError, match="platform-managed"):
        bindings.bind(descriptor, http_driver)  # type: ignore[arg-type]


def test_resolve_unknown_identity_is_rejected() -> None:
    with pytest.raises(StepBindingError, match="not bound"):
        StepBindings().resolve("acme.plugin.missing")
