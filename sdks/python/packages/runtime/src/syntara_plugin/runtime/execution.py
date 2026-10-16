"""Transport-independent raw-input execution for typed plugin steps."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from syntara_plugin.contracts import CanonicalJsonError, canonical_json_bytes

from .abi import FailureCode
from .bindings import StepBinding, StepBindingError, validate_executable_step_binding
from .context import ExecutionContext
from .outcome import ErrorMessage, StandardOutputWrapper
from .validation import StepInputValidationError, validate_step_inputs

_INVALID_STEP_RESPONSE = "The plugin step returned an invalid response."


class StepExecutionError(RuntimeError):
    """Raised when a valid invocation cannot produce a typed result."""


class StepExecutor:
    """Execute a typed step from raw JSON input.

    Input validation is selected by the binding's taxonomy before the typed
    hook runs. The executor itself does not own taxonomy-specific contracts.
    """

    def __init__(self, binding: StepBinding) -> None:
        if type(binding) is not StepBinding:
            message = "StepExecutor requires an exact StepBinding"
            raise StepBindingError(message)
        validate_executable_step_binding(binding.descriptor, binding.step)
        self._binding = binding

    @property
    def step_identity(self) -> str:
        return self._binding.descriptor.step_identity

    def execute(
        self, inputs: Mapping[str, Any], context: ExecutionContext
    ) -> StandardOutputWrapper:
        """Validate raw input, invoke the typed hook once, and wrap its result."""

        try:
            validated_inputs = validate_step_inputs(
                self._binding.descriptor.taxonomy,
                inputs,
                self._binding.descriptor.input_schema,
                self._binding.step.input_model(),
            )
        except StepInputValidationError as error:
            return StandardOutputWrapper.failure(
                ErrorMessage(
                    code=FailureCode.INVALID_INPUT.value,
                    message=error.safe_message,
                )
            )

        output = self._binding.step.execute(validated_inputs, context)
        output_model = self._binding.step.output_model()
        if not isinstance(output, output_model):
            raise StepExecutionError(_INVALID_STEP_RESPONSE)
        result = output.model_dump(mode="json")
        self._validate_result(result)
        return StandardOutputWrapper.success(result)

    def _validate_result(self, result: Mapping[str, Any]) -> None:
        """Validate the serialized business result before declaring success.

        The result is checked against the declared output schema and must be
        canonical-JSON encodable. The inline-size bound is deliberately not
        enforced here: a large but valid result is a transport-representation
        concern owned by the container adapter, which externalizes it to an
        object-store reference. Direct, in-process execution has no such bound.
        """

        schema_errors = sorted(
            Draft202012Validator(
                self._binding.descriptor.output_schema, format_checker=FormatChecker()
            ).iter_errors(result),
            key=lambda error: list(error.path),
        )
        if schema_errors:
            raise StepExecutionError(_INVALID_STEP_RESPONSE)
        try:
            canonical_json_bytes(result)
        except CanonicalJsonError as error:
            raise StepExecutionError(_INVALID_STEP_RESPONSE) from error
