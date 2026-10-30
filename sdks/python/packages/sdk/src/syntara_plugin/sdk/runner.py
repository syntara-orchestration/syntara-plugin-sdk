"""Local execution engine behind the ``syntara-plugin run`` command.

The runner resolves one custom-runner target from a compiled workspace,
validates supplied input against the compiled manifest schema, and executes
the Step through ``ContainerClient`` over real gRPC: source mode binds the
implementation and serves an in-process runtime; image mode connects to an
already-running workload runtime. The module returns data and raises errors
with author-facing guidance; the CLI renders both.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc
from jsonschema import Draft202012Validator, FormatChecker, ValidationError, validators
from syntara_plugin.contracts import canonical_json_bytes
from syntara_plugin.runtime.abi import CONTAINER_ABI, FailureCode
from syntara_plugin.runtime.adapter import container_step_for_binding
from syntara_plugin.runtime.bindings import StepBinding
from syntara_plugin.runtime.container import (
    ContainerClient,
    ContainerInvocation,
    ContainerInvocationError,
    ContainerProgress,
    ContainerRemoteError,
    ContainerResult,
    ContainerStepHandler,
    StepRegistry,
    add_container_runtime_service,
)
from syntara_plugin.runtime.contract_validation import SchemaDriftError
from syntara_plugin.runtime.execution import StepExecutionError
from syntara_plugin.testing import BoundedStepHandler, HarnessUsageError, StepBindingsFactory

from .authoring import CompilationResult
from .input_template import is_declared_secret, is_sensitive

ProgressCallback = Callable[[ContainerProgress], None]

DEFAULT_ADDRESS = "127.0.0.1:50051"
DEFAULT_TIMEOUT_SECONDS = 60


def is_runtime_address(address: str) -> bool:
    """Return whether one address is a bare ``HOST:PORT`` TCP endpoint.

    Mirrors the runtime server's listener rules: no scheme, a decimal port in
    range, and an IPv6 literal in brackets when colons appear in the host.
    """

    if "://" in address or not address.strip():
        return False
    if address.startswith("[") and address.count("]") == 1:
        host, port = address.rsplit(":", 1)
        host = host.removeprefix("[").removesuffix("]")
    elif address.count(":") == 1:
        host, port = address.split(":", 1)
    else:
        return False
    return bool(host) and port.isdecimal() and 1 <= int(port) <= 65_535


def runtime_address_host(address: str) -> str:
    """Return the host part of one ``HOST:PORT`` address."""

    if address.startswith("[") and address.count("]") == 1:
        return address.rsplit(":", 1)[0].removeprefix("[").removesuffix("]")
    if address.count(":") == 1:
        return address.split(":", 1)[0]
    return ""


def is_loopback_address(address: str) -> bool:
    """Return whether one ``HOST:PORT`` address points at this machine's loopback."""

    return runtime_address_host(address).casefold() in {"127.0.0.1", "localhost", "::1"}
_HEALTH_TIMEOUT_SECONDS = 10.0
_MAX_INPUT_BYTES = 1_048_576
_MAX_VALIDATION_ERRORS = 10
_MAX_VALIDATION_LINE_LENGTH = 300
# A traceback with an enormous exception message must not flood the output.
_MAX_ERROR_DETAIL_LENGTH = 16_384
_RUNNABLE_KINDS = frozenset({"Action", "Control", "Trigger"})
_KIND_COLUMN_WIDTH = 9
_VALIDATE_NEXT_STEP = "syntara-plugin validate"
# Aggregate JSON Schema validators embed the whole instance (which may hold
# any field's value, sensitive or not) in their message, so their errors are
# always reported as a sanitized constraint line.
_INSTANCE_EMBEDDING_VALIDATORS = frozenset(
    {"anyOf", "contains", "dependencies", "dependentRequired", "dependentSchemas", "enum",
     "maxProperties", "minProperties", "not", "oneOf", "uniqueItems"}
)
# The container ABI's approved secret mount: the only directory a deployed
# workload ever reads a credential from, and the path image mode mounts into.
CREDENTIAL_MOUNT_DIRECTORY = "/var/run/syntara/secrets"
_CREDENTIAL_SOURCE_STEPS = (
    "--credential NAME=- to read one value from standard input",
    "--credential NAME=file:PATH to read a file outside the workspace",
    "--credential NAME=env:VAR to read an environment variable",
)


class RunUsageError(ValueError):
    """A usage failure that stops before any invocation (exit status 2)."""

    def __init__(
        self,
        message: str,
        *,
        detail: Sequence[str] = (),
        next_steps: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail = tuple(detail)
        self.next_steps = tuple(next_steps)


class RunExecutionError(RuntimeError):
    """A transport or protocol failure during execution (exit status 1)."""

    def __init__(
        self,
        message: str,
        *,
        failure_code: FailureCode | None = None,
        detail: Sequence[str] = (),
        next_steps: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.message = message
        self.failure_code = failure_code
        self.detail = tuple(detail)
        self.next_steps = tuple(next_steps)


@dataclass(frozen=True)
class RunTarget:
    """One resolved, locally runnable custom-runner target."""

    identity: str
    kind: str
    entry_point: str
    metadata_name: str
    input_schema: Mapping[str, Any]
    schema_digests: Mapping[str, str]
    workload_image: str | None = None
    step_revision_id: str = ""
    integration_type: str | None = None


@dataclass(frozen=True)
class RunErrorDetail:
    """The structured failure fields of one terminal outcome."""

    code: str
    message: str
    retryable: bool


@dataclass(frozen=True)
class RunReport:
    """One completed invocation, successful or deliberately failed.

    A terminal outcome with a nonzero status or a workload failure event is a
    valid completion: ``ok`` is false but the transport succeeded. Transport
    and protocol failures raise :class:`RunExecutionError` instead.
    """

    mode: str
    target: str
    input_source: str
    input_values: Mapping[str, Any]
    ok: bool
    invocation_id: str
    workflow_run_id: str
    status: int | None
    status_message: str
    output: Mapping[str, Any] | None
    object_reference: str | None
    output_schema_digest: str | None
    error: RunErrorDetail | None
    outcome: str | None
    error_detail: str | None = None
    image: str | None = None
    # The names of the credentials delivered to this invocation. Never a value
    # and never a path: the report is one of the surfaces secrets must not reach.
    credentials: tuple[str, ...] = ()


@dataclass(frozen=True)
class TlsOptions:
    """The mutual-TLS client material required by non-loopback runtimes."""

    ca: Path
    certificate: Path
    key: Path


_REDACTED = "***"
# Name-based matches collide with legitimate text too often below this length.
_MIN_SECRET_LENGTH = 4
# A schema-declared secret (writeOnly, format: password) is an explicit
# contract, so shorter values are still collected.
# A schema-declared secret is collected whatever its length; only
# heuristic name detection keeps a false-positive threshold.
_MIN_DECLARED_SECRET_LENGTH = 1


def _declared_secret_marker(
    _validator: Any, value: Any, _instance: Any, _schema: Any
) -> Iterator[ValidationError]:
    """Yield one marker error wherever a schema declares ``writeOnly: true``."""

    if value is True:
        yield ValidationError("declared secret")


def _password_format_marker(
    _validator: Any, value: Any, _instance: Any, _schema: Any
) -> Iterator[ValidationError]:
    """Yield one marker error wherever a schema declares ``format: password``."""

    if value == "password":
        yield ValidationError("declared secret")


# One marker pass locates every supplied value whose governing schema declares
# a secret, following the validator's own traversal: $ref, additionalProperties,
# patternProperties, prefixItems, unevaluatedProperties, and composition are
# resolved exactly as validation resolves them. Marker errors are told apart
# from real validation output by their validator names.
_SECRET_MARKER_VALIDATOR = validators.extend(
    Draft202012Validator,
    {"writeOnly": _declared_secret_marker, "format": _password_format_marker},
)


def _declared_secret_paths(
    input_schema: Mapping[str, Any], instance: Any
) -> tuple[tuple[Any, ...], ...]:
    """Return the paths of supplied values a schema declaration marks secret."""

    try:
        marker = _SECRET_MARKER_VALIDATOR(input_schema)
        return tuple(
            path for error in marker.iter_errors(instance) for path in _marker_paths(error)
        )
    except Exception:  # noqa: BLE001 - an unusable schema yields no paths
        return ()


def _marker_paths(error: Any) -> Iterator[tuple[Any, ...]]:
    """Yield the paths one marker error, or its composition branches, marks.

    Inside ``anyOf``/``oneOf``/``allOf`` jsonschema reports a single error for
    the composition keyword and nests the branch errors in ``context``, so a
    top-level scan alone would miss declarations inside a branch.
    """

    if _is_secret_marker(error):
        yield tuple(error.absolute_path)
    for branch in error.context or ():
        yield from _marker_paths(branch)


def _is_secret_marker(error: Any) -> bool:
    """Return whether one validation error is a declared-secret marker."""

    return error.validator == "writeOnly" or (
        error.validator == "format" and error.validator_value == "password"
    )


def redact_inputs(input_schema: Mapping[str, Any], inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Copy the supplied inputs with credential-like fields masked.

    A field the schema marks sensitive (``writeOnly``, ``format: password``)
    or names like a credential masks its whole supplied value, nested objects
    included. Secrets the schema declares through ``$ref`` or on
    ``additionalProperties`` / ``patternProperties`` maps are masked at their
    resolved locations. A declaration on the schema root marks the entire
    input secret: every top-level value is masked wholesale. Everything else
    is echoed back unchanged so the author can see exactly what executed.
    The supplied mapping is never mutated.
    """

    # Masking must never write into the structure the invocation references.
    redacted = _redact_object(input_schema, copy.deepcopy(inputs))
    paths = _declared_secret_paths(input_schema, inputs)
    if any(path == () for path in paths):
        return dict.fromkeys(redacted, _REDACTED)
    for path in paths:
        _mask_path(redacted, path)
    return redacted


def _secret_values(
    input_schema: Mapping[str, Any], inputs: Mapping[str, Any]
) -> tuple[str, ...]:
    """Collect the supplied values of sensitive fields for display scrubbing.

    Field-level masking keeps secrets out of the input echo; this collects the
    raw values so anything a step interpolates into an exception message, a
    failure message, its output, or a progress update can be scrubbed too.
    Values nested under a sensitive name are collected recursively. String and
    integer values are collected; booleans and floats collide with legitimate
    output too often to scrub reliably. Schema-declared secrets are located
    through the validator's own traversal, so declarations reached through
    ``$ref`` or placed on ``additionalProperties`` / ``patternProperties``
    maps are collected too.
    """

    candidates: list[tuple[str, int]] = []
    _collect_secret_values(input_schema, inputs, candidates)
    for path in _declared_secret_paths(input_schema, inputs):
        _collect_nested_strings(_value_at(inputs, path), candidates, _MIN_DECLARED_SECRET_LENGTH)
    picked: list[str] = []
    for value, min_length in candidates:
        if len(value) >= min_length and value not in picked:
            picked.append(value)
    return tuple(picked)


def _collect_secret_values(
    schema: Mapping[str, Any], value: Any, candidates: list[tuple[str, int]]
) -> None:
    if isinstance(value, Mapping):
        properties = _declared_properties(schema)
        for name, item in value.items():
            _collect_property_value(name, item, properties, candidates)
    elif isinstance(value, list):
        _collect_list_values(schema.get("items"), value, candidates)


def _declared_properties(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the schema's declared properties, or an empty mapping."""

    properties = schema.get("properties", {})
    return properties if isinstance(properties, Mapping) else {}


def _collect_property_value(
    name: Any,
    item: Any,
    properties: Mapping[str, Any],
    candidates: list[tuple[str, int]],
) -> None:
    """Collect one supplied property's value following its declaration."""

    if not isinstance(name, str):
        _collect_unschemaed(item, candidates)
        return
    property_schema = properties.get(name, {})
    if is_sensitive(name, property_schema):
        min_length = (
            _MIN_DECLARED_SECRET_LENGTH
            if is_declared_secret(property_schema)
            else _MIN_SECRET_LENGTH
        )
        _collect_nested_strings(item, candidates, min_length)
    elif isinstance(property_schema, Mapping):
        _collect_secret_values(property_schema, item, candidates)
    else:
        _collect_unschemaed(item, candidates)


def _collect_list_values(
    items_schema: Any, values: list[Any], candidates: list[tuple[str, int]]
) -> None:
    """Collect every supplied list item following the declared items schema."""

    if isinstance(items_schema, Mapping) and is_declared_secret(items_schema):
        for item in values:
            _collect_nested_strings(item, candidates, _MIN_DECLARED_SECRET_LENGTH)
    elif isinstance(items_schema, Mapping):
        for item in values:
            _collect_secret_values(items_schema, item, candidates)


def _collect_unschemaed(value: Any, candidates: list[tuple[str, int]]) -> None:
    """Collect secrets under credential-like keys the schema does not declare."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and is_sensitive(key, {}):
                _collect_nested_strings(item, candidates, _MIN_SECRET_LENGTH)
            else:
                _collect_unschemaed(item, candidates)
    elif isinstance(value, list):
        for item in value:
            _collect_unschemaed(item, candidates)


def _collect_nested_strings(
    value: Any, candidates: list[tuple[str, int]], min_length: int
) -> None:
    """Collect every scrubable string under one sensitive-named structure."""

    if isinstance(value, str):
        candidates.append((value, min_length))
    elif isinstance(value, bool):
        return
    elif isinstance(value, int):
        candidates.append((str(value), min_length))
    elif isinstance(value, Mapping):
        for item in value.values():
            _collect_nested_strings(item, candidates, min_length)
    elif isinstance(value, list):
        for item in value:
            _collect_nested_strings(item, candidates, min_length)


def _scrub_text(text: str, secrets: tuple[str, ...]) -> str:
    # Longest first so overlapping secrets cannot leave a suffix behind. Every
    # secret matches case-insensitively so a step cannot dodge the scrubber by
    # changing the case of the value.
    for secret in sorted(secrets, key=len, reverse=True):
        text = re.sub(re.escape(secret), _REDACTED, text, flags=re.IGNORECASE)
    return text


def _scrub_structure(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return _scrub_text(value, secrets)
    if isinstance(value, Mapping):
        return {
            (_scrub_text(key, secrets) if isinstance(key, str) else key): _scrub_structure(
                item, secrets
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub_structure(item, secrets) for item in value]
    return value


def _redact_object(schema: Mapping[str, Any], value: Mapping[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        properties = {}
    redacted: dict[str, Any] = {}
    for name, item in value.items():
        property_schema = properties.get(name) if isinstance(name, str) else None
        if isinstance(name, str) and is_sensitive(name, properties.get(name, {})):
            redacted[name] = _REDACTED
        elif isinstance(property_schema, Mapping):
            redacted[name] = _redact_value(property_schema, item)
        else:
            # Undeclared keys still get name-checked recursively: the schema
            # cannot vouch for them, so a credential-like name is masked.
            redacted[name] = _redact_unschemaed(item)
    return redacted


def _redact_unschemaed(value: Any) -> Any:
    """Redact credential-like keys inside structures the schema does not declare."""

    if isinstance(value, Mapping):
        return {
            key: (
                _REDACTED
                if isinstance(key, str) and is_sensitive(key, {})
                else _redact_unschemaed(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_unschemaed(item) for item in value]
    return value


def _redact_value(schema: Mapping[str, Any], value: Any) -> Any:
    if isinstance(value, Mapping):
        return _redact_object(schema, value)
    if isinstance(value, list):
        items_schema = schema.get("items")
        if isinstance(items_schema, Mapping) and is_declared_secret(items_schema):
            return [_REDACTED] * len(value)
        if isinstance(items_schema, Mapping):
            return [_redact_value(items_schema, item) for item in value]
    return value


def _value_at(value: Any, path: tuple[Any, ...]) -> Any:
    """Return the value at one marker path, or None when it is absent."""

    for item in path:
        if isinstance(item, int):
            if not isinstance(value, list) or not 0 <= item < len(value):
                return None
            value = value[item]
        elif isinstance(value, dict) and item in value:
            value = value[item]
        else:
            return None
    return value


def _mask_path(container: Any, path: tuple[Any, ...]) -> None:
    """Mask the value at one marker path inside an already-copied structure."""

    if not path:
        return
    for item in path[:-1]:
        stepped = _step_into(container, item)
        if stepped is _ABSENT:
            return
        container = stepped
    _mask_leaf(container, path[-1])


_ABSENT = object()


def _step_into(container: Any, item: Any) -> Any:
    """Descend one path segment, or ``_ABSENT`` when it does not exist."""

    if isinstance(item, int):
        if isinstance(container, list) and 0 <= item < len(container):
            return container[item]
        return _ABSENT
    if isinstance(container, dict) and item in container:
        return container[item]
    return _ABSENT


def _mask_leaf(container: Any, leaf: Any) -> None:
    """Mask the leaf of one marker path when it exists."""

    if isinstance(leaf, int):
        if isinstance(container, list) and 0 <= leaf < len(container):
            container[leaf] = _REDACTED
    elif isinstance(container, dict):
        container[leaf] = _REDACTED


_DEFAULT_MANIFEST_NAME = "plugin.yaml"


def resolve_manifest(manifest: Path | None) -> Path:
    """Resolve the run command's manifest path, rejecting missing workspaces.

    The default is ``./plugin.yaml`` in the invocation directory; there is no
    upward search, so the command never selects a workspace the author did
    not name. Any path the author names is accepted, including parent-
    relative ones, exactly like ``validate`` and ``inspect``.
    """

    if manifest is None:
        candidate = Path(_DEFAULT_MANIFEST_NAME)
    else:
        candidate = manifest.expanduser()
    if candidate.is_dir():
        candidate = candidate / _DEFAULT_MANIFEST_NAME
    # Name the directory, never the file: for the bare default this reads
    # "no plugin.yaml in /current/directory" instead of "in plugin.yaml".
    location = str(candidate.resolve().parent)
    if not candidate.is_file():
        if manifest is not None and str(manifest) != _DEFAULT_MANIFEST_NAME:
            raise RunUsageError(
                f"plugin manifest not found: {candidate}",
                next_steps=("check the --manifest path",),
            )
        raise RunUsageError(
            f"no plugin.yaml in {location}",
            next_steps=(
                "run this command from a plugin workspace",
                "or pass --manifest PATH",
            ),
        )
    return candidate


def resolve_run_target(result: CompilationResult, requested: str | None) -> RunTarget:
    """Resolve the runnable target named by ``requested``, or infer the only one.

    Raises :class:`RunUsageError` with listing guidance when the workspace has
    no runnable target, several equally valid matches, or the request names a
    declarative platform-runner target.
    """

    targets = list(result.descriptor["spec"]["targets"])
    runnable = [target for target in targets if _is_runnable(target)]
    if requested is None:
        if not runnable:
            raise RunUsageError(
                "this workspace declares no custom-runner Step target that can run locally",
                detail=_target_lines(result.descriptor["spec"]["targets"]),
                next_steps=("run syntara-plugin inspect to review the declared targets",),
            )
        if len(runnable) > 1:
            raise RunUsageError(
                "this workspace declares more than one runnable target",
                detail=_target_lines(runnable),
                next_steps=(f"syntara-plugin run {runnable[0]['identity']}",),
            )
        selected: Mapping[str, Any] = runnable[0]
    else:
        selected = _select_requested(targets, runnable, requested)
    return RunTarget(
        identity=str(selected["identity"]),
        kind=str(selected["kind"]),
        entry_point=str(selected["runtime"]["entryPoint"]),
        metadata_name=str(selected["metadata"]["name"]),
        input_schema=selected["input"],
        schema_digests=selected["schemaDigests"],
        workload_image=selected.get("runtime", {}).get("workload"),
        step_revision_id=str(selected["step_revision_id"]),
        integration_type=selected.get("integrationType"),
    )


def load_input(inline: str | None, input_file: Path | None) -> tuple[dict[str, Any], str]:
    """Load the invocation input from ``--input`` or ``--input-file``.

    Returns the parsed object and a display label for the input source. The
    JSON constants ``NaN`` and ``Infinity`` are rejected so an accepted input
    is always encodable by the container ABI, and the encoded payload must
    stay within the ABI's 1 MiB bound.
    """

    if inline is not None and input_file is not None:
        raise RunUsageError("--input and --input-file are mutually exclusive")
    if inline is not None:
        raw, source = inline, "--input"
    elif input_file is not None:
        raw, source = _read_input_source(input_file)
    else:
        raise RunUsageError("load_input requires --input or --input-file")
    value = _parse_input_json(raw, source)
    _require_input_size(value, source)
    return value, source


def _read_input_source(input_file: Path) -> tuple[str, str]:
    """Read the raw input and its display label from a file or stdin.

    Files are read as UTF-8 with a byte-order mark tolerated; undecodable
    bytes, a closed stdin, and any other read failure become one usage error
    instead of a traceback.
    """

    reading_stdin = str(input_file) == "-"
    if reading_stdin:
        try:
            return sys.stdin.read(), "stdin"
        except (OSError, ValueError) as error:
            raise RunUsageError(f"could not read the input from stdin: {error}") from error
    try:
        # utf-8-sig strips a leading byte-order mark, which authors hit when
        # their editor writes one; the ABI expects plain UTF-8 JSON.
        raw = input_file.read_text(  # NOSONAR - the author's own --input-file path, accepted like validate/inspect/build
            encoding="utf-8-sig"
        )
    except OSError as error:
        raise RunUsageError(f"could not read the input file {input_file}: {error}") from error
    except UnicodeDecodeError as error:
        raise RunUsageError(
            f"the input file {input_file} is not valid UTF-8: {error}",
            next_steps=("save the file as UTF-8 and rerun",),
        ) from error
    return raw, str(input_file)


def _parse_input_json(raw: str, source: str) -> dict[str, Any]:
    """Parse the raw input as one JSON object, rejecting non-finite constants."""

    try:
        value = json.loads(raw, parse_constant=_reject_json_constant)
    except (ValueError, RecursionError) as error:
        raise RunUsageError(f"the input from {source} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise RunUsageError(
            f"the input from {source} must be a JSON object",
            next_steps=('wrap the value in an object, for example {"value": ...}',),
        )
    return value


def _require_input_size(value: dict[str, Any], source: str) -> None:
    """Reject inputs whose encoded form exceeds the container ABI's bound."""

    try:
        # The unescaped form is what every display surface prints, so lone
        # surrogates must be rejected here rather than crashing the report.
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"), ensure_ascii=False)
        size = len(encoded.encode("utf-8"))
    except (ValueError, RecursionError) as error:
        raise RunUsageError(
            f"the input from {source} must contain only UTF-8 encodable values: {error}",
            next_steps=("remove lone surrogate characters and rerun",),
        ) from error
    if size > _MAX_INPUT_BYTES:
        raise RunUsageError(
            "the input is too large for the container ABI "
            f"({size} encoded bytes, limit {_MAX_INPUT_BYTES})",
            next_steps=("reduce the payload",),
        )


def validate_inputs(
    input_schema: Mapping[str, Any], inputs: Mapping[str, Any]
) -> list[str]:
    """Validate input against the compiled schema, returning guidance lines.

    Every error is reported as one ``inputs.<pointer>: message`` line. Values
    of sensitive fields are never echoed: the line names the field and the
    violated constraint only, and a secret value pasted into a non-sensitive
    field is scrubbed from the message before it is printed.
    """

    secrets = _secret_values(input_schema, inputs)
    secret_paths = _declared_secret_paths(input_schema, inputs)
    errors = _sorted_schema_errors(input_schema, inputs)
    lines = [
        line
        for error in errors
        for line in _validation_lines_for_error(
            error, properties=_declared_properties(input_schema), secrets=secrets,
            secret_paths=secret_paths,
        )
    ]
    return _capped_validation_lines(lines)


def _sorted_schema_errors(
    input_schema: Mapping[str, Any], inputs: Mapping[str, Any]
) -> list[Any]:
    """Return the schema's errors in a stable order."""

    try:
        validator = Draft202012Validator(input_schema, format_checker=FormatChecker())
        # Sorting by the rendered pointer avoids comparing mixed-type path
        # elements (string keys against array indices) across errors.
        return sorted(
            validator.iter_errors(inputs),
            key=lambda error: (_pointer(error.path), str(error.validator)),
        )
    except Exception as error:
        raise RunUsageError("the declared input schema could not be evaluated") from error


def _validation_lines_for_error(
    error: Any,
    *,
    properties: Mapping[str, Any],
    secrets: tuple[str, ...],
    secret_paths: Sequence[tuple[Any, ...]],
) -> list[str]:
    """Render one schema error as one or more guidance lines."""

    if error.validator == "required" and isinstance(error.validator_value, list):
        instance = error.instance if isinstance(error.instance, dict) else {}
        missing = [name for name in error.validator_value if name not in instance]
        return [
            f"inputs{_join_pointer(error.path, str(name))}: required property is missing"
            for name in missing
        ]
    pointer = _pointer(error.path)
    if str(error.validator) in _INSTANCE_EMBEDDING_VALIDATORS or _sensitive_error(
        properties, error.path, secret_paths
    ):
        return [f"inputs{pointer}: {error.validator} constraint is not satisfied"]
    return [_scrub_text(_bounded(f"inputs{pointer}: {error.message}"), secrets)]


def _capped_validation_lines(lines: list[str]) -> list[str]:
    """Bound the number of reported validation errors."""

    if len(lines) <= _MAX_VALIDATION_ERRORS:
        return lines
    remaining = len(lines) - _MAX_VALIDATION_ERRORS
    return lines[:_MAX_VALIDATION_ERRORS] + [f"and {remaining} more"]


@dataclass(frozen=True)
class CredentialSpec:
    """One parsed ``--credential NAME=SOURCE`` request, before it is read."""

    name: str
    kind: str
    locator: str


@dataclass(frozen=True)
class CredentialRecipe:
    """One credential recipe the selected target can reach."""

    name: str
    schema: Mapping[str, Any]
    secret_fields: tuple[str, ...]


@dataclass(frozen=True)
class LoadedCredential:
    """One supplied credential, shaped and validated, held in memory only."""

    name: str
    document: Mapping[str, Any]
    secret_values: tuple[str, ...]


def parse_credential_options(raw: Sequence[str] | None) -> tuple[CredentialSpec, ...]:
    """Parse every ``--credential NAME=SOURCE`` option into a source request.

    An inline value is refused rather than read: it would be visible in the
    shell history and in other local users' view of ``/proc/<pid>/cmdline``,
    the same reason ``--input-file`` and ``--password-stdin`` exist. No
    message here echoes the supplied text, because a refused inline value is
    exactly the case where that text is a secret.
    """

    specs: list[CredentialSpec] = []
    seen: set[str] = set()
    for item in raw or ():
        name, separator, source = item.partition("=")
        if not separator or not name:
            raise RunUsageError(
                "--credential expects NAME=SOURCE",
                next_steps=_CREDENTIAL_SOURCE_STEPS,
            )
        if name in seen:
            raise RunUsageError(f"--credential {name} was supplied more than once")
        seen.add(name)
        specs.append(_credential_spec(name, source))
    stdin_names = [spec.name for spec in specs if spec.kind == "stdin"]
    if len(stdin_names) > 1:
        raise RunUsageError(
            "only one credential can be read from standard input",
            detail=tuple(f"{name}=-" for name in stdin_names),
            next_steps=("supply the others with file: or env:",),
        )
    return tuple(specs)


def _credential_spec(name: str, source: str) -> CredentialSpec:
    """Classify one credential source, refusing an inline value."""

    if source == "-":
        return CredentialSpec(name, "stdin", "")
    for prefix, kind in (("file:", "file"), ("env:", "env")):
        if source.startswith(prefix):
            locator = source[len(prefix):]
            if not locator:
                raise RunUsageError(f"--credential {name}={prefix} names no {kind}")
            return CredentialSpec(name, kind, locator)
    raise RunUsageError(
        f"--credential {name} does not accept an inline value",
        detail=(
            "An inline value is visible in the shell history and to other",
            "local users through /proc/<pid>/cmdline.",
        ),
        next_steps=_CREDENTIAL_SOURCE_STEPS,
    )


def reachable_credential_recipes(
    result: CompilationResult, target: RunTarget
) -> dict[str, CredentialRecipe]:
    """Return the credential recipes reachable through the target's integration.

    A Step reaches a recipe only through the integration type it declares, so
    the lookup is integration -> ``credentialTypes`` -> recipe. The compiler
    has already rejected a broken link in either direction, so an empty result
    means the target declares no integration type, not a malformed workspace.
    """

    if not target.integration_type:
        return {}
    targets = list(result.descriptor["spec"]["targets"])
    declared: set[str] = set()
    for candidate in targets:
        if candidate.get("kind") == "IntegrationType" and (
            candidate.get("id") == target.integration_type
        ):
            declared = {str(name) for name in candidate.get("credentialTypes", ())}
    recipes: dict[str, CredentialRecipe] = {}
    for candidate in targets:
        identifier = str(candidate.get("id", ""))
        if candidate.get("kind") != "CredentialRecipe" or identifier not in declared:
            continue
        schema = candidate.get("credentialSchema") or {}
        recipes[identifier] = CredentialRecipe(
            name=identifier,
            schema=schema if isinstance(schema, Mapping) else {},
            secret_fields=tuple(str(field) for field in candidate.get("secretFields", ())),
        )
    return recipes


def load_credentials(
    specs: Sequence[CredentialSpec],
    recipes: Mapping[str, CredentialRecipe],
    target: RunTarget,
    *,
    workspace: Path,
) -> tuple[tuple[LoadedCredential, ...], tuple[str, ...]]:
    """Read, shape, and validate every supplied credential before execution.

    Returns the loaded credentials and any warnings for the CLI to render.
    Values are held in memory: nothing is written until the delivery file is
    created for the invocation itself.
    """

    loaded: list[LoadedCredential] = []
    warnings: list[str] = []
    for spec in specs:
        recipe = _credential_recipe_for(spec, recipes, target)
        raw, source_warnings = _read_credential_source(spec, workspace)
        warnings.extend(source_warnings)
        document = _credential_document(spec.name, raw, recipe)
        errors = _credential_validation_lines(recipe, document)
        if errors:
            raise RunUsageError(
                f"the value supplied for {spec.name} does not satisfy its credential schema",
                detail=tuple(errors),
                next_steps=(f"correct the value and rerun --credential {spec.name}=...",),
            )
        loaded.append(
            LoadedCredential(
                name=spec.name,
                document=document,
                secret_values=_credential_secret_values(recipe, document, raw),
            )
        )
    return tuple(loaded), tuple(warnings)


def _credential_recipe_for(
    spec: CredentialSpec, recipes: Mapping[str, CredentialRecipe], target: RunTarget
) -> CredentialRecipe:
    """Resolve one credential name to a recipe the target can reach."""

    recipe = recipes.get(spec.name)
    if recipe is not None:
        return recipe
    if not target.integration_type:
        raise RunUsageError(
            f"{target.identity} declares no integrationType, so it reaches no credential",
            next_steps=("add spec.integrationType to the target, or drop --credential",),
        )
    reachable = ", ".join(sorted(recipes)) or "none"
    raise RunUsageError(
        f"{spec.name} is not a credential recipe reachable from {target.identity}",
        detail=(f"Reachable through {target.integration_type}: {reachable}",),
        next_steps=("declare the recipe in this workspace, or name a reachable one",),
    )


def _read_credential_source(spec: CredentialSpec, workspace: Path) -> tuple[str, tuple[str, ...]]:
    """Read one credential value from stdin, a file, or the environment."""

    if spec.kind == "stdin":
        return _read_credential_stdin(spec), ()
    if spec.kind == "env":
        value = os.environ.get(spec.locator)
        if value is None:
            raise RunUsageError(f"the environment variable {spec.locator} is not set")
        if not value.strip():
            raise RunUsageError(f"the environment variable {spec.locator} is empty")
        return value.rstrip("\r\n"), ()
    return _read_credential_file(spec, workspace)


def _read_credential_stdin(spec: CredentialSpec) -> str:
    """Read one credential value from standard input, as --password-stdin does."""

    try:
        value = sys.stdin.read()
    except (OSError, UnicodeDecodeError) as error:
        raise RunUsageError(f"--credential {spec.name}=- could not read standard input") from error
    if not value.strip():
        raise RunUsageError(f"--credential {spec.name}=- received no value")
    return value.rstrip("\r\n")


def _read_credential_file(spec: CredentialSpec, workspace: Path) -> tuple[str, tuple[str, ...]]:
    """Read one credential value from a file outside the plugin workspace."""

    path = Path(spec.locator).expanduser()
    resolved = path.resolve()
    if resolved.is_relative_to(workspace.resolve()):
        raise RunUsageError(
            f"the credential file for {spec.name} is inside the plugin workspace",
            detail=("A secret inside the workspace can reach the artifact or version control.",),
            next_steps=("move the file outside the workspace and rerun",),
        )
    try:
        # The author named this path themselves; it is read, never executed.
        value = resolved.read_text(encoding="utf-8-sig")  # NOSONAR
    except (OSError, UnicodeDecodeError) as error:
        raise RunUsageError(f"the credential file {path} could not be read") from error
    if not value.strip():
        raise RunUsageError(f"the credential file {path} is empty")
    warnings: list[str] = []
    if _is_group_or_world_readable(resolved):
        warnings.append(f"warning: {path} is readable by other local users")
    return value.rstrip("\r\n"), tuple(warnings)


def _is_group_or_world_readable(path: Path) -> bool:
    """Whether a credential file grants read access beyond its owner."""

    try:
        return bool(path.stat().st_mode & 0o044)
    except OSError:
        return False


def _credential_document(
    name: str, raw: str, recipe: CredentialRecipe
) -> Mapping[str, Any]:
    """Shape one supplied value into the recipe's declared document.

    A JSON object is taken as the complete document. Any other text is a raw
    value, wrapped into the single field the recipe declares; a recipe with
    several fields has no unambiguous wrapping, so the full document is
    required instead.
    """

    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if isinstance(parsed, Mapping):
        return parsed
    field_name = _sole_credential_field(recipe)
    if field_name is not None:
        return {field_name: raw}
    declared = ", ".join(sorted(_declared_properties(recipe.schema))) or "none"
    raise RunUsageError(
        f"{name} declares more than one field, so a raw value cannot be wrapped",
        detail=(f"Declared fields: {declared}",),
        next_steps=(f"supply the complete JSON document for {name}",),
    )


def _sole_credential_field(recipe: CredentialRecipe) -> str | None:
    """Return the only field a raw value can be wrapped into, when there is one."""

    required = recipe.schema.get("required")
    if isinstance(required, Sequence) and not isinstance(required, str) and len(required) == 1:
        return str(required[0])
    properties = _declared_properties(recipe.schema)
    if len(properties) == 1:
        return str(next(iter(properties)))
    return None


def _credential_validation_lines(
    recipe: CredentialRecipe, document: Mapping[str, Any]
) -> list[str]:
    """Validate one credential document, naming fields and never values.

    Every field of a credential is secret by construction, so no line here
    carries the instance: a violated constraint is reported by name only.
    """

    try:
        validator = Draft202012Validator(recipe.schema, format_checker=FormatChecker())
        errors = sorted(
            validator.iter_errors(document),
            key=lambda error: (_pointer(error.path), str(error.validator)),
        )
    except Exception as error:
        raise RunUsageError(
            f"the credential schema declared for {recipe.name} could not be evaluated"
        ) from error
    lines: list[str] = []
    for error in errors:
        if error.validator == "required" and isinstance(error.validator_value, list):
            instance = error.instance if isinstance(error.instance, dict) else {}
            lines.extend(
                f"{recipe.name}{_join_pointer(error.path, str(name))}: "
                "required property is missing"
                for name in error.validator_value
                if name not in instance
            )
            continue
        lines.append(
            f"{recipe.name}{_pointer(error.path)}: "
            f"{error.validator} constraint is not satisfied"
        )
    return _capped_validation_lines(lines)


def _credential_secret_values(
    recipe: CredentialRecipe, document: Mapping[str, Any], raw: str
) -> tuple[str, ...]:
    """Collect everything about one credential that must never be displayed.

    The supplied text, the delivered document, and the values of the recipe's
    declared ``secretFields`` all join the run's scrub set, so a step that
    interpolates any of them into output, an error, or a traceback has it
    masked before anything is printed.
    """

    candidates: list[tuple[str, int]] = [(raw, _MIN_DECLARED_SECRET_LENGTH)]
    for field_name in recipe.secret_fields:
        if field_name in document:
            _collect_nested_strings(
                document[field_name], candidates, _MIN_DECLARED_SECRET_LENGTH
            )
    picked: list[str] = []
    for value, min_length in candidates:
        if len(value) >= min_length and value not in picked:
            picked.append(value)
    return tuple(picked)


@contextlib.contextmanager
def credential_files(
    credentials: Sequence[LoadedCredential], *, container_readable: bool
) -> Iterator[tuple[Path | None, dict[str, str]]]:
    """Deliver every credential as a read-only file for one invocation.

    ``mkdtemp`` creates the directory readable only by its owner, which is
    what protects the values on this machine; the files inside it must still
    be readable by the container's runtime user in image mode, exactly as the
    managed container's TLS material is. The directory is removed when the
    invocation ends, so nothing outlives the run.

    A run without credentials creates no directory and so grants no local
    path exception at all.
    """

    if not credentials:
        yield None, {}
        return
    directory = Path(tempfile.mkdtemp(prefix="syntara-run-secrets-"))
    try:
        paths: dict[str, str] = {}
        for credential in credentials:
            path = directory / credential.name
            path.write_bytes(canonical_json_bytes(dict(credential.document)))
            path.chmod(0o644 if container_readable else 0o600)
            paths[credential.name] = str(path)
        yield directory, paths
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def mounted_credential_paths(credentials: Sequence[LoadedCredential]) -> dict[str, str]:
    """Return the in-container path each credential is mounted at."""

    return {
        credential.name: f"{CREDENTIAL_MOUNT_DIRECTORY}/{credential.name}"
        for credential in credentials
    }


def credential_secret_values(credentials: Sequence[LoadedCredential]) -> tuple[str, ...]:
    """Return every value across the supplied credentials that must be scrubbed."""

    picked: list[str] = []
    for credential in credentials:
        for value in credential.secret_values:
            if value not in picked:
                picked.append(value)
    return tuple(picked)


class _ScrubbingWriter:
    """A stdout stand-in that scrubs supplied secrets from everything a Step prints."""

    def __init__(self, stream: Any, secrets: tuple[str, ...]) -> None:
        self._stream = stream
        self._secrets = secrets

    def write(self, text: str) -> int:
        written = self._stream.write(_scrub_text(text, self._secrets))
        return len(text) if written is None else written

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def run_source(
    manifest: Path,
    target: RunTarget,
    inputs: Mapping[str, Any],
    input_source: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    on_progress: ProgressCallback | None = None,
    credentials: Sequence[LoadedCredential] = (),
) -> RunReport:
    """Execute one target from source through an in-process gRPC runtime.

    The workspace root becomes importable, the implementation is bound through
    the harness's compile-install-drift path, and the Step executes through
    ``ContainerClient`` against a runtime owned by this process. The runtime
    listens on a unix domain socket inside a private temporary directory, so
    only this user's processes can reach it while it runs. Unexpected
    exceptions are captured locally (the ABI never carries exception text)
    and attached to the report as its error detail.

    Credentials are delivered as read-only files in a second private directory
    and reach the Step as ABI paths, as they will in production. Nothing here
    can write under the approved secret mount, so this one loopback transport
    (client and runtime are the same process) is told to accept that directory
    as well; every other client and runtime keeps the production rule.
    """

    _make_importable(manifest.parent)
    binding = _bind_target(manifest, target)
    captured: list[str] = []
    handler = BoundedStepHandler(
        _LocalErrorCapture(container_step_for_binding(binding), captured), timeout_seconds
    )
    registry = StepRegistry({target.identity: handler})
    with credential_files(credentials, container_readable=False) as (secrets_directory, paths):
        runtime_directory = tempfile.mkdtemp(prefix="syntara-run-runtime-")
        socket_path = Path(runtime_directory) / "runtime.sock"
        server = grpc.server(ThreadPoolExecutor(max_workers=2))
        add_container_runtime_service(server, registry, secrets_directory)
        if server.add_insecure_port(f"unix:{socket_path}") == 0:
            server.stop(None)
            shutil.rmtree(runtime_directory, ignore_errors=True)
            raise RunExecutionError("could not start the local runtime server")
        server.start()
        channel = grpc.insecure_channel(f"unix:{socket_path}")
        scrubbing_stderr = _ScrubbingWriter(
            sys.stderr,
            _secret_values(target.input_schema, inputs) + credential_secret_values(credentials),
        )
        try:
            # The Step runs in this process; anything it prints belongs on stderr,
            # scrubbed of supplied secrets, so stdout keeps carrying only the
            # report (or the --json document).
            with contextlib.redirect_stdout(scrubbing_stderr):
                return _execute_over_channel(
                    channel,
                    target,
                    inputs,
                    input_source,
                    mode="source",
                    address=f"unix:{socket_path}",
                    timeout_seconds=timeout_seconds,
                    on_progress=on_progress,
                    captured_detail=captured,
                    credentials=credentials,
                    credential_file_paths=paths,
                    local_credential_directory=secrets_directory,
                )
        finally:
            channel.close()
            server.stop(None)
            shutil.rmtree(runtime_directory, ignore_errors=True)


class _LocalErrorCapture:
    """Capture unexpected step exceptions locally for the run report.

    The container ABI deliberately never carries exception text (it may hold
    secrets), so a remote runtime discards it. Source mode executes in this
    process, so the CLI can hand the author their own traceback. Deliberate
    ``ContainerInvocationError`` failures already carry a safe message and
    pass through untouched.
    """

    def __init__(self, handler: ContainerStepHandler, captured: list[str]) -> None:
        self._handler = handler
        self._captured = captured

    def __call__(self, invocation: ContainerInvocation) -> ContainerResult:
        try:
            return self._handler(invocation)
        except ContainerInvocationError as error:
            cause = error.__cause__
            if isinstance(cause, StepExecutionError) and cause.detail:
                self._captured.append(cause.detail)
            elif error.detail:
                self._captured.append(error.detail)
            raise
        except StepExecutionError as error:
            if error.detail:
                self._captured.append(error.detail)
            raise
        except BaseException as error:
            self._captured.append("".join(traceback.format_exception(error)).rstrip())
            raise


def run_image(
    address: str,
    target: RunTarget,
    inputs: Mapping[str, Any],
    input_source: str,
    *,
    tls: TlsOptions | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    on_progress: ProgressCallback | None = None,
    image: str | None = None,
    startup_seconds: float = 10.0,
    liveness: Callable[[], None] | None = None,
    credentials: Sequence[LoadedCredential] = (),
) -> RunReport:
    """Execute one target against an already-running workload runtime.

    ``startup_seconds`` bounds how long the health check waits for a runtime
    that may still be booting (a freshly started container); the check
    retries within that window instead of failing on the first refusal.
    ``liveness`` is an optional probe consulted after each failed health
    attempt; it raises when the target is known to be gone, so a managed
    container that exited is reported with its own failure instead of
    consuming the whole window.

    ``credentials`` names what the caller has already mounted into the
    container: the paths sent here are the approved in-container mount paths,
    so this path needs no exception to the ABI rule.
    """

    channel = _channel(address, tls)
    try:
        return _execute_over_channel(
            channel,
            target,
            inputs,
            input_source,
            mode="image",
            address=address,
            timeout_seconds=timeout_seconds,
            on_progress=on_progress,
            image=image,
            startup_seconds=startup_seconds,
            liveness=liveness,
            credentials=credentials,
            credential_file_paths=mounted_credential_paths(credentials),
        )
    finally:
        channel.close()


def _execute_over_channel(
    channel: grpc.Channel,
    target: RunTarget,
    inputs: Mapping[str, Any],
    input_source: str,
    *,
    mode: str,
    address: str,
    timeout_seconds: int,
    on_progress: ProgressCallback | None,
    captured_detail: list[str] | None = None,
    image: str | None = None,
    startup_seconds: float = 10.0,
    liveness: Callable[[], None] | None = None,
    credentials: Sequence[LoadedCredential] = (),
    credential_file_paths: Mapping[str, str] | None = None,
    local_credential_directory: Path | None = None,
) -> RunReport:
    client = ContainerClient(channel, local_credential_directory)
    advertised = _health(client, address, startup_seconds=startup_seconds, liveness=liveness)
    dispatch_id = target.step_revision_id if mode == "image" else target.identity
    if dispatch_id not in advertised:
        listed = ", ".join(sorted(advertised)) or "none"
        raise RunExecutionError(
            f"the runtime at {address} does not advertise {dispatch_id!r}",
            detail=(f"Advertised steps: {listed}",),
            next_steps=(
                "check that the runtime was built from this workspace",
                "or select an advertised target",
            ),
        )
    invocation_id, workflow_run_id = _identifiers()
    invocation = ContainerInvocation(
        invocation_id=invocation_id,
        workflow_run_id=workflow_run_id,
        step_revision_id=dispatch_id,
        inputs=inputs,
        credential_file_paths=credential_file_paths or {},
        timeout_seconds=timeout_seconds,
        runtime_context={},
    )
    secrets = _secret_values(target.input_schema, inputs) + credential_secret_values(credentials)
    credential_names = tuple(credential.name for credential in credentials)
    redacted_inputs = _scrub_structure(redact_inputs(target.input_schema, inputs), secrets)
    terminal = _consume(client, invocation, _scrubbing_progress(on_progress, secrets))
    detail = (
        _scrub_text("\n\n".join(captured_detail).rstrip(), secrets)
        if captured_detail
        else None
    )
    if detail is not None and len(detail) > _MAX_ERROR_DETAIL_LENGTH:
        detail = detail[:_MAX_ERROR_DETAIL_LENGTH].rstrip() + "\n... (truncated)"
    if isinstance(terminal, _WorkloadFailure):
        return RunReport(
            mode=mode,
            target=target.identity,
            input_source=input_source,
            input_values=redacted_inputs,
            ok=False,
            invocation_id=invocation_id,
            workflow_run_id=workflow_run_id,
            status=None,
            status_message="",
            output=None,
            object_reference=None,
            output_schema_digest=None,
            error=RunErrorDetail(
                code=_scrub_text(terminal.error.code, secrets),
                message=_scrub_text(terminal.error.message, secrets),
                retryable=terminal.error.retryable,
            ),
            outcome=terminal.outcome,
            error_detail=detail,
            image=image,
            credentials=credential_names,
        )
    result = terminal
    error: RunErrorDetail | None = None
    if result.error_result is not None:
        error_result = result.error_result
        error = RunErrorDetail(
            code=_scrub_text(str(error_result["code"]), secrets),
            message=_scrub_text(str(error_result["message"]), secrets),
            retryable=bool(error_result["retryable"]),
        )
    return RunReport(
        mode=mode,
        target=target.identity,
        input_source=input_source,
        input_values=redacted_inputs,
        ok=result.status_code == 0 and error is None,
        invocation_id=invocation_id,
        workflow_run_id=workflow_run_id,
        status=result.status_code,
        status_message=_scrub_text(result.status_message, secrets),
        output=_scrub_structure(result.output, secrets) if result.output is not None else None,
        object_reference=(
            _scrub_text(result.object_reference, secrets)
            if result.object_reference is not None
            else None
        ),
        output_schema_digest=result.output_schema_digest,
        error=error,
        outcome=None,
        error_detail=detail,
        image=image,
        credentials=credential_names,
    )


def _health(
    client: ContainerClient,
    address: str,
    *,
    startup_seconds: float = 10.0,
    liveness: Callable[[], None] | None = None,
) -> tuple[str, ...]:
    """Confirm the runtime's health, retrying while it may still be booting.

    A runtime that was just started (a fresh container) may need a few
    seconds before its listener exists, so transport failures retry within
    ``startup_seconds``; an ABI mismatch fails immediately. After each failed
    attempt the optional ``liveness`` probe runs; it raises when the target
    (a managed container) has exited, so a dead runtime is reported with its
    own failure instead of waiting out the window.
    """

    deadline = time.monotonic() + max(startup_seconds, 0.0)
    last_remote_error: ContainerRemoteError | None = None
    while True:
        try:
            return client.health(timeout_seconds=_HEALTH_TIMEOUT_SECONDS)
        except ContainerRemoteError as error:
            if error.code == FailureCode.ABI_VIOLATION:
                raise RunExecutionError(
                    f"the runtime at {address} does not implement the supported "
                    f"container ABI ({CONTAINER_ABI})",
                    failure_code=error.code,
                    next_steps=("rebuild or update the workload image to the supported ABI",),
                ) from error
            last_remote_error = error
            if liveness is not None:
                liveness()
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
    assert last_remote_error is not None
    if liveness is not None:
        # A managed container is still running but never served: point at its
        # logs instead of suggesting the author start something they started.
        raise RunExecutionError(
            f"the workload container never became healthy within "
            f"{startup_seconds:.0f} seconds",
            failure_code=last_remote_error.code,
            next_steps=(
                "check the container log for the startup failure",
                "or rerun with --mode source to execute the Step from the workspace",
            ),
        ) from last_remote_error
    raise RunExecutionError(
        f"cannot reach a container runtime at {address}",
        failure_code=last_remote_error.code,
        next_steps=(
            "start the workload image and publish its port",
            f"or pass --address HOST:PORT (default {DEFAULT_ADDRESS})",
        ),
    ) from last_remote_error


def _scrubbing_progress(
    on_progress: ProgressCallback | None, secrets: tuple[str, ...]
) -> ProgressCallback | None:
    """Wrap the progress callback so step messages are scrubbed of secrets."""

    if on_progress is None:
        return None

    def wrapped(event: ContainerProgress) -> None:
        on_progress(
            ContainerProgress(
                invocation_id=event.invocation_id,
                sequence=event.sequence,
                state=event.state,
                message=_scrub_text(event.message, secrets),
                percent=event.percent,
            )
        )

    return wrapped


def _consume(
    client: ContainerClient, invocation: ContainerInvocation, on_progress: ProgressCallback | None
) -> ContainerResult | _WorkloadFailure:
    """Consume the event stream, returning the terminal outcome.

    A workload failure event is a terminal outcome of the Step, not a
    transport failure, so it is returned as a failed completion; protocol and
    transport failures raise :class:`RunExecutionError`.
    """

    try:
        for event in client.stream(invocation):
            if isinstance(event, ContainerResult):
                return event
            if on_progress is not None:
                on_progress(event)
    except ContainerRemoteError as error:
        if error.source == "workload":
            return _WorkloadFailure(
                error=RunErrorDetail(
                    code=str(error.code),
                    message=error.message,
                    retryable=error.retryable,
                ),
                outcome=error.outcome,
            )
        if error.code == FailureCode.TIMEOUT:
            raise RunExecutionError(
                f"the invocation did not complete within its "
                f"{invocation.timeout_seconds} second timeout",
                failure_code=error.code,
                detail=(f"outcome: {error.outcome}",),
                next_steps=(
                    "make the Step finish within the timeout",
                    "or split the work into smaller invocations",
                ),
            ) from error
        raise RunExecutionError(
            f"the invocation did not complete ({error.code}, source: {error.source})",
            failure_code=error.code,
            detail=(f"outcome: {error.outcome}",),
            next_steps=("address the failure above and rerun",),
        ) from error
    raise RunExecutionError("the runtime closed the stream without a terminal result")


@dataclass(frozen=True)
class _WorkloadFailure:
    """One terminal workload failure event, kept distinct from transport errors."""

    error: RunErrorDetail
    outcome: str


def _bind_target(manifest: Path, target: RunTarget) -> StepBinding:
    factory = StepBindingsFactory()
    try:
        return factory.bind_step(target.entry_point, workspace=manifest, identity=target.identity)
    except SchemaDriftError as error:
        raise RunExecutionError(
            "the Step implementation drifted from the compiled manifest",
            detail=(str(error),),
            next_steps=(
                "fix the implementation or the manifest, then rerun",
                _VALIDATE_NEXT_STEP,
            ),
        ) from error
    except HarnessUsageError as error:
        raise RunExecutionError(
            f"the Step implementation could not be bound: {error}",
            next_steps=(
                "check that the entry point module imports and its dependencies are installed",
                _VALIDATE_NEXT_STEP,
            ),
        ) from error
    except (Exception, SystemExit) as error:  # noqa: BLE001 - an author module's  # NOSONAR - converted to guidance below, not swallowed
        # import-time failure of any shape must become guidance, not a traceback
        location = _raise_location(error)
        raise RunExecutionError(
            f"the Step implementation could not be loaded: {type(error).__name__}: {error}",
            detail=(location,) if location is not None else (),
            next_steps=(
                "fix the error above; the entry point module must import cleanly",
                _VALIDATE_NEXT_STEP,
            ),
        ) from error


def _raise_location(error: BaseException) -> str | None:
    """Name the file and line an import-time failure came from, when known."""

    if isinstance(error, SyntaxError) and error.filename is not None:
        return f"at {error.filename}:{error.lineno}"
    frames = traceback.extract_tb(error.__traceback__)
    if not frames:
        return None
    frame = frames[-1]
    return f"at {frame.filename}:{frame.lineno} in {frame.name}"


def _select_requested(
    targets: Sequence[Mapping[str, Any]],
    runnable: Sequence[Mapping[str, Any]],
    requested: str,
) -> Mapping[str, Any]:
    for matcher in (_by_identity, _by_entry_point, _by_short_name):
        matches = [target for target in runnable if matcher(target, requested)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RunUsageError(
                f"target {requested!r} matches more than one runnable target",
                detail=_target_lines(matches),
                next_steps=("use the full Step identity to select one",),
            )
    declarative = [
        target
        for target in targets
        if _is_step_target(target)
        and not _is_runnable(target)
        and (
            _by_identity(target, requested)
            or _by_entry_point(target, requested)
            or _by_short_name(target, requested)
        )
    ]
    if declarative:
        runtime_kind = declarative[0].get("runtime", {}).get("kind", "platform-runner")
        raise RunUsageError(
            f"{declarative[0]['identity']!r} uses the {runtime_kind} runtime "
            "and cannot run locally",
            detail=("only custom-runner targets can be executed by this command",),
            next_steps=("run syntara-plugin inspect to review the target",),
        )
    detail = _target_lines(runnable) or ("the workspace declares no custom-runner Step targets",)
    raise RunUsageError(
        f"no runnable target matches {requested!r} in the compiled workspace",
        detail=detail,
        next_steps=("run syntara-plugin run with one of the targets above",)
        if runnable
        else ("run syntara-plugin inspect to review the declared targets",),
    )


def _is_step_target(target: Mapping[str, Any]) -> bool:
    return target.get("kind") in _RUNNABLE_KINDS


def _is_runnable(target: Mapping[str, Any]) -> bool:
    return _is_step_target(target) and target.get("runtime", {}).get("kind") == "custom-runner"


def _by_identity(target: Mapping[str, Any], requested: str) -> bool:
    return str(target.get("identity", "")) == requested


def _by_entry_point(target: Mapping[str, Any], requested: str) -> bool:
    return str(target.get("runtime", {}).get("entryPoint", "")) == requested


def _by_short_name(target: Mapping[str, Any], requested: str) -> bool:
    return str(target.get("metadata", {}).get("name", "")) == requested


def _target_lines(targets: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    if not targets:
        return ()
    width = max(len(str(target["identity"])) for target in targets) + 4
    return tuple(
        f"  {target['identity']:<{width}}{str(target['kind']):<{_KIND_COLUMN_WIDTH}}"
        f"{target.get('runtime', {}).get('entryPoint', '-')}"
        for target in targets
    )


def _make_importable(workspace_root: Path) -> None:
    root = str(workspace_root)
    if root not in sys.path:
        sys.path.insert(0, root)


def _identifiers() -> tuple[str, str]:
    suffix = uuid.uuid4().hex
    return f"run-{suffix}", f"run-{suffix}-run"


def _channel(address: str, tls: TlsOptions | None) -> grpc.Channel:
    # The runtime is local or author-named; a shell's proxy variables would
    # otherwise stall or misroute the channel for minutes.
    options = [("grpc.enable_http_proxy", 0)]
    if tls is None:
        return grpc.insecure_channel(address, options=options)
    try:
        credentials = grpc.ssl_channel_credentials(
            root_certificates=tls.ca.read_bytes(),
            private_key=tls.key.read_bytes(),
            certificate_chain=tls.certificate.read_bytes(),
        )
    except OSError as error:
        raise RunUsageError(f"could not read the TLS material: {error}") from error
    return grpc.secure_channel(address, credentials, options=options)


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"{value} is not valid JSON")


def _bounded(line: str) -> str:
    """Bound one validation line so embedded instances cannot flood output."""

    if len(line) <= _MAX_VALIDATION_LINE_LENGTH:
        return line
    return line[: _MAX_VALIDATION_LINE_LENGTH - 3].rstrip() + "..."


def _pointer(path: Sequence[Any]) -> str:
    """Render one error path as an RFC 6901 JSON Pointer.

    Every segment is escaped (``~`` becomes ``~0``, ``/`` becomes ``~1``), so
    a property literally named ``foo.bar`` renders ``/foo.bar`` and stays
    distinguishable from the nested ``/foo/bar``.
    """

    rendered = ""
    for item in path:
        escaped = str(item).replace("~", "~0").replace("/", "~1")
        rendered += f"/{escaped}"
    return rendered


def _join_pointer(path: Sequence[Any], name: str) -> str:
    escaped = str(name).replace("~", "~0").replace("/", "~1")
    return _pointer(path) + f"/{escaped}"


def _sensitive_error(
    properties: Mapping[str, Any],
    path: Sequence[Any],
    secret_paths: Sequence[tuple[Any, ...]] = (),
) -> bool:
    """Return whether an error path touches a credential-like field.

    The top-level property is checked against its declared schema; the
    declared-secret locations found by the marker pass are checked for overlap
    in either direction, so an error on a secret-holding map or inside one is
    sanitized; the leaf is checked against the schema resolved by walking the
    error path (array indices descend into ``items``), so a nested
    ``writeOnly`` or ``format: password`` declaration is honored even when
    the parent property has a non-credential name and the leaf is an array
    index.
    """

    if not path:
        return False
    top = path[0]
    if isinstance(top, str) and is_sensitive(top, properties.get(top, {})):
        return True
    rendered = tuple(path)
    if any(_paths_overlap(secret_path, rendered) for secret_path in secret_paths):
        return True
    leaf_schema = _resolve_leaf_schema(properties, path)
    if is_declared_secret(leaf_schema):
        return True
    leaf = path[-1]
    return isinstance(leaf, str) and is_sensitive(leaf, leaf_schema)


def _paths_overlap(first: tuple[Any, ...], second: tuple[Any, ...]) -> bool:
    """Return whether either path is a prefix of the other."""

    return first[: len(second)] == second or second[: len(first)] == first


def _resolve_leaf_schema(
    properties: Mapping[str, Any], path: Sequence[Any]
) -> Mapping[str, Any]:
    """Walk one error path through the schema to the leaf property's schema.

    Array indices descend into the declared ``items`` schema; a segment that
    leaves the schema yields an empty mapping, leaving the name-only check.
    """

    schema: Any = {"properties": properties}
    for item in path:
        if not isinstance(schema, Mapping):
            return {}
        if isinstance(item, int):
            schema = schema.get("items", {})
            continue
        nested = schema.get("properties", {})
        if not isinstance(nested, Mapping):
            return {}
        schema = nested.get(item, {})
    return schema if isinstance(schema, Mapping) else {}
