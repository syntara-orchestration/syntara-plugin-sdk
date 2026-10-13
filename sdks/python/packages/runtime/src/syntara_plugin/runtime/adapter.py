"""Adapter that dispatches container invocations to typed Python steps."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import ValidationError

from .container import (
    ActionRegistry,
    ContainerAction,
    ContainerInvocation,
    ContainerInvocationError,
    ContainerResult,
    FailureCode,
)
from .context import ExecutionContext
from .node import BaseNode
from .registry import StepRegistrationError, StepRegistry


def _execution_context(invocation: ContainerInvocation) -> ExecutionContext:
    """Build the transport-independent context for one container invocation."""

    return ExecutionContext(
        invocation_id=invocation.invocation_id,
        runtime_context=invocation.runtime_context,
        cancellation=invocation.cancellation,
    )


def _output_schema_digest(node: BaseNode[Any, Any]) -> str:
    """Hash the node's Pydantic output schema into the container ABI digest."""

    schema = node.output_model().model_json_schema()
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validation_details(error: ValidationError) -> str:
    """Return field paths and messages without echoing raw input values."""

    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['msg']}"
        for item in error.errors()
    )


def container_action_for_node(node: BaseNode[Any, Any]) -> ContainerAction:
    """Wrap one typed step as a container action.

    The action validates the invocation's raw JSON inputs against the node's
    Pydantic input model, executes the node through ``BaseNode.execute()``, and
    converts the typed output into a ``ContainerResult``. The output schema
    digest is derived from the implementation's own Pydantic output model;
    comparing it with an installed manifest schema is owned by the schema-drift
    validation story.
    """

    if not isinstance(node, BaseNode):
        message = "node must be a BaseNode implementation"
        raise TypeError(message)
    output_schema_digest = _output_schema_digest(node)

    def action(invocation: ContainerInvocation) -> ContainerResult:
        context = _execution_context(invocation)
        try:
            inputs = node.input_model().model_validate(invocation.inputs)
        except ValidationError as error:
            message = (
                "The plugin action input failed schema validation: "
                f"{_validation_details(error)}"
            )
            raise ContainerInvocationError(
                FailureCode.INVALID_INPUT,
                message,
                outcome="not_started",
            ) from error
        output = node.execute(inputs, context)
        return ContainerResult(output_schema_digest, output=output.model_dump(mode="json"))

    return action


def container_action_for_registry(registry: StepRegistry) -> ContainerAction:
    """Wrap a typed step registry as an action resolving by step identity."""

    def action(invocation: ContainerInvocation) -> ContainerResult:
        try:
            node = registry.resolve(invocation.step_revision_id)
        except StepRegistrationError as error:
            message = "The requested plugin action is not available in this container image."
            raise ContainerInvocationError(
                FailureCode.NOT_FOUND,
                message,
                outcome="not_started",
            ) from error
        return container_action_for_node(node)(invocation)

    return action


def action_registry_from_steps(registry: StepRegistry) -> ActionRegistry:
    """Build a container action registry with one action per typed step."""

    actions = {
        step_revision_id: container_action_for_node(registry.resolve(step_revision_id))
        for step_revision_id in registry.step_revision_ids
    }
    if not actions:
        message = "at least one typed step must be registered"
        raise ValueError(message)
    return ActionRegistry(actions)
