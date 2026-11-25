"""Adapter that dispatches container invocations to typed Python steps."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable
from urllib.parse import unquote

from syntara_plugin.contracts import (
    CanonicalJsonError,
    build_validator,
    canonical_json_bytes,
    canonical_json_digest,
)

from . import _telemetry, invoke
from .abi import ContainerInvocationError
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
    ContainerResult,
    ContainerStepHandler,
    FailureCode,
    StepRegistry,
    _PartialResultReporter,
)
from .context import ExecutionContext
from .credential_callback import CallbackCredentialProvider
from .execution import StepExecutionError
from .outcome import StandardOutputWrapper, thaw_json
from .step import BaseStep

_REFERENCE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_INVALID_STEP_RESPONSE = "The step returned an invalid response."
# Business results above this canonical-JSON size cannot travel inline over the
# container ABI and are externalized to an object-store reference instead.
_MAX_INLINE_RESULT_BYTES = 1_048_576

_PARTIAL_SCHEMA_MAP_KEYWORDS = frozenset(
    {"$defs", "definitions", "dependentSchemas", "patternProperties", "properties"}
)
_PARTIAL_SCHEMA_LIST_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf"})
_PARTIAL_SCHEMA_SINGLE_KEYWORDS = frozenset(
    {"additionalProperties", "else", "propertyNames", "then", "unevaluatedProperties"}
)


def _is_partial_object_schema(
    schema: Mapping[str, Any],
    root_schema: Mapping[str, Any] | None = None,
    seen_references: frozenset[str] = frozenset(),
) -> bool:
    """Whether a schema node describes an object view that may be incomplete."""

    reference = schema.get("$ref")
    root = schema if root_schema is None else root_schema
    if isinstance(reference, str) and reference not in seen_references:
        target = _resolve_local_schema(root, reference)
        if isinstance(target, Mapping):
            return _is_partial_object_schema(target, root, seen_references | {reference})
    schema_type = schema.get("type")
    if schema_type == "object" or (
        isinstance(schema_type, list) and "object" in schema_type
    ):
        return True
    if any(
        keyword in schema
        for keyword in (
            "properties",
            "required",
            "additionalProperties",
            "patternProperties",
            "dependentRequired",
            "dependentSchemas",
            "minProperties",
            "maxProperties",
            "propertyNames",
            "unevaluatedProperties",
        )
    ):
        return True
    all_of = schema.get("allOf")
    if isinstance(all_of, list) and any(
        isinstance(branch, Mapping) and _is_partial_object_schema(branch, root, seen_references)
        for branch in all_of
    ):
        return True
    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list) and branches and all(
            isinstance(branch, Mapping)
            and _is_partial_object_schema(branch, root, seen_references)
            for branch in branches
        ):
            return True
    return False


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


def _resolve_local_schema(root_schema: Mapping[str, Any], reference: str) -> Any | None:
    """Resolve a local JSON Pointer reference against its output schema."""

    if reference == "#":
        return root_schema
    if not reference.startswith("#/"):
        return None
    current: Any = root_schema
    for raw_segment in unquote(reference[2:]).split("/"):
        segment = raw_segment.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or segment not in current:
            return None
        current = current[segment]
    return current


def _conditional_schema_members(
    schema: Any,
    root_schema: Mapping[str, Any],
    seen_references: frozenset[str] = frozenset(),
) -> set[str]:
    """Collect the object members a conditional schema uses at this level."""

    if not isinstance(schema, Mapping):
        return set()
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference not in seen_references:
        target = _resolve_local_schema(root_schema, reference)
        if isinstance(target, Mapping):
            return _conditional_schema_members(
                target, root_schema, seen_references | {reference}
            ) | _conditional_schema_members(
                {key: value for key, value in schema.items() if key != "$ref"},
                root_schema,
                seen_references,
            )
    members = {
        name for name in schema.get("required", []) if isinstance(name, str)
    }
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        members.update(name for name in properties if isinstance(name, str))
    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list):
            for branch in branches:
                members.update(
                    _conditional_schema_members(branch, root_schema, seen_references)
                )
    return members


def _conditional_property_schemas(
    schema: Any,
    root_schema: Mapping[str, Any],
    seen_references: frozenset[str] = frozenset(),
) -> dict[str, list[Any]]:
    """Collect same-level property declarations from conditional branches."""

    if not isinstance(schema, Mapping):
        return {}
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference not in seen_references:
        target = _resolve_local_schema(root_schema, reference)
        if isinstance(target, Mapping):
            properties = _conditional_property_schemas(
                target, root_schema, seen_references | {reference}
            )
            siblings = {
                key: value for key, value in schema.items() if key != "$ref"
            }
            for name, values in _conditional_property_schemas(
                siblings, root_schema, seen_references
            ).items():
                properties.setdefault(name, []).extend(values)
            return properties
    properties: dict[str, list[Any]] = {}
    declared = schema.get("properties")
    if isinstance(declared, Mapping):
        properties.update({name: [subschema] for name, subschema in declared.items()})
    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list):
            for branch in branches:
                for name, values in _conditional_property_schemas(
                    branch, root_schema, seen_references
                ).items():
                    properties.setdefault(name, []).extend(values)
    return properties


def _conditional_partial_guard(
    schema: Mapping[str, Any], root_schema: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Require a selector when a partial object supplies branch-specific fields."""

    condition = schema.get("if")
    then_schema = schema.get("then")
    else_schema = schema.get("else")
    if not isinstance(condition, Mapping) or not (
        isinstance(then_schema, Mapping) or isinstance(else_schema, Mapping)
    ):
        return None
    selectors = _conditional_schema_members(condition, root_schema)
    if not selectors:
        return None
    then_members = _conditional_schema_members(then_schema, root_schema)
    else_members = _conditional_schema_members(else_schema, root_schema)
    branch_members = then_members ^ else_members
    then_properties = _conditional_property_schemas(then_schema, root_schema)
    else_properties = _conditional_property_schemas(else_schema, root_schema)
    for name in then_members & else_members:
        if then_properties.get(name, []) != else_properties.get(name, []):
            branch_members.add(name)
    branch_members.difference_update(selectors)
    if not branch_members:
        return None
    branch_field_present = {
        "not": {
            "anyOf": [{"required": [name]} for name in sorted(branch_members)]
        }
    }
    selector_present = {"required": sorted(selectors)}
    return {"anyOf": [branch_field_present, selector_present]}


def _partial_schema(
    schema: Any,
    root_schema: Mapping[str, Any] | None = None,
    seen_references: frozenset[str] = frozenset(),
    partial_definitions: dict[str, Any] | None = None,
    partial_reference_names: dict[str, str] | None = None,
) -> Any:
    """Build a schema for partial objects, relaxing object completeness only."""

    if not isinstance(schema, Mapping):
        return schema
    root = root_schema or schema
    partial_defs = partial_definitions if partial_definitions is not None else {}
    partial_names = partial_reference_names if partial_reference_names is not None else {}
    reference = schema.get("$ref")
    if isinstance(reference, str):
        target = _resolve_local_schema(root, reference)
        if isinstance(target, Mapping) and reference in seen_references:
            partial_name = partial_names.get(reference)
            if partial_name is None:
                name_index = len(partial_names)
                partial_name = f"__syntara_partial_{name_index}"
                while partial_name in root.get("$defs", {}) or partial_name in partial_defs:
                    name_index += 1
                    partial_name = f"__syntara_partial_{name_index}"
                partial_names[reference] = partial_name
                partial_defs[partial_name] = {}
                partial_defs[partial_name] = _partial_schema(
                    target,
                    root,
                    seen_references,
                    partial_defs,
                    partial_names,
                )
            partial_reference = {"$ref": f"#/$defs/{partial_name}"}
            siblings = {
                keyword: value for keyword, value in schema.items() if keyword != "$ref"
            }
            if siblings:
                return {
                    "allOf": [
                        partial_reference,
                        _partial_schema(siblings, root, seen_references, partial_defs, partial_names),
                    ]
                }
            return partial_reference
        if isinstance(target, Mapping) and reference not in seen_references:
            expanded = _partial_schema(
                target,
                root,
                seen_references | {reference},
                partial_defs,
                partial_names,
            )
            siblings = {keyword: value for keyword, value in schema.items() if keyword != "$ref"}
            if siblings:
                return {
                    "allOf": [
                        expanded,
                        _partial_schema(siblings, root, seen_references, partial_defs, partial_names),
                    ]
                }
            return expanded
    partial: dict[str, Any] = {}
    for keyword, value in schema.items():
        if keyword in {"required", "minProperties", "dependentRequired"}:
            continue
        if keyword in {"$defs", "definitions"}:
            # Keep originals so array item references still enforce the full
            # item schema. Object-property references are expanded below.
            partial[keyword] = value
        elif keyword in _PARTIAL_SCHEMA_MAP_KEYWORDS and isinstance(value, Mapping):
            partial[keyword] = {
                name: _partial_schema(
                    subschema, root, seen_references, partial_defs, partial_names
                )
                for name, subschema in value.items()
            }
        elif keyword in _PARTIAL_SCHEMA_LIST_KEYWORDS and isinstance(value, list):
            transformed = [
                _partial_schema(
                    subschema, root, seen_references, partial_defs, partial_names
                )
                for subschema in value
            ]
            object_union = bool(transformed) and all(
                isinstance(original, Mapping)
                and isinstance(partial_branch, Mapping)
                and (
                    _is_partial_object_schema(original, root)
                    or _is_partial_object_schema(partial_branch, root)
                )
                for original, partial_branch in zip(value, transformed, strict=True)
            )
            if keyword == "oneOf" and (
                _is_partial_object_schema(schema, root) or object_union
            ):
                all_of = partial.setdefault("allOf", [])
                if isinstance(all_of, list):
                    all_of.append({"anyOf": transformed})
            elif keyword == "oneOf":
                partial[keyword] = transformed
            elif keyword == "allOf":
                all_of = partial.setdefault("allOf", [])
                if isinstance(all_of, list):
                    all_of.extend(transformed)
            else:
                partial[keyword] = transformed
        elif keyword in _PARTIAL_SCHEMA_SINGLE_KEYWORDS:
            partial[keyword] = _partial_schema(
                value, root, seen_references, partial_defs, partial_names
            )
        elif keyword in {"not", "if", "items", "prefixItems", "contains"}:
            # These schemas define exclusions, branch selection, or complete
            # array values. Applying object-subset rules inside them changes
            # their meaning, so preserve their original validation semantics.
            partial[keyword] = value
        else:
            partial[keyword] = value
    conditional_guard = _conditional_partial_guard(schema, root)
    if conditional_guard is not None:
        constraints = partial.setdefault("allOf", [])
        if isinstance(constraints, list):
            constraints.append(conditional_guard)
    return partial


def _partial_result_validator(
    output_schema: Mapping[str, Any],
) -> Callable[[Mapping[str, Any]], None]:
    """Build a validator that accepts schema-compatible partial object views."""

    source_schema = _thaw_json_document(output_schema)
    partial_definitions: dict[str, Any] = {}
    partial_schema = _partial_schema(
        source_schema,
        source_schema,
        partial_definitions=partial_definitions,
        partial_reference_names={},
    )
    for keyword in ("$defs", "definitions"):
        if keyword in source_schema:
            partial_schema.setdefault(keyword, source_schema[keyword])
    if partial_definitions:
        definitions = dict(partial_schema.get("$defs", {}))
        definitions.update(partial_definitions)
        partial_schema["$defs"] = definitions
    validator = build_validator(partial_schema)

    def validate(data: Mapping[str, Any]) -> None:
        try:
            valid = validator.is_valid(_thaw_json_document(data))
        except Exception:
            valid = False
        if not valid:
            raise ValueError("partial result is incompatible with the output schema")

    return validate


def _execution_context(
    invocation: ContainerInvocation,
) -> ExecutionContext:
    """Build the transport-independent context for one container invocation."""

    context = ExecutionContext(
        credential_provider=invocation.credential_provider,
        invocation_id=invocation.invocation_id,
        runtime_context=invocation.runtime_context,
        cancellation=invocation.cancellation,
        _partial_result_reporter=invocation._partial_result_reporter,
    )
    if isinstance(invocation.credential_provider, CallbackCredentialProvider):
        object.__setattr__(context, "_toolbox", invocation.credential_provider._public_toolbox)
    return context


def _applied_output_schema_digest(binding: StepBinding) -> str:
    """Return the digest of the output schema the executor actually applies.

    The digest is computed over the descriptor's declared output schema in the
    SDK's normative canonical JSON form, so equivalent schemas hash identically
    across language SDKs. Binding the digest to the applied schema keeps the
    terminal ``Result`` self-describing: a digest-resolving consumer reads the
    same schema the business result was validated against.
    """

    return canonical_json_digest(_thaw_json_document(binding.descriptor.output_schema))


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
        payload = canonical_json_bytes(thaw_json(result))
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
    applies to any business output, regardless of whether the step type deliberately
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
    step_revision_id: str | None = None,
) -> ContainerStepHandler:
    """Wrap one step type as a container handler.

    The handler validates the invocation's raw JSON inputs against the installed
    manifest schema for a custom-runner Step, invokes the public
    ``runtime.invoke()`` operation, and converts its typed output into a
    ``ContainerResult``. The wrapper's business result and status fields map
    onto the terminal ``Result`` envelope. The output schema digest is the
    canonical digest of the declared schema applied by ``runtime.invoke()``;
    that public operation also verifies any digest in the invocation context.

    A ``ContainerInvocationError`` raised from ``run()`` propagates unchanged
    so the container service reports it as a terminal failure with its code,
    message, retryability, and outcome preserved.

    A successful business result that exceeds the inline limit is externalized
    through ``object_store`` and carried as an ``object_reference``; without a
    store, an oversized result is a safe ABI violation.
    """

    validate_executable_step_binding(binding.descriptor, binding.step)
    output_schema_digest = _applied_output_schema_digest(binding)
    partial_validator = _partial_result_validator(binding.descriptor.output_schema)

    def execute(invocation: ContainerInvocation) -> ContainerResult:
        if invocation.step_revision_id != (step_revision_id or binding.descriptor.step_identity):
            raise ContainerInvocationError(
                FailureCode.NOT_FOUND,
                "The requested step type is not available in this container image.",
                outcome="not_started",
            )
        reporter = invocation._partial_result_reporter
        if isinstance(reporter, _PartialResultReporter):
            reporter.set_validator(partial_validator)
        context = _execution_context(invocation)
        try:
            wrapper = invoke(binding, invocation.inputs, context)
        except StepExecutionError as error:
            raise ContainerInvocationError(
                error.failure_code,
                error.safe_message,
                outcome=error.outcome,
            ) from error
        with _telemetry.measure_detail("container_result"):
            return _container_result(
                wrapper,
                output_schema_digest,
                wrapper.error_schema_digest or "",
                object_store,
            )

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        telemetry = _telemetry.runtime_telemetry
        if telemetry is None:
            return execute(invocation)
        with telemetry.measure("container_invocation") as measurement:
            result = execute(invocation)
            measurement.failed = result.status_code != 0
            return result

    return handler


def container_step_for_installed_manifest(
    step: BaseStep[Any, Any],
    *,
    installed_step: InstalledStepManifest,
    object_store: ResultObjectStore | None = None,
) -> ContainerStepHandler:
    """Wrap one custom-runner step type with its trusted installed identity and schema."""

    descriptor = StepDescriptor.for_installed_step(step, installed_step)
    return container_step_for_binding(StepBinding(descriptor, step), object_store=object_store)


def container_step_for_bindings(
    bindings: StepBindings,
    *,
    object_store: ResultObjectStore | None = None,
) -> ContainerStepHandler:
    """Wrap step type bindings as a handler resolving by step type identity.

    This helper uses declared identities and is intended for local invocation.
    Server startup uses resolved revisions with :func:`container_step_for_binding`.
    """

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        try:
            binding = bindings.binding(invocation.step_revision_id)
            return container_step_for_binding(binding, object_store=object_store)(invocation)
        except StepBindingError:
            message = "The requested step type is not available in this container image."
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
    """Build a container type registry with one handler per step type.

    This local helper uses declared identities. Server startup supplies exact
    resolved revision IDs separately from their installed descriptors.
    """

    handlers = {
        step_identity: container_step_for_binding(
            bindings.binding(step_identity), object_store=object_store
        )
        for step_identity in bindings.step_identities
    }
    if not handlers:
        message = "at least one step type must be registered"
        raise ValueError(message)
    return StepRegistry(handlers)
