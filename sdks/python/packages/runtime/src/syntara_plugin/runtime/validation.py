"""Strict pre-execution validation for installed Action inputs."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ValidationError

_MAX_VALIDATION_ERRORS = 10
_MAX_VALIDATION_DETAILS_LENGTH = 2_000
_MAX_LOCATION_LENGTH = 128


class ActionInputValidationError(ValueError):
    """An invalid Action input with a caller-safe diagnostic message."""

    def __init__(self, safe_message: str) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message


def validate_action_inputs(
    inputs: Mapping[str, Any],
    input_schema: Mapping[str, Any],
    input_model: type[BaseModel],
) -> BaseModel:
    """Validate raw Action input and construct its registered Pydantic model.

    The installed manifest schema is authoritative. Pydantic is constructed in
    strict mode only after JSON Schema validation succeeds, preventing lax
    coercion from accepting a value that the published Action contract rejects.
    JSON Schema ``default`` values are annotations; the registered Pydantic model
    supplies the actual default used by the implementation.
    Unknown fields follow the manifest schema: they are rejected when the schema
    forbids them and otherwise ignored by the implementation model.
    """

    if not isinstance(inputs, Mapping):
        raise ActionInputValidationError("input: must be a JSON object")
    payload = dict(inputs)
    if not _is_json_compatible(payload):
        raise ActionInputValidationError("input: must contain only JSON-compatible values")

    schema_validator = Draft202012Validator(input_schema, format_checker=FormatChecker())
    schema_errors = sorted(
        schema_validator.iter_errors(payload),
        key=lambda error: (error.json_path, str(error.validator)),
    )
    if schema_errors:
        raise ActionInputValidationError(
            "The plugin action input failed schema validation: "
            + _bounded_details(
                [
                    _schema_error_detail(error)
                    for error in schema_errors
                ]
            )
        )

    try:
        return input_model.model_validate(payload, strict=True)
    except ValidationError as error:
        raise ActionInputValidationError(
            "The plugin action input failed model validation: "
            + _pydantic_details(error)
        ) from error


def _pydantic_details(error: ValidationError) -> str:
    errors = error.errors()
    return _bounded_details(
        [f"{_bounded_location(item['loc'])}: {item['type']}" for item in errors]
    )


def _schema_error_detail(error: JsonSchemaValidationError) -> str:
    location = _bounded_location(error.json_path)
    if error.validator == "required" and isinstance(error.validator_value, list):
        if isinstance(error.instance, dict):
            missing_keys = [k for k in error.validator_value if k not in error.instance]
        else:
            missing_keys = list(error.validator_value)
        missing = ", ".join(str(k) for k in missing_keys)
        return f"{location}: required ({_bounded_location(missing)})"
    return f"{location}: {error.validator}"


def _is_json_compatible(value: object) -> bool:
    if value is None or isinstance(value, str | bool | int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_compatible(item) for item in value)
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _is_json_compatible(item) for key, item in value.items()
        )
    return False


def _bounded_details(details: list[str]) -> str:
    reported = details[:_MAX_VALIDATION_ERRORS]
    remaining = len(details) - len(reported)
    if remaining > 0:
        reported.append(f"and {remaining} more")
    value = "; ".join(reported)
    if len(value) > _MAX_VALIDATION_DETAILS_LENGTH:
        value = value[: _MAX_VALIDATION_DETAILS_LENGTH - 3].rstrip() + "..."
    return value


def _bounded_location(location: object) -> str:
    if isinstance(location, tuple):
        path = ".".join(str(part) for part in location)
    else:
        path = str(location)
    path = path or "<root>"
    if len(path) > _MAX_LOCATION_LENGTH:
        path = path[: _MAX_LOCATION_LENGTH - 3] + "..."
    return path
