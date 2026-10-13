"""Adapter that dispatches container invocations to typed Python steps."""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError
from syntara_plugin.contracts import canonical_json_digest

from .bindings import StepBindingError, StepBindings
from .container import (
    ActionRegistry,
    ContainerAction,
    ContainerInvocation,
    ContainerInvocationError,
    ContainerResult,
    FailureCode,
)
from .context import ExecutionContext
from .step import BaseStep

_MAX_VALIDATION_ERRORS = 10
_MAX_VALIDATION_DETAILS_LENGTH = 2_000
_MAX_LOCATION_LENGTH = 128
_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")


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


def _validation_details(error: ValidationError) -> str:
    """Return bounded field paths and stable validation codes.

    Pydantic messages and location keys can be author- or caller-controlled and
    may quote rejected values, so this reports only bounded paths plus the
    stable Pydantic error ``type`` codes. The result is capped so a guaranteed
    input failure can never exceed the transport failure-message limit and
    degrade into an unknown outcome.
    """

    errors = error.errors()
    reported = errors[:_MAX_VALIDATION_ERRORS]
    parts = [f"{_format_location(item['loc'])}: {item['type']}" for item in reported]
    remaining = len(errors) - len(reported)
    if remaining > 0:
        parts.append(f"and {remaining} more")
    details = "; ".join(parts)
    if len(details) > _MAX_VALIDATION_DETAILS_LENGTH:
        details = details[: _MAX_VALIDATION_DETAILS_LENGTH - 3].rstrip() + "..."
    return details


def _format_location(location: object) -> str:
    """Render one bounded error location without raw input values."""

    if isinstance(location, tuple):
        parts = [str(part) for part in location]
    else:
        parts = [str(location)]
    path = ".".join(parts) or "<root>"
    if len(path) > _MAX_LOCATION_LENGTH:
        path = path[: _MAX_LOCATION_LENGTH - 3] + "..."
    return path


def container_action_for_step(step: BaseStep[Any, Any]) -> ContainerAction:
    """Wrap one typed step as a container action.

    The action validates the invocation's raw JSON inputs against the step's
    Pydantic input model, executes the step through ``BaseStep.execute()``, and
    converts the typed output into a ``ContainerResult``. The output schema
    digest echoes the installed digest supplied in the invocation's runtime
    context, matching the platform contract used by the HTTP runner. When no
    installed digest is supplied, a provisional canonical digest of the
    implementation's Pydantic output model is used until the registry-binding
    story binds the installed manifest schema.
    """

    if not isinstance(step, BaseStep):
        message = "step must be a BaseStep implementation"
        raise TypeError(message)
    output_schema_digest = _output_schema_digest(step)
    output_model = step.output_model()

    def action(invocation: ContainerInvocation) -> ContainerResult:
        context = _execution_context(invocation)
        schema_digest = _installed_output_schema_digest(invocation) or output_schema_digest
        try:
            inputs = step.input_model().model_validate(invocation.inputs)
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
        output = step.execute(inputs, context)
        if not isinstance(output, output_model):
            raise ContainerInvocationError(
                FailureCode.ABI_VIOLATION,
                "The plugin action returned an invalid response.",
                outcome="unknown",
            )
        return ContainerResult(schema_digest, output=output.model_dump(mode="json"))

    return action


def container_action_for_bindings(bindings: StepBindings) -> ContainerAction:
    """Wrap typed step bindings as an action resolving by step identity.

    The invocation's ``step_revision_id`` is matched against the declared step
    identity. This serves direct local dispatch and testing; production
    dispatch requires the registry-binding loader that maps an installed step
    revision to its declared identity.
    """

    def action(invocation: ContainerInvocation) -> ContainerResult:
        try:
            step = bindings.resolve(invocation.step_revision_id)
        except StepBindingError as error:
            message = "The requested plugin action is not available in this container image."
            raise ContainerInvocationError(
                FailureCode.NOT_FOUND,
                message,
                outcome="not_started",
            ) from error
        return container_action_for_step(step)(invocation)

    return action


def action_registry_from_steps(bindings: StepBindings) -> ActionRegistry:
    """Build a container action registry with one action per typed step.

    Actions are keyed by declared step identity and matched exactly against the
    invocation's ``step_revision_id``. This supports local execution and tests;
    production dispatch requires the registry-binding loader that maps installed
    step revisions to declared identities.
    """

    actions = {
        step_identity: container_action_for_step(bindings.resolve(step_identity))
        for step_identity in bindings.step_identities
    }
    if not actions:
        message = "at least one typed step must be registered"
        raise ValueError(message)
    return ActionRegistry(actions)
