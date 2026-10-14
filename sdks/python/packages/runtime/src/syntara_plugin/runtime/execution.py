"""Transport-independent raw-input execution for installed plugin Actions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .abi import FailureCode
from .bindings import StepBinding, StepBindingError, StepTaxonomy
from .context import ExecutionContext
from .outcome import ErrorMessage, StandardOutputWrapper
from .validation import ActionInputValidationError, validate_action_inputs


class StepExecutionError(RuntimeError):
    """Raised when a valid Action invocation cannot produce a typed result."""


class StepExecutor:
    """Execute a typed step from raw JSON input.

    Installed Actions use their manifest schema. Other taxologies retain the
    implementation-derived Pydantic schema for backward compatibility.
    """

    def __init__(self, binding: StepBinding) -> None:
        if (
            binding.descriptor.taxonomy is StepTaxonomy.ACTION
            and binding.descriptor.input_schema_source != "manifest"
        ):
            message = (
                "StepExecutor requires a descriptor created with "
                "StepDescriptor.for_installed_action"
            )
            raise StepBindingError(message)
        self._binding = binding

    @property
    def step_identity(self) -> str:
        return self._binding.descriptor.step_identity

    def execute(
        self, inputs: Mapping[str, Any], context: ExecutionContext
    ) -> StandardOutputWrapper:
        """Validate raw input, invoke the typed hook once, and wrap its result."""

        try:
            validated_inputs = validate_action_inputs(
                inputs,
                self._binding.descriptor.input_schema,
                self._binding.step.input_model(),
            )
        except ActionInputValidationError as error:
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
            raise StepExecutionError("The plugin action returned an invalid response.")
        return StandardOutputWrapper(status=0, output=output.model_dump(mode="json"))
