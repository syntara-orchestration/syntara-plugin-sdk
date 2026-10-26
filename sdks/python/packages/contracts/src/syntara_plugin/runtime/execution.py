"""Transport-independent execution for trusted typed plugin steps."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from syntara_plugin.contracts import (
    canonical_json_bytes,
    canonical_json_digest,
)

from .abi import FailureCode
from .bindings import (
    StepBinding,
    StepBindingError,
    _thaw_json_document,
    validate_executable_step_binding,
)
from .context import ExecutionContext
from .outcome import ErrorCategory, ErrorMessage, StandardOutputWrapper, _bounded_message
from .validation import StepInputValidationError, validate_step_inputs

_INVALID_STEP_RESPONSE = "The plugin step returned an invalid response."
_UNEXPECTED_STEP_FAILURE = "The plugin step failed unexpectedly."
_UNTRUSTED_BINDING = "The plugin step is not bound to a trusted installed descriptor."
_SCHEMA_DIGEST_MISMATCH = (
    "The runtime context output schema digest does not match the declared schema."
)


class StepExecutionError(RuntimeError):
    """A safe SDK execution failure for which no result wrapper is available."""

    def __init__(self, safe_message: str = _UNEXPECTED_STEP_FAILURE) -> None:
        self.safe_message = _bounded_message(
            safe_message,
            fallback=_UNEXPECTED_STEP_FAILURE,
        )
        super().__init__(self.safe_message)


class StepFailure(Exception):
    """A deliberate, author-facing step failure that produces an outcome wrapper."""

    def __init__(
        self,
        status_code: int,
        status_message: str,
        error_message: str,
        *,
        code: str = "STEP_FAILED",
        category: ErrorCategory = ErrorCategory.BUSINESS,
        retryable: bool = False,
    ) -> None:
        if type(status_code) is not int or not 1 <= status_code <= 4_294_967_295:
            raise ValueError("StepFailure status_code must be a nonzero unsigned 32-bit integer")
        if not isinstance(code, str) or not code.isascii() or not code.replace("_", "A").isalnum():
            raise ValueError("StepFailure code must be an uppercase error identifier")
        if not code[0:1].isalpha() or code != code.upper() or len(code) > 128:
            raise ValueError("StepFailure code must be an uppercase error identifier")
        self.status_code = status_code
        self.status_message = _bounded_message(status_message, fallback="The step failed.")
        self.error_message = _bounded_message(error_message, fallback="The step failed.")
        self.code = code
        self.category = ErrorCategory(category)
        if not isinstance(retryable, bool):
            raise TypeError("StepFailure retryable must be a boolean")
        self.retryable = retryable
        super().__init__(self.error_message)


class StepExecutor:
    """Execute a trusted typed step from raw JSON input.

    Input validation is selected by the binding's concrete Step kind before the
    typed hook runs. The executor itself does not own Step-type-specific contracts.
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
    ) -> StandardOutputWrapper[Mapping[str, Any]]:
        """Validate raw input, invoke the typed hook once, and wrap its result."""

        try:
            validated_inputs = validate_step_inputs(
                inputs,
                self._binding.descriptor.input_schema,
                self._binding.step.input_model(),
                self._binding.descriptor.input_schema_source,
            )
        except StepInputValidationError as error:
            return StandardOutputWrapper.failure(
                ErrorMessage(
                    category=ErrorCategory.VALIDATION,
                    code=FailureCode.INVALID_INPUT.value,
                    message=error.safe_message,
                )
            )

        unexpected_step_failure = False
        output: Any = None
        try:
            output = self._binding.step.execute(validated_inputs, context)
        except StepFailure as error:
            return StandardOutputWrapper.failure(
                status_code=error.status_code,
                status_message=error.status_message,
                error_message=error.error_message,
                code=error.code,
                category=error.category,
                retryable=error.retryable,
            )
        except Exception:
            unexpected_step_failure = True
        if unexpected_step_failure:
            raise StepExecutionError(_UNEXPECTED_STEP_FAILURE)

        output_model = self._binding.step.output_model()
        if not isinstance(output, output_model):
            raise StepExecutionError(_INVALID_STEP_RESPONSE)
        invalid_output = False
        result: Any = None
        try:
            result = output.model_dump(mode="json")
        except Exception:
            invalid_output = True
        if invalid_output:
            raise StepExecutionError(_INVALID_STEP_RESPONSE)
        if not isinstance(result, Mapping):
            raise StepExecutionError(_INVALID_STEP_RESPONSE)
        self._validate_result(result)
        return StandardOutputWrapper.success(result)

    def _validate_result(self, result: Mapping[str, Any]) -> None:
        """Validate serialization and output-schema conformance without leaking details."""

        invalid_result = False
        try:
            schema_errors = sorted(
                Draft202012Validator(
                    _thaw_json_document(self._binding.descriptor.output_schema),
                    format_checker=FormatChecker(),
                ).iter_errors(result),
                key=lambda error: list(error.path),
            )
            if schema_errors:
                invalid_result = True
            else:
                canonical_json_bytes(result)
        except Exception:
            invalid_result = True
        if invalid_result:
            raise StepExecutionError(_INVALID_STEP_RESPONSE)


def invoke(
    step: StepBinding,
    input: Mapping[str, Any],
    context: ExecutionContext,
) -> StandardOutputWrapper[Mapping[str, Any]]:
    """Invoke one typed step through its trusted descriptor and schema contract.

    Invalid inputs and author-raised ``StepFailure`` values produce wrappers.
    SDK failures, including invalid output, an untrusted binding, digest mismatch,
    or unexpected exceptions, raise
    ``StepExecutionError`` and therefore have no wrapper.
    """

    untrusted_binding = False
    executor: StepExecutor | None = None
    try:
        executor = StepExecutor(step)
    except Exception:
        untrusted_binding = True
    if untrusted_binding or executor is None:
        raise StepExecutionError(_UNTRUSTED_BINDING)

    execution_error: StepExecutionError | None = None
    wrapper: StandardOutputWrapper[Mapping[str, Any]] | None = None
    try:
        _validate_context_output_digest(step, context)
        wrapper = executor.execute(input, context)
    except StepExecutionError as error:
        execution_error = StepExecutionError(error.safe_message)
    except Exception:
        execution_error = StepExecutionError(_UNEXPECTED_STEP_FAILURE)
    if execution_error is not None:
        raise execution_error
    if wrapper is None:
        raise StepExecutionError(_UNEXPECTED_STEP_FAILURE)
    return wrapper


def _validate_context_output_digest(
    binding: StepBinding,
    context: ExecutionContext,
) -> None:
    """Check any runtime-context digest against the trusted applied schema."""

    invalid_context = False
    expected = ""
    supplied: Any = None
    try:
        expected = canonical_json_digest(
            _thaw_json_document(binding.descriptor.output_schema)
        )
        supplied = context.runtime_context.get("outputSchemaDigest")
    except Exception:
        invalid_context = True
    if invalid_context:
        raise StepExecutionError(_UNTRUSTED_BINDING)
    if supplied is not None and supplied != expected:
        raise StepExecutionError(_SCHEMA_DIGEST_MISMATCH)
