"""Immutable outcomes returned by direct plugin execution."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer
from syntara_plugin.contracts import CanonicalJsonError, canonical_json_bytes, load_bundle

MAX_MESSAGE_LENGTH = 4_096
_DEFAULT_FAILURE_MESSAGE = "The step failed."


class ErrorCategory(StrEnum):
    """Broad category for a deliberate step failure."""

    VALIDATION = "validation"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    BUSINESS = "business"
    DEPENDENCY = "dependency"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    POLICY = "policy"
    CONTRACT = "contract"
    INTERNAL = "internal"


def bounded_message(value: str, *, fallback: str) -> str:
    """Return printable, bounded text suitable for a runtime diagnostic."""

    if not isinstance(value, str):
        return fallback
    safe = "".join(character if character.isprintable() else " " for character in value)
    safe = safe.strip()
    if not safe:
        return fallback
    if len(safe) > MAX_MESSAGE_LENGTH:
        safe = safe[: MAX_MESSAGE_LENGTH - 3].rstrip() + "..."
    return safe


class _FrozenList(tuple[Any, ...]):
    """Tuple-backed JSON array that compares by JSON sequence contents."""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (list, tuple)):
            return tuple(self) == tuple(other)
        return NotImplemented

    __hash__ = tuple.__hash__


class ErrorResult(BaseModel):
    """Structured, safe error metadata carried by a deliberate failure."""

    model_config = ConfigDict(frozen=True, extra="allow")

    category: ErrorCategory = ErrorCategory.BUSINESS
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)
    retryable: bool = Field(default=False, strict=True)

    @field_validator("message", mode="before")
    @classmethod
    def _bound_message(cls, value: object) -> str:
        if not isinstance(value, str):
            return _DEFAULT_FAILURE_MESSAGE
        return bounded_message(value, fallback=_DEFAULT_FAILURE_MESSAGE)

    def model_post_init(self, __context: Any) -> None:
        extras = self.__pydantic_extra__ or {}
        validate_json_object(extras)
        object.__setattr__(self, "__pydantic_extra__", freeze_json(extras))
        for name in type(self).model_fields.keys() - ErrorResult.model_fields.keys():
            value = getattr(self, name)
            validate_json_object({name: value})
            object.__setattr__(self, name, freeze_json(value))

    @model_serializer
    def _serialize(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            **{
                name: thaw_json(getattr(self, name))
                for name in type(self).model_fields.keys() - ErrorResult.model_fields.keys()
            },
            **thaw_json(self.__pydantic_extra__ or {}),
        }


# Compatibility for existing Python callers; serialized contracts use ErrorResult.
ErrorMessage = ErrorResult


def error_result_schema() -> dict[str, Any]:
    """Return the strict fallback schema used when no spec.error is declared."""

    return load_bundle().documents["runtime.schema.json"]["$defs"]["errorResult"]


def error_result_base_schema() -> dict[str, Any]:
    """Return the shared schema that custom error schemas extend."""

    return load_bundle().documents["runtime.schema.json"]["$defs"]["errorResultBase"]


def freeze_json(value: Any) -> Any:
    """Copy a JSON value into recursively immutable containers."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("outcome result object keys must be strings")
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return _FrozenList(freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise TypeError("outcome result must contain only JSON-compatible values")


def thaw_json(value: Any) -> Any:
    """Return a detached, mutable JSON copy for legacy transport consumers."""

    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def validate_json_object(value: object) -> None:
    """Require an object with values supported by the canonical JSON contract."""

    if not isinstance(value, Mapping):
        raise ValueError("result must be a JSON object")
    try:
        frozen = freeze_json(value)
        canonical_json_bytes(thaw_json(frozen))
    except (CanonicalJsonError, RecursionError, TypeError, ValueError):
        raise ValueError("result must be a JSON object") from None


@dataclass(frozen=True, slots=True, eq=False)
class StandardOutputWrapper[TOutput]:
    """A deeply immutable result envelope for one completed step invocation.

    ``result`` contains the JSON representation checked against the installed
    output schema. Mappings become read-only mapping proxies and arrays become
    tuples, including at nested levels.
    """

    result: TOutput | None
    status_code: int
    status_message: str
    error_result: ErrorResult | None

    def __post_init__(self) -> None:
        if type(self.status_code) is not int or not 0 <= self.status_code <= 4_294_967_295:
            raise ValueError("status_code must be an unsigned 32-bit integer")
        object.__setattr__(
            self,
            "status_message",
            bounded_message(self.status_message, fallback="Completed"),
        )
        if self.result is not None:
            validate_json_object(self.result)
            object.__setattr__(self, "result", cast(TOutput, freeze_json(self.result)))

        if self.status_code == 0:
            if self.error_result is not None:
                raise ValueError("a successful outcome cannot carry an error")
            if self.result is None:
                raise ValueError("a successful outcome requires a result")
        elif not isinstance(self.error_result, ErrorResult):
            raise ValueError("a failed outcome requires structured error metadata")

    @classmethod
    def success(
        cls,
        result: TOutput,
        *,
        status_message: str = "Completed",
    ) -> StandardOutputWrapper[TOutput]:
        """Build a successful outcome from a JSON-compatible result."""

        return cls(
            result=result,
            status_code=0,
            status_message=status_message,
            error_result=None,
        )

    @classmethod
    def failure(
        cls,
        error: ErrorResult | None = None,
        *,
        status_code: int = 1,
        status_message: str | None = None,
        error_result: ErrorResult | None = None,
        error_message: ErrorResult | str | None = None,
        code: str = "STEP_FAILED",
        category: ErrorCategory = ErrorCategory.BUSINESS,
        retryable: bool = False,
        result: TOutput | None = None,
        output: TOutput | None = None,
    ) -> StandardOutputWrapper[TOutput]:
        """Build a deliberate failure outcome from caller-authored safe metadata.

        ``error=`` and ``error_message=`` are compatibility aliases for
        ``error_result=``; ``output=`` aliases ``result=``.
        """

        if sum(value is not None for value in (error, error_result, error_message)) > 1:
            raise ValueError("provide only one error result")
        if error_result is not None or error is not None:
            error_message = error_result if error_result is not None else error
        if result is not None and output is not None:
            raise ValueError("provide either result or output")
        if result is None:
            result = output
        if status_code == 0:
            raise ValueError("a failure status_code must be nonzero")
        if isinstance(error_message, ErrorResult):
            structured_error = error_message
        elif isinstance(error_message, str):
            structured_error = ErrorResult(
                category=category,
                code=code,
                message=error_message,
                retryable=retryable,
            )
        else:
            raise ValueError("failure requires structured error metadata")
        safe_status = bounded_message(
            status_message if status_message is not None else structured_error.message,
            fallback=_DEFAULT_FAILURE_MESSAGE,
        )
        return cls(
            result=result,
            status_code=status_code,
            status_message=safe_status,
            error_result=structured_error,
        )

    @property
    def status(self) -> int:
        """Compatibility alias for ``status_code``."""

        return self.status_code

    @property
    def output(self) -> Mapping[str, Any] | None:
        """Compatibility copy for mapping-shaped results used by transports."""

        return thaw_json(self.result) if isinstance(self.result, Mapping) else None

    @property
    def error(self) -> ErrorResult | None:
        """Compatibility alias used by the existing container adapter."""

        return self.error_result

    @property
    def error_message(self) -> ErrorResult | None:
        """Compatibility alias for the previous Python field name."""

        return self.error_result

    def model_dump(
        self,
        *,
        mode: str = "python",
        by_alias: bool = True,
    ) -> dict[str, Any]:
        """Serialize the four wrapper fields using the contract's JSON names."""

        if mode not in {"python", "json"}:
            raise ValueError("mode must be 'python' or 'json'")
        names = (
            ("result", "statusCode", "statusMessage", "errorResult")
            if by_alias
            else ("result", "status_code", "status_message", "error_result")
        )
        error = (
            self.error_result.model_dump(mode=mode)
            if self.error_result is not None
            else None
        )
        return {
            names[0]: thaw_json(self.result),
            names[1]: self.status_code,
            names[2]: self.status_message,
            names[3]: error,
        }

    def _canonical_content(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        other_wrapper = cast(StandardOutputWrapper[Any], other)
        return self._canonical_content() == other_wrapper._canonical_content()

    def __hash__(self) -> int:
        """Hash the canonical immutable envelope, including nested JSON data."""

        return hash(self._canonical_content())

    def model_dump_json(self, *, by_alias: bool = True) -> str:
        """Serialize the wrapper as compact JSON using contract field names."""

        return json.dumps(
            self.model_dump(mode="json", by_alias=by_alias),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
