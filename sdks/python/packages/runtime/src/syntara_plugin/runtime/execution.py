"""Transport-independent raw-input execution for typed plugin steps."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .abi import FailureCode
from .bindings import StepBinding, StepBindingError, validate_executable_step_binding
from .context import ExecutionContext
from .outcome import ErrorMessage, StandardOutputWrapper
from .validation import StepInputValidationError, validate_step_inputs


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
            return StandardOutputWrapper(
                status=1,
                error=ErrorMessage(
                    code=FailureCode.INVALID_INPUT.value,
                    message=error.safe_message,
                ),
            )

        output = self._binding.step.execute(validated_inputs, context)
        output_model = self._binding.step.output_model()
        if not isinstance(output, output_model):
            raise StepExecutionError("The plugin step returned an invalid response.")
        return StandardOutputWrapper(status=0, output=output.model_dump(mode="json"))
