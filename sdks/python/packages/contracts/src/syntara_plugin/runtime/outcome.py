"""Immutable outcomes returned by direct plugin execution."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator
from syntara_plugin.contracts import CanonicalJsonError, canonical_json_bytes

_MAX_MESSAGE_LENGTH = 4_096


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


def _bounded_message(value: str, *, fallback: str) -> str:
    """Return printable, bounded text suitable for a runtime diagnostic."""

    if not isinstance(value, str):
        return fallback
    safe = "".join(character if character.isprintable() else " " for character in value)
    safe = safe.strip()
    if not safe:
        return fallback
    if len(safe) > _MAX_MESSAGE_LENGTH:
        safe = safe[: _MAX_MESSAGE_LENGTH - 3].rstrip() + "..."
    return safe


class _FrozenList(tuple[Any, ...]):
    """Tuple-backed JSON array that compares by JSON sequence contents."""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (list, tuple)):
            return tuple(self) == tuple(other)
        return NotImplemented

    __hash__ = tuple.__hash__


class ErrorMessage(BaseModel):
    """Structured, safe error metadata carried by a deliberate failure."""

    model_config = ConfigDict(frozen=True)

    category: ErrorCategory = ErrorCategory.BUSINESS
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    message: str = Field(min_length=1, max_length=_MAX_MESSAGE_LENGTH)
    retryable: bool = Field(default=False, strict=True)

    @field_validator("message", mode="before")
    @classmethod
    def _bound_message(cls, value: object) -> str:
        if not isinstance(value, str):
            return "The step failed."
        return _bounded_message(value, fallback="The step failed.")


def _freeze_json(value: Any) -> Any:
    """Copy a JSON value into recursively immutable containers."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("outcome result object keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return _FrozenList(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise TypeError("outcome result must contain only JSON-compatible values")


def _thaw_json(value: Any) -> Any:
    """Return a detached, mutable JSON copy for legacy transport consumers."""

    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _validate_json_object(value: object) -> None:
    """Require an object with values supported by the canonical JSON contract."""

    if not isinstance(value, Mapping):
        raise ValueError("result must be a JSON object")
    try:
        frozen = _freeze_json(value)
        canonical_json_bytes(_thaw_json(frozen))
    except (CanonicalJsonError, RecursionError, TypeError, ValueError):
        raise ValueError("result must be a JSON object") from None


@dataclass(frozen=True, slots=True)
class StandardOutputWrapper[TOutput]:
    """A deeply immutable result envelope for one completed step invocation.

    ``result`` contains the JSON representation checked against the installed
    output schema. Mappings become read-only mapping proxies and arrays become
    tuples, including at nested levels.
    """

    result: TOutput | None
    status_code: int
    status_message: str
    error_message: ErrorMessage | None

    def __post_init__(self) -> None:
        if type(self.status_code) is not int or not 0 <= self.status_code <= 4_294_967_295:
            raise ValueError("status_code must be an unsigned 32-bit integer")
        object.__setattr__(
            self,
            "status_message",
            _bounded_message(self.status_message, fallback="Completed"),
        )
        if self.result is not None:
            _validate_json_object(self.result)
            object.__setattr__(self, "result", cast(TOutput, _freeze_json(self.result)))

        if self.status_code == 0:
            if self.error_message is not None:
                raise ValueError("a successful outcome cannot carry an error")
            if self.result is None:
                raise ValueError("a successful outcome requires a result")
        elif not isinstance(self.error_message, ErrorMessage):
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
            error_message=None,
        )

    @classmethod
    def failure(
        cls,
        error: ErrorMessage | None = None,
        *,
        status_code: int = 1,
        status_message: str | None = None,
        error_message: ErrorMessage | str | None = None,
        code: str = "STEP_FAILED",
        category: ErrorCategory = ErrorCategory.BUSINESS,
        retryable: bool = False,
        result: TOutput | None = None,
        output: TOutput | None = None,
    ) -> StandardOutputWrapper[TOutput]:
        """Build a deliberate failure outcome from caller-authored safe metadata.

        ``error=`` and ``output=`` retain compatibility with the prior runtime
        result API. New callers should pass ``status_message`` and
        ``error_message`` directly.
        """

        if error is not None:
            if error_message is not None:
                raise ValueError("provide either error or error_message")
            error_message = error
        if result is not None and output is not None:
            raise ValueError("provide either result or output")
        if result is None:
            result = output
        if status_code == 0:
            raise ValueError("a failure status_code must be nonzero")
        if isinstance(error_message, ErrorMessage):
            structured_error = error_message
        elif isinstance(error_message, str):
            structured_error = ErrorMessage(
                category=category,
                code=code,
                message=error_message,
                retryable=retryable,
            )
        else:
            raise ValueError("failure requires structured error metadata")
        safe_status = _bounded_message(
            status_message if status_message is not None else structured_error.message,
            fallback="The step failed.",
        )
        return cls(
            result=result,
            status_code=status_code,
            status_message=safe_status,
            error_message=structured_error,
        )

    @property
    def status(self) -> int:
        """Compatibility alias for ``status_code``."""

        return self.status_code

    @property
    def output(self) -> Mapping[str, Any] | None:
        """Compatibility copy for mapping-shaped results used by transports."""

        return _thaw_json(self.result) if isinstance(self.result, Mapping) else None

    @property
    def error(self) -> ErrorMessage | None:
        """Compatibility alias used by the existing container adapter."""

        return self.error_message

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
            ("result", "statusCode", "statusMessage", "errorMessage")
            if by_alias
            else ("result", "status_code", "status_message", "error_message")
        )
        error = (
            self.error_message.model_dump(mode=mode)
            if self.error_message is not None
            else None
        )
        return {
            names[0]: _thaw_json(self.result),
            names[1]: self.status_code,
            names[2]: self.status_message,
            names[3]: error,
        }

    def model_dump_json(self, *, by_alias: bool = True) -> str:
        """Serialize the wrapper as compact JSON using contract field names."""

        return json.dumps(
            self.model_dump(mode="json", by_alias=by_alias),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
