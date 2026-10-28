"""Adapter that dispatches container invocations to typed Python steps."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from syntara_plugin.contracts import CanonicalJsonError, canonical_json_bytes, canonical_json_digest

from . import invoke
from .bindings import (
    InstalledStepManifest,
    StepBinding,
    StepBindingError,
    StepBindings,
    StepDescriptor,
    _thaw_json_document,
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
from .execution import StepExecutionError
from .outcome import StandardOutputWrapper
from .step import BaseStep

_REFERENCE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_INVALID_STEP_RESPONSE = "The plugin step returned an invalid response."
# Business results above this canonical-JSON size cannot travel inline over the
# container ABI and are externalized to an object-store reference instead.
_MAX_INLINE_RESULT_BYTES = 1_048_576


@runtime_checkable
class ResultObjectStore(Protocol):
    """Execution-Plane boundary that externalizes an oversized business result.

    The SDK hands the serialized business result and its content digest to the
    object store and receives back a Syntara-identity reference to carry on
    ``ContainerResult.object_reference``. The SDK never owns storage, transport,
    or addressing policy; the host supplies the implementation.
    """

    def store(self, payload: bytes, *, content_digest: str) -> str:
        """Persist ``payload`` and return its object-store reference identity."""
        ...


def _execution_context(invocation: ContainerInvocation) -> ExecutionContext:
    """Build the transport-independent context for one container invocation."""

    return ExecutionContext(
        invocation_id=invocation.invocation_id,
        workflow_run_id=invocation.workflow_run_id,
        runtime_context=invocation.runtime_context,
        cancellation=invocation.cancellation,
    )


def _applied_output_schema_digest(binding: StepBinding) -> str:
    """Return the digest of the output schema the executor actually applies.

    The digest is computed over the descriptor's declared output schema in the
    SDK's normative canonical JSON form, so equivalent schemas hash identically
    across language SDKs. Binding the digest to the applied schema keeps the
    terminal ``Result`` self-describing: a digest-resolving consumer reads the
    same schema the business result was validated against.
    """

    return canonical_json_digest(_thaw_json_document(binding.descriptor.output_schema))


def _thaw_result_value(value: Any) -> Any:
    """Materialize immutable wrapper values as ordinary JSON containers."""

    if isinstance(value, Mapping):
        return {key: _thaw_result_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_result_value(item) for item in value]
    return value


def _place_business_result(
    result: Mapping[str, Any],
    object_store: ResultObjectStore | None,
) -> tuple[Mapping[str, Any] | None, str | None]:
    """Return the business result as inline output or an object-store reference.

    A result within the inline bound travels inline. A larger result is
    externalized through ``object_store``; when no store is wired, an oversized
    result is a safe ABI violation rather than a silently truncated response.
    """

    invalid_result = False
    try:
        payload = canonical_json_bytes(_thaw_result_value(result))
    except (CanonicalJsonError, RecursionError):
        invalid_result = True
    if invalid_result:
        raise ContainerInvocationError(
            FailureCode.ABI_VIOLATION,
            _INVALID_STEP_RESPONSE,
            outcome="unknown",
        )
    if len(payload) <= _MAX_INLINE_RESULT_BYTES:
        return json.loads(payload), None
    if object_store is None:
        raise ContainerInvocationError(
            FailureCode.ABI_VIOLATION,
            "The business result exceeds the inline limit and no object store is configured.",
            outcome="unknown",
        )
    content_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    store_failed = False
    reference = ""
    try:
        reference = object_store.store(payload, content_digest=content_digest)
    except Exception:
        store_failed = True
    if store_failed:
        raise ContainerInvocationError(
            FailureCode.ABI_VIOLATION,
            "The business result could not be externalized to the object store.",
            outcome="unknown",
        )
    if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
        raise ContainerInvocationError(
            FailureCode.ABI_VIOLATION,
            "The object store returned an invalid result reference.",
            outcome="unknown",
        )
    return None, reference


def _container_result(
    wrapper: StandardOutputWrapper,
    output_schema_digest: str,
    error_schema_digest: str,
    object_store: ResultObjectStore | None,
) -> ContainerResult:
    """Map the complete runtime envelope onto the container ABI result.

    ``ContainerResult`` is the transport representation of a valid SDK
    outcome: status, status message, optional output, and optional error
    metadata all survive the mapping. Inline versus object-reference placement
    applies to any business output, regardless of whether the step deliberately
    returned a nonzero status. Exceptions that cannot produce a valid wrapper
    are mapped separately to the container ``Failure`` event.
    """

    output: Mapping[str, Any] | None = None
    object_reference: str | None = None
    business_result = wrapper.result
    if business_result is not None:
        if not isinstance(business_result, Mapping):
            raise ContainerInvocationError(
                FailureCode.ABI_VIOLATION,
                _INVALID_STEP_RESPONSE,
                outcome="unknown",
            )
        output, object_reference = _place_business_result(business_result, object_store)

    error = wrapper.error
    invalid_wrapper = False
    try:
        return ContainerResult(
            output_schema_digest,
            output=output,
            object_reference=object_reference,
            status_code=wrapper.status_code,
            status_message=wrapper.status_message,
            error_result=error.model_dump(mode="json") if error is not None else None,
            error_schema_digest=error_schema_digest if error is not None else "",
        )
    except (TypeError, ValueError):
        invalid_wrapper = True
    if invalid_wrapper:
        raise ContainerInvocationError(
            FailureCode.ABI_VIOLATION,
            _INVALID_STEP_RESPONSE,
            outcome="unknown",
        )


def container_step_for_binding(
    binding: StepBinding,
    *,
    object_store: ResultObjectStore | None = None,
) -> ContainerStepHandler:
    """Wrap one typed step as a container handler.

    The handler validates the invocation's raw JSON inputs against the installed
    manifest schema for a custom-runner Step, invokes the public
    ``runtime.invoke()`` operation, and converts its typed output into a
    ``ContainerResult``. The wrapper's business result and status fields map
    onto the terminal ``Result`` envelope. The output schema digest is the
    canonical digest of the declared schema applied by ``runtime.invoke()``;
    that public operation also verifies any digest in the invocation context.

    A successful business result that exceeds the inline limit is externalized
    through ``object_store`` and carried as an ``object_reference``; without a
    store, an oversized result is a safe ABI violation.
    """

    validate_executable_step_binding(binding.descriptor, binding.step)
    output_schema_digest = _applied_output_schema_digest(binding)
    error_schema_digest = canonical_json_digest(_thaw_json_document(binding.descriptor.error_schema))

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        if invocation.step_revision_id != binding.descriptor.step_identity:
            raise ContainerInvocationError(
                FailureCode.NOT_FOUND,
                "The requested plugin step is not available in this container image.",
                outcome="not_started",
            )
        context = _execution_context(invocation)
        execution_failure: str | None = None
        try:
            wrapper = invoke(binding, invocation.inputs, context)
        except StepExecutionError as error:
            execution_failure = error.safe_message
        if execution_failure is not None:
            raise ContainerInvocationError(
                FailureCode.ABI_VIOLATION,
                execution_failure,
                outcome="unknown",
            )
        return _container_result(wrapper, output_schema_digest, error_schema_digest, object_store)

    return handler


def container_step_for_installed_manifest(
    step: BaseStep[Any, Any],
    *,
    installed_step: InstalledStepManifest,
    object_store: ResultObjectStore | None = None,
) -> ContainerStepHandler:
    """Wrap one custom-runner Step with its trusted installed identity and schema."""

    descriptor = StepDescriptor.for_installed_step(step, installed_step)
    return container_step_for_binding(StepBinding(descriptor, step), object_store=object_store)


def container_step_for_bindings(
    bindings: StepBindings,
    *,
    object_store: ResultObjectStore | None = None,
) -> ContainerStepHandler:
    """Wrap typed step bindings as a handler resolving by step identity.

    The invocation's ``step_revision_id`` is matched against the declared step
    identity. The current container bootstrap requires the Execution Plane to
    resolve an installed revision to that identity before sending the request.
    """

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        try:
            binding = bindings.binding(invocation.step_revision_id)
            return container_step_for_binding(binding, object_store=object_store)(invocation)
        except StepBindingError:
            message = "The requested plugin step is not available in this container image."
            raise ContainerInvocationError(
                FailureCode.NOT_FOUND,
                message,
                outcome="not_started",
            ) from None

    return handler


def step_registry_from_bindings(
    bindings: StepBindings,
    *,
    object_store: ResultObjectStore | None = None,
) -> StepRegistry:
    """Build a container step registry with one handler per typed step.

    Handlers are keyed by declared step identity and matched exactly against the
    invocation's ``step_revision_id``. The current container bootstrap requires
    the Execution Plane to resolve an installed revision to that identity
    before sending the request.
    """

    handlers = {
        step_identity: container_step_for_binding(
            bindings.binding(step_identity), object_store=object_store
        )
        for step_identity in bindings.step_identities
    }
    if not handlers:
        message = "at least one typed step must be registered"
        raise ValueError(message)
    return StepRegistry(handlers)
