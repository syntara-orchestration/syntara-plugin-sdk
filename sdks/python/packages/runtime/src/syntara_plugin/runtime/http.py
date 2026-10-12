"""Pure request and response mapping for the Syntara-managed ``http.v1`` runner.

This module intentionally has no HTTP client, credential handling, endpoint
selection, or environment access.  A platform runner supplies a trusted base
endpoint after Syntara resolves the installed action and approved connection.
The helpers here only turn one validated, installed operation plus structured
data into a relative request path, optional JSON body, or declared output.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from syntara_plugin.contracts import ContractBundle, load_bundle, validate_document

_HTTP_METHODS = frozenset({"DELETE", "GET", "PATCH", "POST", "PUT"})
_HTTP_OPERATION_KEYS = frozenset({"method", "path", "requestMap", "responseMap"})
_HTTP_PATH_STATIC_CHARACTERS = re.compile(r"^[A-Za-z0-9._~!$&'()*+,;=:@{}\-/]+$")
_HTTP_PATH_INPUT = re.compile(r"\{input\.([A-Za-z][A-Za-z0-9_-]{0,127})\}")
_JSON_POINTER = re.compile(r"^(?:/(?:[^~/]|~[01])*)+$")
_MAX_HTTP_PATH_LENGTH = 1024
_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
_INVALID_RUNNER_CONTEXT = "http.v1 runtime context does not satisfy the contract"


class HttpOperationError(ValueError):
    """Raised when an installed ``http.v1`` operation cannot be mapped safely."""


class HttpRunnerContextError(ValueError):
    """Raised when trusted runtime context is not a safe ``http.v1`` declaration."""


@dataclass(frozen=True)
class HttpRequest:
    """One credential-free request ready for a trusted platform HTTP client.

    ``relative_path`` is deliberately not a URL.  Only the Syntara-managed
    connection boundary may join it to an approved endpoint origin.
    """

    method: str
    relative_path: str
    json_body: dict[str, Any] | None


@dataclass(frozen=True)
class HttpRunnerContext:
    """The non-secret context a Syntara-managed HTTP runner needs per invocation."""

    operation: Mapping[str, Any]
    output_schema_digest: str


class HttpOperationMapper:
    """Map a validated declarative operation without performing network I/O."""

    def __init__(self, bundle: ContractBundle | None = None) -> None:
        self._bundle = bundle or load_bundle()

    def build_request(
        self, operation: Mapping[str, Any], input_data: Mapping[str, Any]
    ) -> HttpRequest:
        """Build a relative request from one installed operation and workflow input."""

        normalized = self._validate_operation(operation)
        path = _render_relative_path(normalized["path"], input_data)
        request_map = normalized.get("requestMap")
        body = (
            _apply_mapping(request_map, input_data, "requestMap")
            if request_map is not None
            else None
        )
        return HttpRequest(method=normalized["method"], relative_path=path, json_body=body)

    def map_response(self, operation: Mapping[str, Any], response_data: object) -> dict[str, Any]:
        """Project a parsed JSON response into the action's declared output fields."""

        normalized = self._validate_operation(operation)
        response_map = normalized.get("responseMap")
        if response_map is None:
            return {}
        return _apply_mapping(response_map, response_data, "responseMap")

    def _validate_operation(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        if set(operation) - _HTTP_OPERATION_KEYS:
            raise HttpOperationError("http.v1 operation contains unsupported fields")
        method = operation.get("method")
        path = operation.get("path")
        if not isinstance(method, str) or method not in _HTTP_METHODS:
            raise HttpOperationError("http.v1 operation requires a supported HTTP method")
        if not isinstance(path, str) or not _valid_http_path(path):
            raise HttpOperationError("http.v1 operation requires an origin-less relative path")

        normalized: dict[str, Any] = {"method": method, "path": path}
        for mapping_name in ("requestMap", "responseMap"):
            mapping = operation.get(mapping_name)
            if mapping is None:
                continue
            errors = validate_document(self._bundle, "http_mapping", mapping)
            if errors:
                raise HttpOperationError(
                    f"http.v1 {mapping_name} does not satisfy the mapping contract"
                )
            normalized[mapping_name] = mapping
        return normalized


def build_http_request(
    operation: Mapping[str, Any],
    input_data: Mapping[str, Any],
    *,
    bundle: ContractBundle | None = None,
) -> HttpRequest:
    """Build one request using the default pure ``http.v1`` mapper."""

    return HttpOperationMapper(bundle).build_request(operation, input_data)


def map_http_response(
    operation: Mapping[str, Any], response_data: object, *, bundle: ContractBundle | None = None
) -> dict[str, Any]:
    """Map one parsed response using the default pure ``http.v1`` mapper."""

    return HttpOperationMapper(bundle).map_response(operation, response_data)


def load_http_runner_context(
    context: Mapping[str, Any], *, bundle: ContractBundle | None = None
) -> HttpRunnerContext:
    """Validate one non-secret ``http.v1`` context carried by the generic ABI.

    The context is resolved from an installed action by Syntara. It has no
    endpoint, authorization header, credential value, or network capability.
    """

    selected_bundle = bundle or load_bundle()
    if not isinstance(context, Mapping) or validate_document(
        selected_bundle, "http_runner_context", context
    ):
        raise HttpRunnerContextError(_INVALID_RUNNER_CONTEXT)
    operation = context.get("operation")
    output_schema_digest = context.get("outputSchemaDigest")
    if not isinstance(operation, Mapping) or not isinstance(output_schema_digest, str):
        raise HttpRunnerContextError(_INVALID_RUNNER_CONTEXT)
    if not _SHA256.fullmatch(output_schema_digest):
        raise HttpRunnerContextError(_INVALID_RUNNER_CONTEXT)
    return HttpRunnerContext(operation=dict(operation), output_schema_digest=output_schema_digest)


def _valid_http_path(path: str) -> bool:
    if not path.startswith("/") or path.startswith("//") or len(path) > _MAX_HTTP_PATH_LENGTH:
        return False
    static_path = _HTTP_PATH_INPUT.sub("", path)
    if "{" in static_path or "}" in static_path:
        return False
    if not _HTTP_PATH_STATIC_CHARACTERS.fullmatch(path):
        return False
    return all(segment not in {".", ".."} for segment in static_path.split("/"))


def _render_relative_path(template: str, input_data: Mapping[str, Any]) -> str:
    if not _valid_http_path(template):
        raise HttpOperationError("http.v1 operation requires an origin-less relative path")

    def replace(match: re.Match[str]) -> str:
        field = match.group(1)
        value = input_data.get(field)
        if not isinstance(value, str) or not _safe_path_segment(value):
            raise HttpOperationError(
                f"http.v1 path input {field!r} must be a safe non-empty string"
            )
        return quote(value, safe="-._~")

    return _HTTP_PATH_INPUT.sub(replace, template)


def _safe_path_segment(value: str) -> bool:
    if value in {"", ".", ".."}:
        return False
    return not any(
        character in {"/", "\\", "%", "?", "#"} or ord(character) < 32 or ord(character) == 127
        for character in value
    )


def _apply_mapping(mapping: object, document: object, mapping_name: str) -> dict[str, Any]:
    if not isinstance(mapping, Mapping):
        raise HttpOperationError(f"http.v1 {mapping_name} must be an object")

    result: dict[str, Any] = {}
    for field in sorted(mapping):
        source = mapping[field]
        if not isinstance(field, str) or not isinstance(source, Mapping):
            raise HttpOperationError(f"http.v1 {mapping_name} contains an invalid entry")
        if "literal" in source:
            result[field] = source["literal"]
            continue
        pointer = source.get("from")
        optional = source.get("optional", False)
        if not isinstance(pointer, str) or not _JSON_POINTER.fullmatch(pointer):
            raise HttpOperationError(f"http.v1 {mapping_name} contains an invalid JSON Pointer")
        value = _json_pointer_value(document, pointer)
        if value is _MISSING:
            if optional is True:
                continue
            raise HttpOperationError(
                f"http.v1 {mapping_name} is missing required input for {field!r}"
            )
        result[field] = value
    return result


_MISSING = object()


def _json_pointer_value(document: object, pointer: str) -> object:
    value = document
    for encoded_segment in pointer.removeprefix("/").split("/"):
        segment = encoded_segment.replace("~1", "/").replace("~0", "~")
        if isinstance(value, Mapping):
            value = value.get(segment, _MISSING)
        elif isinstance(value, list) and segment.isdecimal():
            index = int(segment)
            value = value[index] if index < len(value) else _MISSING
        else:
            return _MISSING
        if value is _MISSING:
            return _MISSING
    return value
