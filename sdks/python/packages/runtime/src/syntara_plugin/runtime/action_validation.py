"""Installed-Action input validation kept outside the generic executor."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ValidationError

from .bindings import _thaw_json_document
from .validation import (
    StepInputValidationError,
    format_validation_details,
    format_validation_location,
)


class ActionInputValidationError(StepInputValidationError):
    """An invalid Action input with a caller-safe diagnostic message."""


def validate_action_inputs(
    inputs: object,
    input_schema: Mapping[str, Any],
    input_model: type[BaseModel],
) -> BaseModel:
    """Validate raw Action input and construct its registered Pydantic model."""

    if not isinstance(inputs, Mapping):
        raise ActionInputValidationError("input: must be a JSON object")
    payload = dict(inputs)
    if not _is_json_compatible(payload):
        raise ActionInputValidationError("input: must contain only JSON-compatible values")

    schema_validator = Draft202012Validator(
        _thaw_json_document(input_schema), format_checker=FormatChecker()
    )
    try:
        schema_errors = sorted(
            schema_validator.iter_errors(payload),
            key=lambda error: (tuple(error.schema_path), str(error.validator)),
        )
    except Exception as error:
        raise ActionInputValidationError(
            "The installed input schema could not be evaluated."
        ) from error
    if schema_errors:
        raise ActionInputValidationError(
            "The input failed schema validation: "
            + format_validation_details([_schema_error_detail(error) for error in schema_errors])
        )

    try:
        # Validate in Pydantic's JSON mode: the manifest contract describes
        # JSON values, so standard representations such as date-time strings
        # must remain valid while Python-side coercion stays disabled.
        payload_json = json.dumps(payload, allow_nan=False, separators=(",", ":"))
        return input_model.model_validate_json(payload_json, strict=True)
    except ValidationError as error:
        raise ActionInputValidationError(
            "The input failed model validation: " + _pydantic_details(error)
        ) from error
    except Exception as error:
        raise ActionInputValidationError("The input failed model construction.") from error


def _pydantic_details(error: ValidationError) -> str:
    return format_validation_details(
        [
            f"{format_validation_location(item['loc'])}: {item['type']}"
            for item in error.errors()
        ]
    )


def _schema_error_detail(error: JsonSchemaValidationError) -> str:
    location = format_validation_location(tuple(error.schema_path))
    if error.validator == "required" and isinstance(error.validator_value, list):
        if isinstance(error.instance, dict):
            missing_keys = [key for key in error.validator_value if key not in error.instance]
        else:
            missing_keys = list(error.validator_value)
        missing = format_validation_location(
            ", ".join(str(key) for key in missing_keys)
        )
        return f"{location}: required ({missing})"
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
