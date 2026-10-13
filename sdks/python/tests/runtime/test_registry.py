"""Tests for the transport-independent typed step registry."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel
from syntara_plugin.runtime import (
    ActionStep,
    BaseNode,
    ExecutionContext,
    StepDescriptor,
    StepRegistrationError,
    StepRegistry,
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


def _make_step(base: type[BaseNode[Any, Any]]) -> BaseNode[EchoInput, EchoOutput]:
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
def test_register_and_resolve_all_four_taxonomies(
    base: type[BaseNode[Any, Any]], taxonomy: StepTaxonomy
) -> None:
    registry = StepRegistry()
    node = _make_step(base)
    descriptor = StepDescriptor.for_node("acme.plugin.echo", node)

    assert descriptor.taxonomy is taxonomy
    registry.register(descriptor, node)
    assert registry.resolve("acme.plugin.echo") is node
    assert registry.step_revision_ids == ("acme.plugin.echo",)


def test_descriptor_for_node_derives_pydantic_schemas() -> None:
    node = _make_step(ActionStep)
    descriptor = StepDescriptor.for_node("acme.plugin.echo", node)

    assert descriptor.input_schema == EchoInput.model_json_schema()
    assert descriptor.output_schema == EchoOutput.model_json_schema()


def test_resolved_node_executes_directly_without_container_invocation() -> None:
    registry = StepRegistry()
    node = _make_step(TaskStep)
    registry.register(StepDescriptor.for_node("acme.plugin.echo", node), node)

    context = ExecutionContext(invocation_id="invoke-1")
    output = registry.resolve("acme.plugin.echo").execute(EchoInput(message="hello"), context)

    assert output == EchoOutput(message="hello", invocation_id="invoke-1")


def test_duplicate_identity_is_rejected() -> None:
    registry = StepRegistry()
    node = _make_step(ActionStep)
    descriptor = StepDescriptor.for_node("acme.plugin.echo", node)
    registry.register(descriptor, node)

    with pytest.raises(StepRegistrationError, match="already registered"):
        registry.register(descriptor, node)


def test_malformed_identity_is_rejected() -> None:
    with pytest.raises(StepRegistrationError, match="Syntara identity"):
        StepDescriptor(
            step_revision_id="not a valid identity",
            taxonomy=StepTaxonomy.ACTION,
            input_schema=EchoInput.model_json_schema(),
            output_schema=EchoOutput.model_json_schema(),
        )


def test_taxonomy_mismatch_is_rejected() -> None:
    registry = StepRegistry()
    node = _make_step(TaskStep)
    descriptor = StepDescriptor(
        step_revision_id="acme.plugin.echo",
        taxonomy=StepTaxonomy.ACTION,
        input_schema=EchoInput.model_json_schema(),
        output_schema=EchoOutput.model_json_schema(),
    )

    with pytest.raises(StepRegistrationError, match="does not match"):
        registry.register(descriptor, node)


def test_platform_managed_driver_is_rejected_as_a_python_implementation() -> None:
    registry = StepRegistry()
    descriptor = StepDescriptor(
        step_revision_id="acme.plugin.echo",
        taxonomy=StepTaxonomy.ACTION,
        input_schema=EchoInput.model_json_schema(),
        output_schema=EchoOutput.model_json_schema(),
    )

    def http_driver(_invocation: Any) -> Any:
        return None

    with pytest.raises(StepRegistrationError, match="platform-managed"):
        registry.register(descriptor, http_driver)  # type: ignore[arg-type]


def test_resolve_unknown_identity_is_rejected() -> None:
    with pytest.raises(StepRegistrationError, match="not registered"):
        StepRegistry().resolve("acme.plugin.missing")
