"""Concrete-Step-kind pre-execution input validation dispatch."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

_MAX_VALIDATION_ERRORS = 10
_MAX_VALIDATION_DETAILS_LENGTH = 2_000
_MAX_LOCATION_LENGTH = 128


class StepInputValidationError(ValueError):
    """An invalid step input with a caller-safe diagnostic message."""

    def __init__(self, safe_message: str) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message


def validate_step_inputs(
    inputs: object,
    input_schema: Mapping[str, Any],
    input_model: type[BaseModel],
    input_schema_source: str,
) -> BaseModel:
    """Select validation from the trusted descriptor's schema source."""

    if input_schema_source == "manifest":
        # Delayed to keep manifest validation outside generic execution.
        from .manifest_validation import validate_manifest_inputs

        return validate_manifest_inputs(inputs, input_schema, input_model)
    return _validate_model_inputs(inputs, input_model)


def _validate_model_inputs(inputs: object, input_model: type[BaseModel]) -> BaseModel:
    """Retain registered Pydantic semantics for implementation-derived inputs."""

    try:
        return input_model.model_validate(inputs)
    except ValidationError as error:
        raise StepInputValidationError(
            "The input failed model validation: " + _pydantic_details(error)
        ) from error
    except Exception as error:
        raise StepInputValidationError("The input failed model construction.") from error


def _pydantic_details(error: ValidationError) -> str:
    return format_validation_details(
        [
            f"{format_validation_location(item['loc'])}: {item['type']}"
            for item in error.errors()
        ]
    )


def format_validation_details(details: list[str]) -> str:
    """Bound stable validation diagnostics without including rejected values."""

    reported = details[:_MAX_VALIDATION_ERRORS]
    remaining = len(details) - len(reported)
    if remaining > 0:
        reported.append(f"and {remaining} more")
    value = "; ".join(reported)
    if len(value) > _MAX_VALIDATION_DETAILS_LENGTH:
        value = value[: _MAX_VALIDATION_DETAILS_LENGTH - 3].rstrip() + "..."
    return value


def format_validation_location(location: object) -> str:
    """Render a bounded JSON Schema or Pydantic location."""

    if isinstance(location, tuple):
        path = ".".join(_escape_location_part(part) for part in location)
    else:
        path = _escape_location_part(location)
    path = path or "<root>"
    if len(path) > _MAX_LOCATION_LENGTH:
        path = path[: _MAX_LOCATION_LENGTH - 3] + "..."
    return path


def _escape_location_part(part: object) -> str:
    """Keep caller-controlled field names safe in returned diagnostics."""

    value = str(part)
    escaped: list[str] = []
    for character in value:
        if character.isprintable():
            escaped.append(character)
        else:
            escaped.append(f"\\u{ord(character):04x}")
    return "".join(escaped)
