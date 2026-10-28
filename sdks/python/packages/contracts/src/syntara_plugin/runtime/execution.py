"""Transport-independent execution for trusted typed plugin steps."""

from __future__ import annotations

import traceback
from collections.abc import Mapping
from typing import Any, Literal, cast

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
from .outcome import (
    ErrorCategory,
    ErrorResult,
    StandardOutputWrapper,
    bounded_message,
    error_result_base_schema,
)
from .validation import StepInputValidationError, validate_step_inputs

_INVALID_STEP_RESPONSE = "The plugin step returned an invalid response."
_UNEXPECTED_STEP_FAILURE = "The plugin step did not complete."
_UNTRUSTED_BINDING = "The plugin step is not bound to a trusted installed descriptor."
_SCHEMA_DIGEST_MISMATCH = (
    "The runtime context output schema digest does not match the declared schema."
)
_UNSET = object()
_UNSET_CODE: str = cast(str, _UNSET)
_UNSET_CATEGORY: ErrorCategory = cast(ErrorCategory, _UNSET)
_UNSET_RETRYABLE: bool = cast(bool, _UNSET)


class StepExecutionError(RuntimeError):
    """A safe SDK failure without a result wrapper and with execution certainty.

    ``not_started`` means the typed step hook was not entered. ``unknown`` means
    execution began, so the caller cannot know whether side effects occurred.
    """

    def __init__(
        self,
        safe_message: str = _UNEXPECTED_STEP_FAILURE,
        *,
        failure_code: FailureCode = FailureCode.ABI_VIOLATION,
        detail: str | None = None,
        outcome: Literal["not_started", "unknown"] = "unknown",
    ) -> None:
        self.safe_message = bounded_message(
            safe_message,
            fallback=_UNEXPECTED_STEP_FAILURE,
        )
        self.failure_code = failure_code
        self.detail = detail
        if outcome not in {"not_started", "unknown"}:
            raise ValueError("outcome must be not_started or unknown")
        self.outcome = outcome
        super().__init__(self.safe_message)


class StepFailure(Exception):
    """A deliberate failure that produces a wrapper with one error source.

    Use either ``error_result`` or the legacy message plus its metadata fields.
    """

    def __init__(
        self,
        status_code: int,
        status_message: str,
        error_message: str | None = None,
        *,
        error_result: ErrorResult | None = None,
        code: str = _UNSET_CODE,
        category: ErrorCategory = _UNSET_CATEGORY,
        retryable: bool = _UNSET_RETRYABLE,
    ) -> None:
        if type(status_code) is not int or not 1 <= status_code <= 4_294_967_295:
            raise ValueError("StepFailure status_code must be a nonzero unsigned 32-bit integer")
        self.status_code = status_code
        self.status_message = bounded_message(status_message, fallback="The step failed.")
        if error_result is not None and error_message is not None:
            raise ValueError("provide either error_result or error_message")
        if error_result is not None and any(
            value is not _UNSET for value in (code, category, retryable)
        ):
            raise ValueError("provide error_result without code, category, or retryable overrides")
        if error_result is None:
            if error_message is None:
                raise ValueError("StepFailure requires an error result")
            effective_code = "STEP_FAILED" if code is _UNSET else code
            effective_category = ErrorCategory.BUSINESS if category is _UNSET else category
            effective_retryable = False if retryable is _UNSET else retryable
            if (
                not isinstance(effective_code, str)
                or not effective_code.isascii()
                or not effective_code.replace("_", "A").isalnum()
            ):
                raise ValueError("StepFailure code must be an uppercase error identifier")
            if (
                not effective_code[0:1].isalpha()
                or effective_code != effective_code.upper()
                or len(effective_code) > 128
            ):
                raise ValueError("StepFailure code must be an uppercase error identifier")
            if not isinstance(effective_retryable, bool):
                raise TypeError("StepFailure retryable must be a boolean")
            error_result = ErrorResult(
                category=effective_category,
                code=effective_code,
                message=error_message,
                retryable=effective_retryable,
            )
        self.error_result = error_result
        self.error_message = error_result.message
        self.code = error_result.code
        self.category = error_result.category
        self.retryable = error_result.retryable
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
            return self._failure(
                ErrorResult(
                    category=ErrorCategory.VALIDATION,
                    code=FailureCode.INVALID_INPUT.value,
                    message=error.safe_message,
                ),
                outcome="not_started",
            )
        except Exception as error:
            raise StepExecutionError(
                _UNEXPECTED_STEP_FAILURE,
                detail="".join(traceback.format_exception(error)).rstrip(),
                outcome="not_started",
            ) from None

        unexpected_step_error: str | None = None
        output: Any = None
        try:
            output = self._binding.step.execute(validated_inputs, context)
        except StepFailure as error:
            return self._failure(
                error.error_result,
                status_code=error.status_code,
                status_message=error.status_message,
            )
        except Exception as error:
            unexpected_step_error = "".join(traceback.format_exception(error)).rstrip()
        if unexpected_step_error is not None:
            raise StepExecutionError(
                _UNEXPECTED_STEP_FAILURE,
                failure_code=FailureCode.OUTCOME_UNKNOWN,
                detail=unexpected_step_error,
            )

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

    def _failure(
        self,
        error: ErrorResult,
        *,
        status_code: int = 1,
        status_message: str | None = None,
        outcome: Literal["not_started", "unknown"] = "unknown",
    ) -> StandardOutputWrapper[Mapping[str, Any]]:
        """Apply both the SDK base and the effective installed error contract."""

        try:
            document = error.model_dump(mode="json")
            canonical_json_bytes(document)
            for schema in (error_result_base_schema(), self._binding.descriptor.error_schema):
                validator = Draft202012Validator(
                    _thaw_json_document(schema), format_checker=FormatChecker()
                )
                if not validator.is_valid(document):
                    raise ValueError("invalid error result")
        except Exception:
            raise StepExecutionError(_INVALID_STEP_RESPONSE, outcome=outcome) from None
        return StandardOutputWrapper.failure(
            error, status_code=status_code, status_message=status_message
        )

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
        raise StepExecutionError(_UNTRUSTED_BINDING, outcome="not_started")

    execution_error: StepExecutionError | None = None
    wrapper: StandardOutputWrapper[Mapping[str, Any]] | None = None
    try:
        _validate_context_output_digest(step, context)
        wrapper = executor.execute(input, context)
    except StepExecutionError as error:
        execution_error = error
    except Exception as error:
        raise StepExecutionError(
            _UNEXPECTED_STEP_FAILURE,
            detail="".join(traceback.format_exception(error)).rstrip(),
        ) from error
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
        raise StepExecutionError(_UNTRUSTED_BINDING, outcome="not_started")
    if supplied is not None and supplied != expected:
        raise StepExecutionError(_SCHEMA_DIGEST_MISMATCH, outcome="not_started")
