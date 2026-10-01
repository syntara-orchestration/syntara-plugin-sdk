"""Base step classes for Syntara workflow steps."""

from __future__ import annotations

import json
import traceback
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field, ValidationError

from syntara_sdk.context import ExecutionContext

#: Shortest ``redact``-flagged value the echo check will search for. Values
#: below this length collide with ordinary output too often, and the execution
#: plane remains the authoritative scrubbing boundary (R5/AC-4).
MIN_SENSITIVE_MATCH_LENGTH = 4


def _iter_strings(value: Any) -> Iterator[str]:
    """Yield every string leaf in a nested input value."""

    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_strings(item)


class StandardOutputWrapper(BaseModel):
    """Immutable output envelope for all step executions.

    This standardized structure ensures backwards compatibility and enables
    stable template expressions like ${task.Result.stdout} across plugin upgrades.
    """

    Result: Any = Field(
        ...,
        description="Primary task return payload (type varies by step)",
    )
    StatusCode: int = Field(
        default=0,
        ge=0,
        le=255,
        description="Execution status (0 = success, non-zero = failure)",
    )
    StatusMessage: str = Field(
        default="",
        max_length=500,
        description="Human-readable execution summary",
    )
    ErrorMessage: str = Field(
        default="",
        max_length=10000,
        description="Detailed error diagnostics (populated only on failure)",
    )


# Type variables for generic input/output types
TInput = TypeVar("TInput", bound=BaseModel)
TOutput = TypeVar("TOutput", bound=BaseModel)


class BaseStep(ABC, Generic[TInput, TOutput]):
    """Abstract base for all Syntara steps.

    Provides typed input validation, automatic StandardOutputWrapper wrapping,
    and error handling. Subclasses implement the `run()` method with their
    business logic.

    Type Parameters:
        TInput: Pydantic model for step inputs
        TOutput: Pydantic model for step outputs

    Example:
        ```python
        class MyInput(BaseModel):
            url: str
            method: str = "GET"

        class MyOutput(BaseModel):
            status_code: int
            body: str

        class MyStep(ActionStep[MyInput, MyOutput]):
            def run(self, inputs: MyInput, context: ExecutionContext) -> MyOutput:
                # Implementation
                return MyOutput(status_code=200, body="OK")
        ```
    """

    def __init__(self, input_model: type[TInput], output_model: type[TOutput]) -> None:
        """Initialize base step.

        Args:
            input_model: Pydantic model class for inputs
            output_model: Pydantic model class for outputs
        """
        self.input_model = input_model
        self.output_model = output_model
        self._redact_fields = self._discover_redact_fields(input_model)

    @staticmethod
    def _discover_redact_fields(input_model: type[BaseModel]) -> frozenset[str]:
        """Collect input fields the schema flags for redaction.

        Mirrors the manifest contract: a property is sensitive when it carries
        ``redact: true``. Declare it with
        ``Field(json_schema_extra={"redact": True})``.
        """

        try:
            schema = input_model.model_json_schema()
        except Exception:  # noqa: BLE001 - a model that cannot emit a JSON
            # schema simply has no declarable redact fields; never block init.
            return frozenset()
        return frozenset(
            name
            for name, prop in (schema.get("properties") or {}).items()
            if isinstance(prop, dict) and prop.get("redact") is True
        )

    def _find_echoed_fields(self, inputs: TInput, output: TOutput) -> list[str]:
        """Return the ``redact``-flagged input fields echoed into ``output``.

        Defense in depth for R5/AC-4. The platform dispatcher and execution
        plane own scrubbing and remain the authoritative boundary; this only
        validates that a step does not return its own sensitive input.
        """

        if not self._redact_fields:
            return []
        data = inputs.model_dump()
        try:
            rendered = json.dumps(output.model_dump(), default=str)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            rendered = str(output)
        echoed = [
            field
            for field in sorted(self._redact_fields)
            if any(
                len(text) >= MIN_SENSITIVE_MATCH_LENGTH and text in rendered
                for text in _iter_strings(data.get(field))
            )
        ]
        return echoed

    @abstractmethod
    def run(self, inputs: TInput, context: ExecutionContext) -> TOutput:
        """Execute the step's business logic.

        This is the main entrypoint that subclasses must implement.

        Args:
            inputs: Validated, typed input parameters
            context: Execution context with logging and secrets

        Returns:
            Typed output result

        Raises:
            Exception: Any exception will be caught and wrapped in StandardOutputWrapper
        """
        pass

    def execute_raw(
        self,
        raw_inputs: dict[str, Any],
        context: ExecutionContext | None = None,
    ) -> StandardOutputWrapper:
        """Execute step with raw dictionary inputs and return wrapped output.

        This is the entrypoint called by the execution plane. It:
        1. Validates raw inputs against TInput schema
        2. Creates execution context if not provided
        3. Calls run() with typed inputs
        4. Wraps result in StandardOutputWrapper
        5. Catches and wraps any exceptions

        Args:
            raw_inputs: Raw input dictionary from workflow engine
            context: Execution context (created if not provided)

        Returns:
            StandardOutputWrapper with result or error
        """
        if context is None:
            context = ExecutionContext(step_name=self.__class__.__name__)

        context.log_execution_start(raw_inputs)

        try:
            # Validate and parse inputs
            try:
                inputs = self.input_model.model_validate(raw_inputs)
            except ValidationError as e:
                error_msg = self._format_validation_error(e)
                context.log_execution_error(e)
                return StandardOutputWrapper(
                    Result=None,
                    StatusCode=1,
                    StatusMessage="Input validation failed",
                    ErrorMessage=error_msg,
                )

            # Execute step logic
            output = self.run(inputs, context)

            # Validate output
            if not isinstance(output, self.output_model):
                output = self.output_model.model_validate(output)

            # R5/AC-4: refuse to emit an output echoing a sensitive input.
            echoed = self._find_echoed_fields(inputs, output)
            if echoed:
                return StandardOutputWrapper(
                    Result=None,
                    StatusCode=1,
                    StatusMessage="Sensitive input echoed in output",
                    ErrorMessage=(
                        "Step output contains the value of redact-flagged "
                        f"input(s): {', '.join(echoed)}. Sensitive inputs must "
                        "not be returned in step results."
                    ),
                )

            # Wrap in StandardOutputWrapper
            result = StandardOutputWrapper(
                Result=output.model_dump(),
                StatusCode=0,
                StatusMessage=f"Execution completed successfully",
                ErrorMessage="",
            )

            context.log_execution_complete(0, "Success")
            return result

        except Exception as e:
            # Catch any unhandled exceptions
            error_msg = self._format_exception(e)
            context.log_execution_error(e)

            return StandardOutputWrapper(
                Result=None,
                StatusCode=1,
                StatusMessage=f"Execution failed: {type(e).__name__}",
                ErrorMessage=error_msg,
            )

    def _format_validation_error(self, error: ValidationError) -> str:
        """Format Pydantic validation errors for ErrorMessage field.

        Args:
            error: Pydantic validation error

        Returns:
            Formatted error message with field-level details
        """
        errors = []
        for err in error.errors():
            loc = ".".join(str(x) for x in err["loc"])
            msg = err["msg"]
            errors.append(f"{loc}: {msg}")
        return "Input validation errors:\n" + "\n".join(errors)

    def _format_exception(self, error: Exception) -> str:
        """Format exception with stack trace for ErrorMessage field.

        Args:
            error: Exception that occurred

        Returns:
            Formatted error message with stack trace
        """
        tb = traceback.format_exc()
        return f"{type(error).__name__}: {error}\n\nStack trace:\n{tb}"


class ActionStep(BaseStep[TInput, TOutput]):
    """Base class for action steps (domain and API integrations).

    Action steps:
    - Execute in isolated containers
    - Can access API credentials via platform-managed references
    - Typically make external HTTP requests or interact with third-party services

    Examples: http_request, github_issue, slack_message
    """

    pass


class TaskStep(BaseStep[TInput, TOutput]):
    """Base class for task steps (atomic compute operations).

    Task steps:
    - Execute in isolated containers
    - Run scripts, process data, or perform computations
    - Can access infrastructure credentials via platform-managed references

    Examples: script_executor, data_transformer
    """

    pass


class WorkflowStep(BaseStep[TInput, TOutput]):
    """Base class for workflow steps (in-memory control flow logic).

    Workflow steps:
    - Implement control flow (loops, conditions, switches)
    - Typically placed in the control plane by the Execution Plane, though
      placement is derived by the platform and not declared by the manifest

    Examples: loop, condition, switch, converge, subworkflow_call
    """

    pass


class TriggerStep(BaseStep[TInput, TOutput]):
    """Base class for trigger steps (event entry points).

    Trigger steps:
    - Start workflows in response to events
    - Includes child-side subworkflow_trigger for Reference-mode eligibility

    Examples: webhook, schedule, manual, subworkflow_trigger
    """

    pass
