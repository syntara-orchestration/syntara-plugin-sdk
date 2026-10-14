"""Tests for the shared raw-input Action execution boundary."""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, model_validator
from syntara_plugin.runtime import (
    ActionStep,
    ContainerInvocation,
    ContainerInvocationError,
    ExecutionContext,
    FailureCode,
    InstalledActionManifest,
    StandardOutputWrapper,
    StepBindingError,
    StepBindings,
    StepDescriptor,
    StepExecutor,
    TaskStep,
    container_action_for_step,
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


class FailingModelStep(ExampleStep):
    def run(self, inputs: ExampleInput, context: Any) -> ExampleOutput:
        raise AssertionError("run must not be called")


class TaskExampleOutput(BaseModel):
    message: str
    invocation_id: str


class TaskExampleStep(TaskStep[ExampleInput, TaskExampleOutput]):
    def run(self, inputs: ExampleInput, context: Any) -> TaskExampleOutput:
        return TaskExampleOutput(message=inputs.message, invocation_id=context.invocation_id)


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


def _installed_action(input_schema: dict[str, object]) -> InstalledActionManifest:
    manifest = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Action",
        "metadata": {
            "name": "example",
            "displayName": "Example",
            "description": "Example action",
        },
        "spec": {
            "runtime": {
                "kind": "custom-workload",
                "entryPoint": "example_module:ExampleStep",
            },
            "input": {"kind": "inline", "value": input_schema},
            "output": {"kind": "inline", "value": {}},
            "error": {"kind": "inline", "value": {}},
        },
    }
    return InstalledActionManifest.from_manifest(
        "acme.plugin.example", manifest, {}
    )


def _bind(step: ExampleStep) -> StepBindings:
    bindings = StepBindings()
    descriptor = StepDescriptor.for_installed_action(
        step, _installed_action(_input_schema())
    )
    bindings.bind(descriptor, step)
    return bindings


def _executor(step: ExampleStep) -> StepExecutor:
    return StepExecutor(_bind(step).binding("acme.plugin.example"))


def _context() -> ExecutionContext:
    return ExecutionContext(
        invocation_id="invoke-1",
        workflow_run_id="run-1",
        runtime_context={"environment": "test"},
    )


def _container_invocation(inputs: dict[str, object]) -> ContainerInvocation:
    return ContainerInvocation(
        invocation_id="invoke-1",
        workflow_run_id="run-1",
        step_revision_id="acme.plugin.example",
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


def test_model_construction_error_is_a_safe_invalid_input() -> None:
    step = FailingModelStep()
    schema = _input_schema()
    schema["properties"]["mode"] = {"type": "string"}
    bindings = StepBindings()
    bindings.bind(
        StepDescriptor.for_installed_action(
            step, _installed_action(schema)
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

    direct_wrapper = _executor(direct_step).execute({"message": "hello"}, _context())
    container_action = container_action_for_step(
        container_step,
        installed_action=_installed_action(_input_schema()),
    )
    container_result = container_action(_container_invocation({"message": "hello"}))

    assert direct_wrapper.status != 0
    assert direct_wrapper.error is not None
    assert direct_wrapper.error.message == (
        "The plugin action input failed model construction."
    )
    assert "sensitive construction failure" not in direct_wrapper.error.message
    assert container_result.output == direct_wrapper.model_dump(mode="json")
    assert direct_step.run_calls == 0
    assert container_step.run_calls == 0


def test_schema_evaluation_failure_is_a_safe_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct_step = ExampleStep()
    container_step = ExampleStep()
    executor = _executor(direct_step)
    container_action = container_action_for_step(
        container_step,
        installed_action=_installed_action(_input_schema()),
    )

    def fail_schema_evaluation(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("sensitive schema failure")

    monkeypatch.setattr(
        Draft202012Validator,
        "iter_errors",
        fail_schema_evaluation,
    )

    direct_wrapper = executor.execute({"message": "hello"}, _context())
    container_result = container_action(_container_invocation({"message": "hello"}))

    assert direct_wrapper.status != 0
    assert direct_wrapper.error is not None
    assert direct_wrapper.error.message == (
        "The plugin action input schema could not be evaluated."
    )
    assert "sensitive schema failure" not in direct_wrapper.error.message
    assert container_result.output == direct_wrapper.model_dump(mode="json")
    assert direct_step.run_calls == 0
    assert container_step.run_calls == 0


def test_direct_and_container_paths_have_equivalent_outcomes() -> None:
    direct_step = ExampleStep()
    container_step = ExampleStep()
    invalid_input = {"message": "hello", "count": "3"}

    direct_wrapper = _executor(direct_step).execute(invalid_input, _context())
    container_action = container_action_for_step(
        container_step,
        installed_action=_installed_action(_input_schema()),
    )
    container_result = container_action(_container_invocation(invalid_input))

    assert direct_wrapper.error is not None
    assert container_result.output is not None
    assert container_result.output == direct_wrapper.model_dump(mode="json")
    assert direct_wrapper.error.code == FailureCode.INVALID_INPUT.value
    assert direct_step.run_calls == 0
    assert container_step.run_calls == 0


def test_direct_and_container_paths_have_equivalent_successes() -> None:
    direct_step = ExampleStep()
    container_step = ExampleStep()
    valid_input = {"message": "hello", "count": 3, "mode": "slow"}

    direct_wrapper = _executor(direct_step).execute(valid_input, _context())
    container_action = container_action_for_step(
        container_step,
        installed_action=_installed_action(_input_schema()),
    )
    container_result = container_action(_container_invocation(valid_input))

    assert direct_wrapper.status == 0
    assert container_result.output == direct_wrapper.output
    assert direct_step.run_calls == 1
    assert container_step.run_calls == 1


def test_container_action_rejects_a_mismatched_installed_identity() -> None:
    step = ExampleStep()
    action = container_action_for_step(
        step,
        installed_action=_installed_action(_input_schema()),
    )
    invocation = _container_invocation({"message": "hello"})
    object.__setattr__(invocation, "step_revision_id", "acme.plugin.other")

    with pytest.raises(ContainerInvocationError) as raised:
        action(invocation)

    assert raised.value.code is FailureCode.NOT_FOUND
    assert raised.value.outcome == "not_started"
    assert step.run_calls == 0


def test_executor_rejects_an_implementation_schema_binding() -> None:
    bindings = StepBindings()
    step = ExampleStep()
    bindings.bind(StepDescriptor.for_step("acme.plugin.example", step), step)
    binding = bindings.binding("acme.plugin.example")

    with pytest.raises(StepBindingError, match="for_installed_action"):
        StepExecutor(binding)


def test_non_action_taxonomy_retains_implementation_schema_compatibility() -> None:
    step = TaskExampleStep()
    bindings = StepBindings()
    bindings.bind(StepDescriptor.for_step("acme.plugin.example", step), step)

    wrapper = StepExecutor(bindings.binding("acme.plugin.example")).execute(
        {"message": "hello", "count": 3, "mode": "slow"}, _context()
    )

    assert wrapper.status == 0
    assert wrapper.output == {
        "message": "hello",
        "invocation_id": "invoke-1",
    }
