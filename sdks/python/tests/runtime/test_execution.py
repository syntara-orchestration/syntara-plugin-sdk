"""Tests for the shared raw-input step execution boundary."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import datetime
from threading import Event
from typing import Any, Literal

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, model_validator
from syntara_plugin.contracts import canonical_json_digest
from syntara_plugin.runtime import (
    ActionStep,
    BaseStep,
    ContainerInvocation,
    ContainerInvocationError,
    ControlStep,
    ExecutionContext,
    FailureCode,
    InstalledStepManifest,
    StandardOutputWrapper,
    StepBinding,
    StepBindingError,
    StepBindings,
    StepDescriptor,
    StepExecutionError,
    StepExecutor,
    StepKind,
    TriggerStep,
    container_step_for_binding,
    container_step_for_installed_manifest,
)


class ExampleInput(BaseModel):
    message: str
    count: int = 2
    mode: str = "fast"

    @model_validator(mode="after")
    def validate_mode(self) -> ExampleInput:
        if self.mode not in {"fast", "slow"}:
            raise ValueError("invalid mode")
        return self


class ExampleOutput(BaseModel):
    message: str
    invocation_id: str
    workflow_run_id: str | None
    count: int
    mode: str


class ExampleStep(ActionStep[ExampleInput, ExampleOutput]):
    def __init__(self) -> None:
        self.run_calls = 0

    def run(self, inputs: ExampleInput, context: Any) -> ExampleOutput:
        self.run_calls += 1
        return ExampleOutput(
            message=inputs.message,
            invocation_id=context.invocation_id,
            workflow_run_id=context.workflow_run_id,
            count=inputs.count,
            mode=inputs.mode,
        )


class DateTimeInput(BaseModel):
    occurred_at: datetime


class DateTimeOutput(BaseModel):
    occurred_at: datetime


class DateTimeActionStep(ActionStep[DateTimeInput, DateTimeOutput]):
    def __init__(self) -> None:
        self.run_calls = 0

    def run(self, inputs: DateTimeInput, context: Any) -> DateTimeOutput:
        self.run_calls += 1
        return DateTimeOutput(occurred_at=inputs.occurred_at)


class FailingModelStep(ExampleStep):
    def run(self, inputs: ExampleInput, context: Any) -> ExampleOutput:
        raise AssertionError("run must not be called")


class ControlExampleOutput(BaseModel):
    message: str
    invocation_id: str
    count: int
    mode: str


class ControlExampleStep(ControlStep[ExampleInput, ControlExampleOutput]):
    def __init__(self) -> None:
        self.run_calls = 0
        self.last_inputs: ExampleInput | None = None
        self.last_context: ExecutionContext | None = None

    def run(self, inputs: ExampleInput, context: Any) -> ControlExampleOutput:
        self.run_calls += 1
        self.last_inputs = inputs
        self.last_context = context
        return ControlExampleOutput(
            message=inputs.message,
            invocation_id=context.invocation_id,
            count=inputs.count,
            mode=inputs.mode,
        )


class TriggerExampleStep(TriggerStep[ExampleInput, ControlExampleOutput]):
    def __init__(self) -> None:
        self.run_calls = 0
        self.last_inputs: ExampleInput | None = None
        self.last_context: ExecutionContext | None = None

    def run(self, inputs: ExampleInput, context: Any) -> ControlExampleOutput:
        self.run_calls += 1
        self.last_inputs = inputs
        self.last_context = context
        return ControlExampleOutput(
            message=inputs.message,
            invocation_id=context.invocation_id,
            count=inputs.count,
            mode=inputs.mode,
        )


class UnexpectedControlOutput(BaseModel):
    value: str


class InvalidOutputControlStep(ControlStep[ExampleInput, ControlExampleOutput]):
    def __init__(self) -> None:
        self.run_calls = 0

    def run(self, inputs: ExampleInput, context: Any) -> ControlExampleOutput:
        self.run_calls += 1
        return UnexpectedControlOutput(value="not-the-registered-output")  # type: ignore[return-value]


class InvalidOutputTriggerStep(TriggerStep[ExampleInput, ControlExampleOutput]):
    def __init__(self) -> None:
        self.run_calls = 0

    def run(self, inputs: ExampleInput, context: Any) -> ControlExampleOutput:
        self.run_calls += 1
        return UnexpectedControlOutput(value="not-the-registered-output")  # type: ignore[return-value]


class ConfiguredControlInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_to_upper=True)

    message: str
    count: int = 2
    mode: Literal["fast", "slow"] = "fast"

    @model_validator(mode="after")
    def require_positive_slow_count(self) -> ConfiguredControlInput:
        if self.mode == "slow" and self.count < 1:
            raise ValueError("slow mode requires a positive count")
        return self


class ConfiguredControlStep(ControlStep[ConfiguredControlInput, ControlExampleOutput]):
    def __init__(self) -> None:
        self.run_calls = 0

    def run(self, inputs: ConfiguredControlInput, context: Any) -> ControlExampleOutput:
        self.run_calls += 1
        return ControlExampleOutput(
            message=inputs.message,
            invocation_id=context.invocation_id,
            count=inputs.count,
            mode=inputs.mode,
        )


def _input_schema() -> dict[str, object]:
    return {
        "type": "object",
        "required": ["message"],
        "properties": {
            "message": {"type": "string"},
            "count": {"type": "integer", "default": 2},
            "mode": {"enum": ["fast", "slow"], "default": "fast"},
        },
        "additionalProperties": False,
    }


def _installed_step(
    input_schema: dict[str, object],
    output_schema: dict[str, object] | None = None,
    step_class: type[BaseStep[Any, Any]] = ExampleStep,
    step_identity: str = "acme.plugin.example",
    configuration_schema: dict[str, object] | None = None,
) -> InstalledStepManifest:
    kind = (
        StepKind.ACTION
        if issubclass(step_class, ActionStep)
        else StepKind.CONTROL
        if issubclass(step_class, ControlStep)
        else StepKind.TRIGGER
    )
    manifest = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": kind.value,
        "metadata": {
            "name": "example",
            "displayName": "Example",
            "description": "Example action",
        },
        "spec": {
            "runtime": {
                "kind": "custom-runner",
                "entryPoint": f"{step_class.__module__}:{step_class.__qualname__}",
            },
            "input": {"kind": "inline", "value": input_schema},
            "output": {"kind": "inline", "value": output_schema if output_schema is not None else {}},
        },
    }
    if kind is StepKind.CONTROL:
        manifest["spec"]["invocation"] = {"kind": "orchestration"}
    elif kind is StepKind.TRIGGER:
        manifest["spec"].update(
            {
                "driver": "webhook.v1",
                "configuration": {"kind": "inline", "value": {"type": "object"}},
            }
        )
    if configuration_schema is not None:
        manifest["spec"]["configuration"] = {
            "kind": "inline",
            "value": configuration_schema,
        }
    return InstalledStepManifest.from_manifest(
        step_identity, manifest, {}
    )


def _bind(step: ExampleStep, output_schema: dict[str, object] | None = None) -> StepBindings:
    bindings = StepBindings()
    descriptor = StepDescriptor.for_installed_step(
        step, _installed_step(_input_schema(), output_schema, step_class=type(step))
    )
    bindings.bind(descriptor, step)
    return bindings


def _bind_installed(
    bindings: StepBindings,
    identity: str,
    step: BaseStep[Any, Any],
    *,
    input_schema: dict[str, object] | None = None,
    output_schema: dict[str, object] | None = None,
) -> None:
    descriptor = StepDescriptor.for_installed_step(
        step,
        _installed_step(
            input_schema if input_schema is not None else _input_schema(),
            output_schema if output_schema is not None else step.output_model().model_json_schema(),
            step_class=type(step),
            step_identity=identity,
        ),
    )
    bindings.bind(descriptor, step)


def _executor(step: ExampleStep, output_schema: dict[str, object] | None = None) -> StepExecutor:
    return StepExecutor(_bind(step, output_schema).binding("acme.plugin.example"))


def _context() -> ExecutionContext:
    return ExecutionContext(
        invocation_id="invoke-1",
        workflow_run_id="run-1",
        runtime_context={"environment": "test"},
    )


def _container_invocation(
    inputs: dict[str, object],
    *,
    step_revision_id: str = "acme.plugin.example",
) -> ContainerInvocation:
    return ContainerInvocation(
        invocation_id="invoke-1",
        workflow_run_id="run-1",
        step_revision_id=step_revision_id,
        inputs=inputs,
        credential_file_paths={},
        timeout_seconds=30,
        runtime_context={"environment": "test"},
    )


def test_valid_raw_input_uses_defaults_and_calls_run_once() -> None:
    step = ExampleStep()

    wrapper = _executor(step).execute({"message": "hello"}, _context())

    assert isinstance(wrapper, StandardOutputWrapper)
    assert wrapper.status == 0
    assert wrapper.error is None
    assert wrapper.output == {
        "message": "hello",
        "invocation_id": "invoke-1",
        "workflow_run_id": "run-1",
        "count": 2,
        "mode": "fast",
    }
    assert step.run_calls == 1


@pytest.mark.parametrize(
    "inputs",
    [
        {},
        {"message": "hello", "count": "3"},
        {"message": "hello", "mode": "instant"},
        {"message": "hello", "credential": "secret-value"},
    ],
)
def test_invalid_manifest_input_never_calls_run(inputs: dict[str, object]) -> None:
    step = ExampleStep()

    wrapper = _executor(step).execute(inputs, _context())

    assert wrapper.status != 0
    assert wrapper.error is not None
    assert wrapper.error.code == FailureCode.INVALID_INPUT.value
    assert "credential" not in wrapper.error.message
    assert "secret-value" not in wrapper.error.message
    assert step.run_calls == 0


def test_missing_required_input_names_the_missing_field() -> None:
    step = ExampleStep()

    wrapper = _executor(step).execute({}, _context())

    assert wrapper.error is not None
    assert "required (message)" in wrapper.error.message
    assert step.run_calls == 0


def test_non_json_payloads_are_rejected_before_model_construction() -> None:
    step = ExampleStep()

    wrapper = _executor(step).execute(
        {"message": object()},  # type: ignore[dict-item]
        _context(),
    )

    assert wrapper.status != 0
    assert wrapper.error is not None
    assert "JSON-compatible" in wrapper.error.message
    assert step.run_calls == 0


def test_manifest_validation_uses_strict_json_mode_for_json_representations() -> None:
    step = DateTimeActionStep()
    bindings = StepBindings()
    descriptor = StepDescriptor.for_installed_step(
        step,
        _installed_step(DateTimeInput.model_json_schema(), step_class=DateTimeActionStep),
    )
    bindings.bind(descriptor, step)

    wrapper = StepExecutor(bindings.binding("acme.plugin.example")).execute(
        {"occurred_at": "2025-01-01T00:00:00Z"}, _context()
    )

    assert wrapper.status == 0
    assert wrapper.output == {"occurred_at": "2025-01-01T00:00:00Z"}
    assert step.run_calls == 1


def test_action_format_constraints_are_enforced_before_the_hook() -> None:
    step = ExampleStep()
    schema = _input_schema()
    schema["properties"]["message"]["format"] = "email"  # type: ignore[index]
    bindings = StepBindings()
    descriptor = StepDescriptor.for_installed_step(step, _installed_step(schema))
    bindings.bind(descriptor, step)

    wrapper = StepExecutor(bindings.binding("acme.plugin.example")).execute(
        {"message": "not-an-email"}, _context()
    )

    assert wrapper.status != 0
    assert wrapper.error is not None
    assert "format" in wrapper.error.message
    assert step.run_calls == 0


def test_model_construction_error_is_a_safe_invalid_input() -> None:
    step = FailingModelStep()
    schema = _input_schema()
    schema["properties"]["mode"] = {"type": "string"}
    bindings = StepBindings()
    bindings.bind(
        StepDescriptor.for_installed_step(
            step, _installed_step(schema, step_class=FailingModelStep)
        ),
        step,
    )

    wrapper = StepExecutor(bindings.binding("acme.plugin.example")).execute(
        {"message": "hello", "mode": "instant"}, _context()
    )

    assert wrapper.status != 0
    assert wrapper.error is not None
    assert "mode" in wrapper.error.message
    assert "instant" not in wrapper.error.message
    assert step.run_calls == 0


def test_unexpected_model_construction_failure_is_a_safe_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct_step = ExampleStep()
    container_step = ExampleStep()

    def fail_construction(*_args: object, **_kwargs: object) -> ExampleInput:
        raise TypeError("sensitive construction failure")

    monkeypatch.setattr(
        ExampleInput, "model_validate", classmethod(fail_construction)
    )
    monkeypatch.setattr(
        ExampleInput, "model_validate_json", classmethod(fail_construction)
    )

    direct_wrapper = _executor(direct_step).execute({"message": "hello"}, _context())
    container_handler = container_step_for_installed_manifest(
        container_step,
        installed_step=_installed_step(_input_schema()),
    )
    container_result = container_handler(_container_invocation({"message": "hello"}))

    assert direct_wrapper.status != 0
    assert direct_wrapper.error is not None
    assert direct_wrapper.error.message == (
        "The input failed model construction."
    )
    assert "sensitive construction failure" not in direct_wrapper.error.message
    assert container_result.output is None
    assert container_result.status_code != 0
    assert container_result.error_code == direct_wrapper.error.code
    assert container_result.error_message == direct_wrapper.error.message
    assert direct_step.run_calls == 0
    assert container_step.run_calls == 0


def test_manifest_validation_reports_control_characters_safely() -> None:
    step = ConfiguredControlStep()
    bindings = StepBindings()
    _bind_installed(
        bindings,
        "acme.plugin.control",
        step,
        input_schema=ConfiguredControlInput.model_json_schema(),
    )

    wrapper = StepExecutor(bindings.binding("acme.plugin.control")).execute(
        {"message": "hello", "bad\nfield": "value"}, _context()
    )

    assert wrapper.status != 0
    assert wrapper.error is not None
    assert "additionalProperties" in wrapper.error.message
    assert "bad\nfield" not in wrapper.error.message
    assert step.run_calls == 0


def test_schema_evaluation_failure_is_a_safe_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct_step = ExampleStep()
    container_step = ExampleStep()
    executor = _executor(direct_step)
    container_handler = container_step_for_installed_manifest(
        container_step,
        installed_step=_installed_step(_input_schema()),
    )

    def fail_schema_evaluation(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("sensitive schema failure")

    monkeypatch.setattr(
        Draft202012Validator,
        "iter_errors",
        fail_schema_evaluation,
    )

    direct_wrapper = executor.execute({"message": "hello"}, _context())
    container_result = container_handler(_container_invocation({"message": "hello"}))

    assert direct_wrapper.status != 0
    assert direct_wrapper.error is not None
    assert direct_wrapper.error.message == (
        "The installed input schema could not be evaluated."
    )
    assert "sensitive schema failure" not in direct_wrapper.error.message
    assert container_result.output is None
    assert container_result.status_code != 0
    assert container_result.error_code == direct_wrapper.error.code
    assert container_result.error_message == direct_wrapper.error.message
    assert direct_step.run_calls == 0
    assert container_step.run_calls == 0


def test_supported_action_adapter_maps_the_invalid_input_wrapper_to_the_envelope() -> None:
    direct_step = ExampleStep()
    container_step = ExampleStep()
    invalid_input = {"message": "hello", "count": "3"}

    direct_wrapper = _executor(direct_step).execute(invalid_input, _context())
    container_handler = container_step_for_installed_manifest(
        container_step,
        installed_step=_installed_step(_input_schema()),
    )
    container_result = container_handler(_container_invocation(invalid_input))

    assert direct_wrapper.error is not None
    assert container_result.output is None
    assert container_result.status_code != 0
    assert container_result.error_code == direct_wrapper.error.code
    assert container_result.error_message == direct_wrapper.error.message
    assert container_result.output_schema_digest == canonical_json_digest({})
    assert direct_wrapper.error.code == FailureCode.INVALID_INPUT.value
    assert direct_step.run_calls == 0
    assert container_step.run_calls == 0


def test_supported_action_adapter_preserves_success_payload_and_digest() -> None:
    direct_step = ExampleStep()
    container_step = ExampleStep()
    output_schema = ExampleOutput.model_json_schema()
    valid_input = {"message": "hello", "count": 3, "mode": "slow"}

    direct_wrapper = _executor(direct_step, output_schema).execute(valid_input, _context())
    container_handler = container_step_for_installed_manifest(
        container_step,
        installed_step=_installed_step(_input_schema(), output_schema),
    )
    container_result = container_handler(_container_invocation(valid_input))

    assert direct_wrapper.status == 0
    assert container_result.output == direct_wrapper.output
    assert container_result.output_schema_digest == canonical_json_digest(output_schema)
    assert direct_step.run_calls == 1
    assert container_step.run_calls == 1


def test_container_step_rejects_a_mismatched_installed_identity() -> None:
    step = ExampleStep()
    handler = container_step_for_installed_manifest(
        step,
        installed_step=_installed_step(_input_schema()),
    )
    invocation = _container_invocation(
        {"message": "hello"}, step_revision_id="acme.plugin.other"
    )

    with pytest.raises(ContainerInvocationError) as raised:
        handler(invocation)

    assert raised.value.code is FailureCode.NOT_FOUND
    assert raised.value.outcome == "not_started"
    assert step.run_calls == 0


def test_executor_rejects_an_implementation_derived_action_binding() -> None:
    step = ExampleStep()
    bindings = StepBindings()
    bindings.bind(StepDescriptor.for_step("acme.plugin.example", step), step)
    binding = bindings.binding("acme.plugin.example")

    with pytest.raises(StepBindingError, match="installed contract"):
        StepExecutor(binding)


def test_container_execution_rejects_an_implementation_derived_action_binding() -> None:
    step = ExampleStep()
    descriptor = StepDescriptor.for_step("acme.plugin.example", step)
    binding = StepBinding(descriptor, step)

    with pytest.raises(StepBindingError, match="installed contract"):
        container_step_for_binding(binding)


def test_direct_mismatched_binding_is_rejected_before_any_typed_hook_runs() -> None:
    action_step = ExampleStep()
    control_step = ControlExampleStep()
    descriptor = StepDescriptor.for_installed_step(
        action_step, _installed_step(_input_schema())
    )
    binding = StepBinding(descriptor, control_step)

    with pytest.raises(StepBindingError, match="does not match"):
        StepExecutor(binding)

    with pytest.raises(StepBindingError, match="does not match"):
        container_step_for_binding(binding)

    assert action_step.run_calls == 0
    assert control_step.run_calls == 0


@pytest.mark.parametrize(
    ("installed_step_type", "supplied_step_type"),
    [
        (ExampleStep, DateTimeActionStep),
        (DateTimeActionStep, ExampleStep),
    ],
)
def test_same_category_action_entry_point_mismatch_is_rejected_before_hooks(
    installed_step_type: type[ActionStep[Any, Any]],
    supplied_step_type: type[ActionStep[Any, Any]],
) -> None:
    installed_implementation = installed_step_type()
    supplied_step = supplied_step_type()
    installed_step = _installed_step(
        installed_implementation.input_model().model_json_schema(),
        step_class=installed_step_type,
    )
    descriptor = StepDescriptor.for_installed_step(
        installed_implementation, installed_step
    )
    binding = StepBinding(descriptor, supplied_step)

    with pytest.raises(StepBindingError, match="entry point"):
        StepDescriptor.for_installed_step(supplied_step, installed_step)

    with pytest.raises(StepBindingError, match="entry point"):
        container_step_for_installed_manifest(supplied_step, installed_step=installed_step)

    with pytest.raises(StepBindingError, match="entry point"):
        StepExecutor(binding)

    with pytest.raises(StepBindingError, match="entry point"):
        container_step_for_binding(binding)

    assert installed_implementation.run_calls == 0
    assert supplied_step.run_calls == 0


def test_control_manifest_execution_preserves_typed_output() -> None:
    step = ControlExampleStep()
    bindings = StepBindings()
    _bind_installed(bindings, "acme.plugin.example", step)

    wrapper = StepExecutor(bindings.binding("acme.plugin.example")).execute(
        {"message": "hello", "count": 3, "mode": "slow"}, _context()
    )

    assert wrapper.status == 0
    assert wrapper.output == {
        "message": "hello",
        "invocation_id": "invoke-1",
        "count": 3,
        "mode": "slow",
    }
    assert step.run_calls == 1


@pytest.mark.parametrize(
    "step_factory",
    [ControlExampleStep, TriggerExampleStep],
    ids=["control", "trigger"],
)
def test_control_and_trigger_execution_uses_registered_models(
    step_factory: type[ControlExampleStep | TriggerExampleStep],
) -> None:
    step = step_factory()
    bindings = StepBindings()
    _bind_installed(bindings, "acme.plugin.example", step)
    executor = StepExecutor(bindings.binding("acme.plugin.example"))

    missing = executor.execute({}, _context())

    assert missing.status != 0
    assert missing.error is not None
    assert missing.error.code == FailureCode.INVALID_INPUT.value
    assert step.run_calls == 0

    invalid = executor.execute({"message": "hello", "mode": "invalid"}, _context())

    assert invalid.status != 0
    assert invalid.error is not None
    assert invalid.error.code == FailureCode.INVALID_INPUT.value
    assert step.run_calls == 0

    valid = executor.execute({"message": "hello", "count": 3}, _context())

    assert valid.status == 0
    assert valid.output == {
        "message": "hello",
        "invocation_id": "invoke-1",
        "count": 3,
        "mode": "fast",
    }
    assert step.run_calls == 1
    assert step.last_inputs == ExampleInput(message="hello", count=3, mode="fast")


@pytest.mark.parametrize(
    "step_factory",
    [ControlExampleStep, TriggerExampleStep],
    ids=["control", "trigger"],
)
def test_control_and_trigger_manifest_constraints_reject_values_pydantic_would_accept(
    step_factory: type[ControlExampleStep | TriggerExampleStep],
) -> None:
    input_schema = _input_schema()
    properties = input_schema["properties"]
    assert isinstance(properties, dict)
    properties["message"] = {"type": "string", "minLength": 3}
    inputs = {"message": "x"}

    direct_step = step_factory()
    direct_bindings = StepBindings()
    _bind_installed(
        direct_bindings,
        "acme.plugin.direct",
        direct_step,
        input_schema=input_schema,
    )
    direct_result = StepExecutor(direct_bindings.binding("acme.plugin.direct")).execute(
        inputs, _context()
    )

    container_step = step_factory()
    container_bindings = StepBindings()
    _bind_installed(
        container_bindings,
        "acme.plugin.container",
        container_step,
        input_schema=input_schema,
    )
    container_result = container_step_for_binding(
        container_bindings.binding("acme.plugin.container")
    )(_container_invocation(inputs, step_revision_id="acme.plugin.container"))

    assert direct_result.error is not None
    assert direct_result.error.code == FailureCode.INVALID_INPUT.value
    assert container_result.error_code == FailureCode.INVALID_INPUT.value
    assert direct_step.run_calls == 0
    assert container_step.run_calls == 0


@pytest.mark.parametrize(
    "step_factory",
    [ControlExampleStep, TriggerExampleStep],
    ids=["control", "trigger"],
)
def test_control_and_trigger_executor_propagates_the_complete_context(
    step_factory: type[ControlExampleStep | TriggerExampleStep],
) -> None:
    step = step_factory()
    bindings = StepBindings()
    identity = "acme.plugin.control" if isinstance(step, ControlExampleStep) else "acme.plugin.trigger"
    _bind_installed(bindings, identity, step)
    cancellation = Event()
    cancellation.set()
    runtime_context = {"environment": "test", "requestId": "request-1"}
    context = ExecutionContext(
        invocation_id="invoke-1",
        workflow_run_id="run-1",
        runtime_context=runtime_context,
        cancellation=cancellation,
    )

    wrapper = StepExecutor(bindings.binding(identity)).execute(
        {"message": "hello"}, context
    )

    assert wrapper.status == 0
    assert step.last_context is context
    assert step.last_context.runtime_context is runtime_context
    assert step.last_context.cancelled is True


@pytest.mark.parametrize(
    ("inputs", "error_type"),
    [
        ({"message": 7}, "type"),
        ({"message": "hello", "mode": "other"}, "enum"),
        ({"message": "hello", "unexpected": "value"}, "additionalProperties"),
        ({"message": "hello", "count": 0, "mode": "slow"}, "value_error"),
    ],
    ids=["native-type", "enum", "extra", "model"],
)
def test_control_execution_uses_registered_pydantic_model_failures(
    inputs: dict[str, object], error_type: str
) -> None:
    step = ConfiguredControlStep()
    bindings = StepBindings()
    _bind_installed(
        bindings,
        "acme.plugin.configured",
        step,
        input_schema=ConfiguredControlInput.model_json_schema(),
    )

    wrapper = StepExecutor(bindings.binding("acme.plugin.configured")).execute(
        inputs, _context()
    )

    assert wrapper.status != 0
    assert wrapper.error is not None
    assert wrapper.error.code == FailureCode.INVALID_INPUT.value
    assert error_type in wrapper.error.message
    assert step.run_calls == 0


def test_control_execution_preserves_its_pydantic_model_configuration() -> None:
    step = ConfiguredControlStep()
    bindings = StepBindings()
    _bind_installed(
        bindings,
        "acme.plugin.configured",
        step,
        input_schema=ConfiguredControlInput.model_json_schema(),
    )

    wrapper = StepExecutor(bindings.binding("acme.plugin.configured")).execute(
        {"message": "hello", "count": 3}, _context()
    )

    assert wrapper.status == 0
    assert wrapper.output == {
        "message": "HELLO",
        "invocation_id": "invoke-1",
        "count": 3,
        "mode": "fast",
    }
    assert step.run_calls == 1


def test_trigger_executor_uses_its_registered_model_not_manifest_configuration() -> None:
    step = TriggerExampleStep()
    bindings = StepBindings()
    descriptor = StepDescriptor.for_installed_step(
        step,
        _installed_step(
            _input_schema(),
            step_class=type(step),
            step_identity="acme.plugin.trigger",
            configuration_schema={
                "type": "object",
                "required": ["event_type"],
                "properties": {"event_type": {"const": "issue.opened"}},
            },
        ),
    )
    bindings.bind(descriptor, step)

    wrapper = StepExecutor(bindings.binding("acme.plugin.trigger")).execute(
        {"message": "registered-model-input"}, _context()
    )

    assert wrapper.status == 0
    assert wrapper.output == {
        "message": "registered-model-input",
        "invocation_id": "invoke-1",
        "count": 2,
        "mode": "fast",
    }
    assert step.run_calls == 1

    configuration_input = StepExecutor(bindings.binding("acme.plugin.trigger")).execute(
        {"configuration": {"message": "not-executor-input"}}, _context()
    )

    assert configuration_input.status != 0
    assert configuration_input.error is not None
    assert configuration_input.error.code == FailureCode.INVALID_INPUT.value
    assert step.run_calls == 1


@pytest.mark.parametrize(
    ("step_factory", "identity"),
    [
        (InvalidOutputControlStep, "acme.plugin.control"),
        (InvalidOutputTriggerStep, "acme.plugin.trigger"),
    ],
    ids=["control", "trigger"],
)
def test_control_and_trigger_output_validation_is_shared_by_direct_and_container_execution(
    step_factory: type[InvalidOutputControlStep | InvalidOutputTriggerStep], identity: str
) -> None:
    direct_step = step_factory()
    direct_bindings = StepBindings()
    _bind_installed(direct_bindings, identity, direct_step)

    with pytest.raises(StepExecutionError, match="plugin step") as direct_error:
        StepExecutor(direct_bindings.binding(identity)).execute({"message": "hello"}, _context())

    container_step = step_factory()
    container_bindings = StepBindings()
    _bind_installed(container_bindings, identity, container_step)
    with pytest.raises(ContainerInvocationError, match="invalid response"):
        container_step_for_binding(container_bindings.binding(identity))(
            _container_invocation({"message": "hello"}, step_revision_id=identity)
        )

    assert "action" not in str(direct_error.value).lower()
    assert direct_step.run_calls == 1
    assert container_step.run_calls == 1


def test_supported_control_adapter_preserves_success_payload_and_digest() -> None:
    direct_step = ControlExampleStep()
    container_step = ControlExampleStep()
    direct_bindings = StepBindings()
    container_bindings = StepBindings()
    _bind_installed(direct_bindings, "acme.plugin.control", direct_step)
    _bind_installed(container_bindings, "acme.plugin.control", container_step)
    inputs = {"message": "hello", "count": 3}

    direct_wrapper = StepExecutor(direct_bindings.binding("acme.plugin.control")).execute(
        inputs, _context()
    )
    container_handler = container_step_for_binding(
        container_bindings.binding("acme.plugin.control")
    )
    container_result = container_handler(
        _container_invocation(inputs, step_revision_id="acme.plugin.control")
    )

    assert direct_wrapper.status == 0
    assert container_result.output == direct_wrapper.output
    assert container_result.output_schema_digest == canonical_json_digest(
        ControlExampleOutput.model_json_schema()
    )
    assert direct_step.run_calls == 1
    assert container_step.run_calls == 1


def test_supported_trigger_adapter_preserves_success_payload_and_digest() -> None:
    direct_step = TriggerExampleStep()
    container_step = TriggerExampleStep()
    direct_bindings = StepBindings()
    container_bindings = StepBindings()
    _bind_installed(direct_bindings, "acme.plugin.trigger", direct_step)
    _bind_installed(container_bindings, "acme.plugin.trigger", container_step)
    inputs = {"message": "hello", "count": 3}

    direct_wrapper = StepExecutor(direct_bindings.binding("acme.plugin.trigger")).execute(
        inputs, _context()
    )
    container_result = container_step_for_binding(
        container_bindings.binding("acme.plugin.trigger")
    )(_container_invocation(inputs, step_revision_id="acme.plugin.trigger"))

    assert direct_wrapper.status == 0
    assert container_result.output == direct_wrapper.output
    assert container_result.output_schema_digest == canonical_json_digest(
        ControlExampleOutput.model_json_schema()
    )
    assert direct_step.run_calls == 1
    assert container_step.run_calls == 1


def test_supported_control_adapter_maps_the_invalid_input_wrapper_to_the_envelope() -> None:
    step = ControlExampleStep()
    bindings = StepBindings()
    _bind_installed(bindings, "acme.plugin.control", step)

    result = container_step_for_binding(bindings.binding("acme.plugin.control"))(
        _container_invocation({"message": 42}, step_revision_id="acme.plugin.control")
    )

    assert result.output_schema_digest == canonical_json_digest(
        ControlExampleOutput.model_json_schema()
    )
    assert result.output is None
    assert result.status_code != 0
    assert result.error_code == FailureCode.INVALID_INPUT.value
    assert step.run_calls == 0


def test_supported_trigger_adapter_maps_invalid_input_to_the_envelope() -> None:
    step = TriggerExampleStep()
    bindings = StepBindings()
    _bind_installed(bindings, "acme.plugin.trigger", step)

    result = container_step_for_binding(bindings.binding("acme.plugin.trigger"))(
        _container_invocation({"message": 42}, step_revision_id="acme.plugin.trigger")
    )

    assert result.output_schema_digest == canonical_json_digest(
        ControlExampleOutput.model_json_schema()
    )
    assert result.output is None
    assert result.status_code != 0
    assert result.error_code == FailureCode.INVALID_INPUT.value
    assert step.run_calls == 0


@pytest.mark.parametrize(
    ("step_factory", "identity"),
    [
        (ControlExampleStep, "acme.plugin.control"),
        (TriggerExampleStep, "acme.plugin.trigger"),
    ],
    ids=["control", "trigger"],
)
def test_supported_non_action_adapter_propagates_the_complete_context(
    step_factory: type[ControlExampleStep | TriggerExampleStep], identity: str
) -> None:
    step = step_factory()
    bindings = StepBindings()
    _bind_installed(bindings, identity, step)
    cancellation = Event()
    cancellation.set()
    runtime_context = {"environment": "test", "requestId": "request-1"}
    invocation = ContainerInvocation(
        invocation_id="invoke-1",
        workflow_run_id="run-1",
        step_revision_id=identity,
        inputs={"message": "hello"},
        credential_file_paths={},
        timeout_seconds=30,
        runtime_context=runtime_context,
        cancellation=cancellation,
    )

    result = container_step_for_binding(bindings.binding(identity))(invocation)

    assert result.output is not None
    assert step.last_context is not None
    assert step.last_context.invocation_id == invocation.invocation_id
    assert step.last_context.workflow_run_id == invocation.workflow_run_id
    assert step.last_context.runtime_context is runtime_context
    assert step.last_context.cancellation is cancellation
    assert step.last_context.cancelled is True


def test_output_violating_the_manifest_schema_is_a_safe_execution_failure() -> None:
    step = ExampleStep()
    executor = _executor(step, {"type": "object", "required": ["absent_field"]})
    context = _context()

    with pytest.raises(StepExecutionError):
        executor.execute({"message": "hello"}, context)

    assert step.run_calls == 1


def test_output_validation_enforces_format_assertions() -> None:
    step = ExampleStep()
    executor = _executor(
        step,
        {"type": "object", "properties": {"message": {"type": "string", "format": "ipv4"}}},
    )
    context = _context()

    with pytest.raises(StepExecutionError):
        executor.execute({"message": "hello"}, context)

    assert step.run_calls == 1


def test_oversized_output_is_not_capped_by_direct_execution() -> None:
    # The inline-size bound is a transport concern owned by the container
    # adapter; in-process execution returns the large result unchanged.
    step = ExampleStep()
    executor = _executor(step)

    wrapper = executor.execute({"message": "x" * 1_100_000}, _context())

    assert wrapper.status == 0
    assert wrapper.output is not None
    assert wrapper.output["message"] == "x" * 1_100_000
    assert step.run_calls == 1


def test_importing_the_runtime_does_not_require_grpc() -> None:
    """Direct execution must import where gRPC/protobuf are unavailable."""

    script = textwrap.dedent(
        """
        import sys

        # Make `import grpc` fail the way a transport-free environment would.
        sys.modules["grpc"] = None

        import syntara_plugin.runtime as runtime

        # The direct-execution surface resolves without gRPC.
        for name in (
            "StepExecutor",
            "StandardOutputWrapper",
            "ErrorMessage",
            "StepBindings",
            "StepDescriptor",
            "InstalledStepManifest",
        ):
            assert getattr(runtime, name) is not None, name
        assert "grpc" not in [m for m in sys.modules if sys.modules[m] is not None]

        # Transport exports still resolve by name, but their deps are required.
        try:
            runtime.ContainerClient
        except ImportError:
            pass
        else:
            raise AssertionError("ContainerClient resolved despite unavailable grpc")
        """
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
