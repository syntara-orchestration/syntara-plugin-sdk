"""Adapter that dispatches container invocations to typed Python steps."""

from __future__ import annotations

import re
from typing import Any

from syntara_plugin.contracts import canonical_json_digest

from .bindings import (
    InstalledActionManifest,
    StepBinding,
    StepBindingError,
    StepBindings,
    StepDescriptor,
    validate_executable_step_binding,
)
from .container import (
    ContainerInvocation,
    ContainerInvocationError,
    ContainerResult,
    ContainerStepHandler,
    FailureCode,
    StepRegistry,
)
from .context import ExecutionContext
from .execution import StepExecutionError, StepExecutor
from .outcome import StandardOutputWrapper
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


def container_step_for_binding(binding: StepBinding) -> ContainerStepHandler:
    """Wrap one typed step as a container handler.

    The handler validates the invocation's raw JSON inputs against the installed
    Action manifest schema for Actions or the implementation-derived schema for
    other taxonomies, constructs the registered Pydantic model, executes the
    step through the shared executor, and converts the typed output into a
    ``ContainerResult``. The output schema digest echoes the installed digest
    supplied in the invocation's runtime
    context, matching the platform contract used by the HTTP runner. When no
    installed digest is supplied, a provisional canonical digest of the
    implementation's Pydantic output model is used until the registry-binding
    story binds the installed manifest schema.
    """

    validate_executable_step_binding(binding.descriptor, binding.step)
    step = binding.step
    executor = StepExecutor(binding)
    output_schema_digest = _output_schema_digest(step)

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        if invocation.step_revision_id != binding.descriptor.step_identity:
            raise ContainerInvocationError(
                FailureCode.NOT_FOUND,
                "The requested plugin step is not available in this container image.",
                outcome="not_started",
            )
        context = _execution_context(invocation)
        schema_digest = _installed_output_schema_digest(invocation) or output_schema_digest
        try:
            wrapper = executor.execute(invocation.inputs, context)
        except StepExecutionError as error:
            raise ContainerInvocationError(
                FailureCode.ABI_VIOLATION,
                "The plugin step returned an invalid response.",
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
                "The plugin step returned an invalid response.",
                outcome="unknown",
            )
        return ContainerResult(schema_digest, output=output)

    return handler


def container_action_for_step(
    step: BaseStep[Any, Any],
    *,
    installed_action: InstalledActionManifest,
) -> ContainerStepHandler:
    """Wrap one Action step with its trusted installed identity and schema."""

    descriptor = StepDescriptor.for_installed_action(step, installed_action)
    return container_step_for_binding(StepBinding(descriptor, step))


def container_step_for_bindings(bindings: StepBindings) -> ContainerStepHandler:
    """Wrap typed step bindings as a handler resolving by step identity.

    The invocation's ``step_revision_id`` is matched against the declared step
    identity. This serves direct local dispatch and testing; production
    dispatch requires the registry-binding loader that maps an installed step
    revision to its declared identity.
    """

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        try:
            binding = bindings.binding(invocation.step_revision_id)
            return container_step_for_binding(binding)(invocation)
        except StepBindingError as error:
            message = "The requested plugin step is not available in this container image."
            raise ContainerInvocationError(
                FailureCode.NOT_FOUND,
                message,
                outcome="not_started",
            ) from error

    return handler


def step_registry_from_bindings(bindings: StepBindings) -> StepRegistry:
    """Build a container step registry with one handler per typed step.

    Handlers are keyed by declared step identity and matched exactly against the
    invocation's ``step_revision_id``. This supports local execution and tests;
    production dispatch requires the registry-binding loader that maps installed
    step revisions to declared identities.
    """

    handlers = {
        step_identity: container_step_for_binding(bindings.binding(step_identity))
        for step_identity in bindings.step_identities
    }
    if not handlers:
        message = "at least one typed step must be registered"
        raise ValueError(message)
    return StepRegistry(handlers)
