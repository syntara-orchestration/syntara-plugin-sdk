"""Adapter that dispatches container invocations to typed Python steps."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from syntara_plugin.contracts import canonical_json_digest

from .bindings import StepBinding, StepBindingError, StepBindings, StepDescriptor
from .container import (
    ActionRegistry,
    ContainerAction,
    ContainerInvocation,
    ContainerInvocationError,
    ContainerResult,
    FailureCode,
)
from .context import ExecutionContext
from .execution import StandardOutputWrapper, StepExecutionError, StepExecutor
from .step import BaseStep

_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
_WRAPPER_SCHEMA_DIGEST = canonical_json_digest(StandardOutputWrapper.model_json_schema())


def _execution_context(invocation: ContainerInvocation) -> ExecutionContext:
    """Build the transport-independent context for one container invocation."""

    return ExecutionContext(
        invocation_id=invocation.invocation_id,
        workflow_run_id=invocation.workflow_run_id,
        runtime_context=invocation.runtime_context,
        cancellation=invocation.cancellation,
    )


def _output_schema_digest(step: BaseStep[Any, Any]) -> str:
    """Return the provisional digest of the implementation's output schema.

    The value uses the SDK's normative canonical JSON form so equivalent
    schemas hash identically across language SDKs. It remains provisional until
    the registry-binding story associates the implementation with the digest of
    its installed manifest schema.
    """

    return canonical_json_digest(step.output_model().model_json_schema())


def _installed_output_schema_digest(invocation: ContainerInvocation) -> str | None:
    """Return the installed output schema digest carried by the runtime context."""

    digest = invocation.runtime_context.get("outputSchemaDigest")
    if digest is None:
        return None
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ContainerInvocationError(
            FailureCode.ABI_VIOLATION,
            "The runtime context carries an invalid output schema digest.",
            outcome="not_started",
        )
    return digest


def container_action_for_binding(binding: StepBinding) -> ContainerAction:
    """Wrap one typed step as a container action.

    The action validates the invocation's raw JSON inputs against the installed
    Action manifest schema, constructs the registered Pydantic model, executes
    the step through the shared executor, and converts the typed output into a
    ``ContainerResult``. The output schema
    digest echoes the installed digest supplied in the invocation's runtime
    context, matching the platform contract used by the HTTP runner. When no
    installed digest is supplied, a provisional canonical digest of the
    implementation's Pydantic output model is used until the registry-binding
    story binds the installed manifest schema.
    """

    step = binding.step
    if not isinstance(step, BaseStep):
        message = "step must be a BaseStep implementation"
        raise TypeError(message)
    executor = StepExecutor(binding)
    output_schema_digest = _output_schema_digest(step)

    def action(invocation: ContainerInvocation) -> ContainerResult:
        if invocation.step_revision_id != binding.descriptor.step_identity:
            raise ContainerInvocationError(
                FailureCode.NOT_FOUND,
                "The requested plugin action is not available in this container image.",
                outcome="not_started",
            )
        context = _execution_context(invocation)
        schema_digest = _installed_output_schema_digest(invocation) or output_schema_digest
        try:
            wrapper = executor.execute(invocation.inputs, context)
        except StepExecutionError as error:
            raise ContainerInvocationError(
                FailureCode.ABI_VIOLATION,
                "The plugin action returned an invalid response.",
                outcome="unknown",
            ) from error
        if wrapper.status != 0:
            return ContainerResult(
                _WRAPPER_SCHEMA_DIGEST,
                output=wrapper.model_dump(mode="json"),
            )
        output = wrapper.output
        if output is None:
            raise ContainerInvocationError(
                FailureCode.ABI_VIOLATION,
                "The plugin action returned an invalid response.",
                outcome="unknown",
            )
        return ContainerResult(schema_digest, output=output)

    return action


def container_action_for_step(
    step: BaseStep[Any, Any],
    *,
    step_identity: str,
    input_schema: Mapping[str, Any],
) -> ContainerAction:
    """Wrap one Action step with its trusted installed identity and schema."""

    descriptor = StepDescriptor.for_installed_action(step_identity, step, input_schema)
    return container_action_for_binding(StepBinding(descriptor, step))


def container_action_for_bindings(bindings: StepBindings) -> ContainerAction:
    """Wrap typed step bindings as an action resolving by step identity.

    The invocation's ``step_revision_id`` is matched against the declared step
    identity. This serves direct local dispatch and testing; production
    dispatch requires the registry-binding loader that maps an installed step
    revision to its declared identity.
    """

    def action(invocation: ContainerInvocation) -> ContainerResult:
        try:
            binding = bindings.binding(invocation.step_revision_id)
            return container_action_for_binding(binding)(invocation)
        except StepBindingError as error:
            message = "The requested plugin action is not available in this container image."
            raise ContainerInvocationError(
                FailureCode.NOT_FOUND,
                message,
                outcome="not_started",
            ) from error

    return action


def action_registry_from_steps(bindings: StepBindings) -> ActionRegistry:
    """Build a container action registry with one action per typed step.

    Actions are keyed by declared step identity and matched exactly against the
    invocation's ``step_revision_id``. This supports local execution and tests;
    production dispatch requires the registry-binding loader that maps installed
    step revisions to declared identities.
    """

    actions = {
        step_identity: container_action_for_binding(bindings.binding(step_identity))
        for step_identity in bindings.step_identities
    }
    if not actions:
        message = "at least one typed step must be registered"
        raise ValueError(message)
    return ActionRegistry(actions)
