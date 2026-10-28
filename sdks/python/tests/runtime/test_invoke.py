"""Acceptance tests for the public raw-input runtime invocation boundary."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import grpc
import pytest
from pydantic import BaseModel, field_serializer
from syntara_plugin.contracts import canonical_json_digest
from syntara_plugin.runtime import (
    ActionStep,
    BaseStep,
    ConformanceHarness,
    ContainerClient,
    ContainerInvocation,
    ContainerRemoteError,
    ContainerResult,
    ContainerRuntimeService,
    ControlStep,
    ErrorResult,
    ExecutionContext,
    FailureCode,
    InstalledStepManifest,
    StandardOutputWrapper,
    StepBinding,
    StepBindingError,
    StepBindings,
    StepDescriptor,
    StepExecutionError,
    StepFailure,
    StepRegistry,
    TriggerStep,
    add_container_runtime_service,
    container_step_for_binding,
    error_result_base_schema,
    error_result_schema,
    invoke,
    standard_output_from_container_result,
    to_json_document,
)
from syntara_plugin.runtime.container import _request_from_invocation
from syntara_plugin.runtime.server import AsyncContainerRuntimeService


class InvocationInput(BaseModel):
    value: str


class InvocationOutput(BaseModel):
    value: str
    groups: dict[str, list[dict[str, list[str]]]]


class _EchoStep:
    """Shared typed implementation for the three public Step categories."""

    def __init__(self) -> None:
        self.run_calls = 0

    def run(self, inputs: InvocationInput, context: ExecutionContext) -> InvocationOutput:
        self.run_calls += 1
        return InvocationOutput(
            value=inputs.value,
            groups={"primary": [{"labels": [context.invocation_id, "stable"]}]},
        )


class ActionEchoStep(_EchoStep, ActionStep[InvocationInput, InvocationOutput]):
    pass


class ControlEchoStep(_EchoStep, ControlStep[InvocationInput, InvocationOutput]):
    pass


class TriggerEchoStep(_EchoStep, TriggerStep[InvocationInput, InvocationOutput]):
    pass


class _FailingStep:
    """Shared typed implementation that raises an explicit step failure."""

    def __init__(self) -> None:
        self.run_calls = 0

    def run(self, inputs: InvocationInput, context: ExecutionContext) -> InvocationOutput:
        self.run_calls += 1
        raise StepFailure(
            73,
            "The remote operation was rejected.",
            "The remote service rejected the request.",
            category="dependency",
            code="REMOTE_REJECTED",
            retryable=True,
        )


class ActionFailureStep(_FailingStep, ActionStep[InvocationInput, InvocationOutput]):
    pass


class DetailedFailureStep(ActionStep[InvocationInput, InvocationOutput]):
    def run(self, inputs: InvocationInput, context: ExecutionContext) -> InvocationOutput:
        raise StepFailure(
            73, "Rejected", error_result=ErrorResult(
                category="dependency", code="REMOTE_REJECTED", message="Rejected",
                retryable=True, provider={"requestId": "req-1", "attempts": [1, 2]},
            )
        )


class TypedErrorResult(ErrorResult):
    provider: dict[str, list[str]]


class ControlFailureStep(_FailingStep, ControlStep[InvocationInput, InvocationOutput]):
    pass


class TriggerFailureStep(_FailingStep, TriggerStep[InvocationInput, InvocationOutput]):
    pass


class WrongOutput(BaseModel):
    unexpected: str


class WrongOutputStep(ActionStep[InvocationInput, InvocationOutput]):
    def __init__(self) -> None:
        self.run_calls = 0

    def run(self, inputs: InvocationInput, context: ExecutionContext) -> InvocationOutput:
        self.run_calls += 1
        return WrongOutput(unexpected="wrong-model")  # type: ignore[return-value]


class SchemaMismatchStep(ActionStep[InvocationInput, InvocationOutput]):
    def __init__(self) -> None:
        self.run_calls = 0

    def run(self, inputs: InvocationInput, context: ExecutionContext) -> InvocationOutput:
        self.run_calls += 1
        return InvocationOutput(value=inputs.value, groups={"primary": []})


class UnserializableOutput(BaseModel):
    value: str

    @field_serializer("value")
    def fail_serialization(self, value: str) -> str:
        raise ValueError("serialization secret sentinel")


class UnserializableStep(ActionStep[InvocationInput, UnserializableOutput]):
    def __init__(self) -> None:
        self.run_calls = 0

    def run(self, inputs: InvocationInput, context: ExecutionContext) -> UnserializableOutput:
        self.run_calls += 1
        return UnserializableOutput(value=inputs.value)


class LargeOutput(BaseModel):
    data: str


class LargeOutputStep(ActionStep[InvocationInput, LargeOutput]):
    def __init__(self) -> None:
        self.run_calls = 0

    def run(self, inputs: InvocationInput, context: ExecutionContext) -> LargeOutput:
        self.run_calls += 1
        return LargeOutput(data="x" * (16 * 1_048_576 + 1))


class NonFiniteOutput(BaseModel):
    value: float


class NonFiniteStep(ActionStep[InvocationInput, NonFiniteOutput]):
    def __init__(self) -> None:
        self.run_calls = 0

    def run(self, inputs: InvocationInput, context: ExecutionContext) -> NonFiniteOutput:
        self.run_calls += 1
        return NonFiniteOutput(value=float("nan"))


class UnexpectedStep(ActionStep[InvocationInput, InvocationOutput]):
    def __init__(self) -> None:
        self.run_calls = 0

    def run(self, inputs: InvocationInput, context: ExecutionContext) -> InvocationOutput:
        self.run_calls += 1
        raise RuntimeError("unexpected failure secret sentinel")


class ExecuteCountingStep(ActionEchoStep):
    def __init__(self) -> None:
        super().__init__()
        self.execute_calls = 0

    def execute(
        self, inputs: InvocationInput, context: ExecutionContext
    ) -> InvocationOutput:
        self.execute_calls += 1
        return super().execute(inputs, context)


class TriggerConfigurationStep(TriggerStep[InvocationInput, InvocationOutput]):
    def __init__(self) -> None:
        self.run_calls = 0

    def run(self, inputs: InvocationInput, context: ExecutionContext) -> InvocationOutput:
        self.run_calls += 1
        return InvocationOutput(value=inputs.value, groups={"primary": []})


_SUCCESS_CASES: tuple[tuple[type[BaseStep[Any, Any]], str], ...] = (
    (ActionEchoStep, "tests.runtime.invoke.action-echo"),
    (ControlEchoStep, "tests.runtime.invoke.control-echo"),
    (TriggerEchoStep, "tests.runtime.invoke.trigger-echo"),
)
_FAILURE_CASES: tuple[tuple[type[BaseStep[Any, Any]], str], ...] = (
    (ActionFailureStep, "tests.runtime.invoke.action-failure"),
    (ControlFailureStep, "tests.runtime.invoke.control-failure"),
    (TriggerFailureStep, "tests.runtime.invoke.trigger-failure"),
)


def _binding(
    step: BaseStep[Any, Any],
    identity: str,
    *,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    configuration_schema: dict[str, Any] | None = None,
    error_schema: dict[str, Any] | None = None,
    error_asset: bool = False,
) -> StepBinding:
    """Bind a test step to an installed manifest and its trusted schemas."""

    if isinstance(step, ActionStep):
        kind = "Action"
    elif isinstance(step, ControlStep):
        kind = "Control"
    else:
        kind = "Trigger"

    specification: dict[str, Any] = {
        "runtime": {
            "kind": "custom-runner",
            "entryPoint": f"{type(step).__module__}:{type(step).__qualname__}",
        },
        "input": {
            "kind": "inline",
            "value": input_schema or step.input_model().model_json_schema(),
        },
        "output": {
            "kind": "inline",
            "value": output_schema or step.output_model().model_json_schema(),
        },
    }
    if kind == "Control":
        specification["invocation"] = {"kind": "orchestration"}
    elif kind == "Trigger":
        specification.update(
            {
                "driver": "webhook.v1",
                "configuration": {
                    "kind": "inline",
                    "value": configuration_schema or {"type": "object"},
                },
            }
        )

    assets = {}
    if error_schema is not None:
        if error_asset:
            specification["error"] = {"kind": "asset", "path": "schemas/error.json"}
            assets = {"schemas/error.json": error_schema}
        else:
            specification["error"] = {"kind": "inline", "value": error_schema}

    manifest = InstalledStepManifest.from_manifest(
        identity,
        {
            "apiVersion": "syntara.io/v1alpha1",
            "kind": kind,
            "metadata": {
                "name": "invoke-test",
                "displayName": "Invoke Test",
                "description": "Runtime invoke acceptance test step.",
            },
            "spec": specification,
        },
        assets,
    )
    descriptor = StepDescriptor.for_installed_step(step, manifest)
    bindings = StepBindings()
    bindings.bind(descriptor, step)
    return bindings.binding(identity)


def _schema_digest(binding: StepBinding) -> str:
    return canonical_json_digest(to_json_document(binding.descriptor.output_schema))


def _context(
    binding: StepBinding,
    *,
    invocation_id: str = "invoke-acceptance",
    runtime_context: dict[str, Any] | None = None,
) -> ExecutionContext:
    values = {"outputSchemaDigest": _schema_digest(binding)}
    if runtime_context is not None:
        values.update(runtime_context)
    return ExecutionContext(
        invocation_id=invocation_id,
        workflow_run_id=f"{invocation_id}-run",
        runtime_context=values,
    )


def _invocation(
    binding: StepBinding,
    inputs: dict[str, Any],
    *,
    invocation_id: str = "invoke-acceptance",
    runtime_context: dict[str, Any] | None = None,
) -> ContainerInvocation:
    values = {"outputSchemaDigest": _schema_digest(binding)}
    if runtime_context is not None:
        values.update(runtime_context)
    return ContainerInvocation(
        invocation_id=invocation_id,
        workflow_run_id=f"{invocation_id}-run",
        step_revision_id=binding.descriptor.step_identity,
        inputs=inputs,
        credential_file_paths={},
        timeout_seconds=30,
        runtime_context=values,
    )


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _error_document(error: Any) -> dict[str, Any]:
    if isinstance(error, Mapping):
        document = dict(error)
    else:
        document = {
            name: getattr(error, name)
            for name in ("category", "code", "message", "retryable")
        }
    assert set(document) == {"category", "code", "message", "retryable"}
    return document


class _ServicerContext:
    def add_callback(self, callback: Any) -> bool:
        self.callback = callback
        return True


def _terminal_event(
    binding: StepBinding,
    inputs: dict[str, Any],
    *,
    runtime_context: dict[str, Any] | None = None,
) -> Any:
    invocation = _invocation(binding, inputs, runtime_context=runtime_context)
    service = ContainerRuntimeService(
        StepRegistry(
            {
                binding.descriptor.step_identity: container_step_for_binding(binding),
            }
        )
    )
    return next(service.Execute(_request_from_invocation(invocation), _ServicerContext()))


@pytest.mark.parametrize(("step_type", "identity"), _SUCCESS_CASES, ids=("action", "control", "trigger"))
def test_invoke_returns_canonical_success_for_each_step_kind(
    step_type: type[BaseStep[Any, Any]], identity: str
) -> None:
    binding = _binding(step_type(), identity)

    wrapper = invoke(binding, {"value": "hello"}, _context(binding))

    assert wrapper.status_code == 0
    assert wrapper.status_message
    assert wrapper.error_message is None
    assert _plain_json(wrapper.result) == {
        "value": "hello",
        "groups": {"primary": [{"labels": ["invoke-acceptance", "stable"]}]},
    }
    document = wrapper.model_dump(mode="json", by_alias=True)
    assert set(document) == {"result", "statusCode", "statusMessage", "errorResult"}
    assert ConformanceHarness().validate_runtime(document).valid


@pytest.mark.parametrize(("step_type", "identity"), _FAILURE_CASES, ids=("action", "control", "trigger"))
def test_invoke_returns_typed_failure_for_each_step_kind(
    step_type: type[BaseStep[Any, Any]], identity: str
) -> None:
    step = step_type()
    binding = _binding(step, identity)

    wrapper = invoke(binding, {"value": "hello"}, _context(binding))

    assert wrapper.status_code == 73
    assert wrapper.status_message == "The remote operation was rejected."
    error = _error_document(wrapper.error_message)
    assert error == {
        "category": "dependency",
        "code": "REMOTE_REJECTED",
        "message": "The remote service rejected the request.",
        "retryable": True,
    }
    assert wrapper.result is None
    assert step.run_calls == 1
    document = wrapper.model_dump(mode="json", by_alias=True)
    assert set(document) == {"result", "statusCode", "statusMessage", "errorResult"}
    assert ConformanceHarness().validate_runtime(document).valid


def test_wrapper_rejects_invalid_status_and_result_combinations() -> None:
    with pytest.raises(ValueError, match="requires a result"):
        StandardOutputWrapper(
            result=None,
            status_code=0,
            status_message="Completed",
            error_result=None,
        )

    with pytest.raises(ValueError, match="structured error metadata"):
        StandardOutputWrapper(
            result=None,
            status_code=1,
            status_message="Failed",
            error_result=None,
        )

    with pytest.raises(ValueError, match="unsigned 32-bit"):
        StandardOutputWrapper(
            result={"value": "hello"},
            status_code=-1,
            status_message="Completed",
            error_result=None,
        )


def test_wrapper_hash_uses_its_immutable_content() -> None:
    first = StandardOutputWrapper.success({"nested": [{"value": "same"}]})
    second = StandardOutputWrapper.success({"nested": [{"value": "same"}]})

    assert first == second
    assert hash(first) == hash(second)
    assert {first: "stored"}[second] == "stored"

    first_failure = StandardOutputWrapper.failure(
        ErrorResult(
            category="dependency",
            code="REMOTE_REJECTED",
            message="Rejected",
            provider={"requestId": "req-1"},
        )
    )
    second_failure = StandardOutputWrapper.failure(
        ErrorResult(
            category="dependency",
            code="REMOTE_REJECTED",
            message="Rejected",
            provider={"requestId": "req-1"},
        )
    )

    assert first_failure == second_failure
    assert hash(first_failure) == hash(second_failure)


def test_wrapper_equality_distinguishes_boolean_and_number_json_values() -> None:
    boolean_result = StandardOutputWrapper.success({"nested": [{"value": True}]})
    numeric_result = StandardOutputWrapper.success({"nested": [{"value": 1}]})

    assert boolean_result != numeric_result
    assert len({boolean_result: "boolean", numeric_result: "number"}) == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"code": "STEP_FAILED"},
        {"category": "business"},
        {"retryable": False},
        {"retryable": True},
    ],
)
def test_step_failure_rejects_error_result_with_metadata_overrides(
    overrides: dict[str, object],
) -> None:
    error = ErrorResult(
        category="dependency", code="REMOTE_REJECTED", message="Rejected"
    )

    with pytest.raises(ValueError, match="without code, category, or retryable overrides"):
        StepFailure(73, "Rejected", error_result=error, **overrides)


def test_step_failure_preserves_legacy_defaults_and_overrides() -> None:
    defaulted = StepFailure(1, "Failed", "The operation failed.")
    customized = StepFailure(
        73,
        "Rejected",
        "The service rejected the request.",
        code="REMOTE_REJECTED",
        category="dependency",
        retryable=True,
    )

    assert defaulted.error_result.model_dump(mode="json") == {
        "category": "business",
        "code": "STEP_FAILED",
        "message": "The operation failed.",
        "retryable": False,
    }
    assert customized.error_result.code == "REMOTE_REJECTED"
    assert customized.error_result.category == "dependency"
    assert customized.error_result.retryable is True


def test_wrapper_result_is_recursively_immutable_and_detached_from_step_output() -> None:
    source = {"nested": [{"labels": ["original"]}]}
    detached = StandardOutputWrapper.success(source)
    source["nested"][0]["labels"][0] = "mutated-after-construction"
    assert detached.result["nested"][0]["labels"] == ("original",)

    step = ActionEchoStep()
    binding = _binding(step, "tests.runtime.invoke.immutable")
    wrapper = invoke(binding, {"value": "hello"}, _context(binding))
    result = wrapper.result

    assert isinstance(result, Mapping)
    assert isinstance(result["groups"], Mapping)
    assert isinstance(result["groups"]["primary"], tuple)
    assert isinstance(result["groups"]["primary"][0], Mapping)
    assert isinstance(result["groups"]["primary"][0]["labels"], tuple)
    with pytest.raises(TypeError):
        result["groups"]["primary"][0]["labels"][0] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        result["groups"]["primary"][0]["other"] = []  # type: ignore[index]
    with pytest.raises(TypeError):
        result["other"] = "changed"  # type: ignore[index]
    assert wrapper.result["groups"]["primary"][0]["labels"] == (
        "invoke-acceptance",
        "stable",
    )


def test_invalid_input_failure_is_safe_and_does_not_echo_rejected_values() -> None:
    step = ActionEchoStep()
    secret = "secret-sentinel-input-value"
    input_schema = copy.deepcopy(InvocationInput.model_json_schema())
    input_schema["properties"]["value"]["minLength"] = 100
    input_schema["additionalProperties"] = False

    binding = _binding(step, "tests.runtime.invoke.invalid-input", input_schema=input_schema)
    wrapper = invoke(binding, {"value": secret, "extra": "rejected"}, _context(binding))

    assert wrapper.status_code != 0
    error = _error_document(wrapper.error_message)
    assert secret not in error["message"]
    assert "rejected" not in error["message"]
    assert step.run_calls == 0


def test_invalid_input_uses_the_sdk_error_schema_with_a_strict_declared_schema() -> None:
    step = ExecuteCountingStep()
    input_schema = copy.deepcopy(InvocationInput.model_json_schema())
    input_schema["properties"]["value"]["minLength"] = 10
    error_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["category", "code", "message", "retryable"],
        "properties": {
            "category": {"type": "string"},
            "code": {"type": "string"},
            "message": {"type": "string"},
            "retryable": {"type": "boolean"},
        },
    }
    binding = _binding(
        step,
        "tests.runtime.invoke.invalid-input-error-schema",
        input_schema=input_schema,
        error_schema=error_schema,
    )

    event = _terminal_event(binding, {"value": "short"})

    assert event.WhichOneof("event") == "result"
    assert event.result.status_code == 1
    assert json.loads(event.result.error_result_json)["code"] == FailureCode.INVALID_INPUT.value
    assert event.result.error_schema_digest == canonical_json_digest(error_result_base_schema())

    container_result = container_step_for_binding(binding)(_invocation(binding, {"value": "short"}))
    wrapper = standard_output_from_container_result(
        container_result,
        output_schema=to_json_document(binding.descriptor.output_schema),
        error_schema=error_schema,
    )

    assert wrapper.error.code == FailureCode.INVALID_INPUT.value
    assert step.execute_calls == 0
    assert step.run_calls == 0


def test_unexpected_exception_is_raised_as_a_safe_execution_error() -> None:
    step = UnexpectedStep()
    binding = _binding(step, "tests.runtime.invoke.unexpected")

    with pytest.raises(StepExecutionError) as raised:
        invoke(binding, {"value": "hello"}, _context(binding))

    assert "unexpected failure secret sentinel" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.outcome == "unknown"
    assert step.run_calls == 1


def test_input_model_preparation_exception_is_not_started(monkeypatch: pytest.MonkeyPatch) -> None:
    step = ExecuteCountingStep()
    binding = _binding(step, "tests.runtime.invoke.input-model-preparation-failure")

    def fail_input_model(_step_type: type[BaseStep[Any, Any]]) -> type[BaseModel]:
        raise RuntimeError("input model preparation failed")

    monkeypatch.setattr(type(step), "input_model", classmethod(fail_input_model))

    with pytest.raises(StepExecutionError) as raised:
        invoke(binding, {"value": "hello"}, _context(binding))

    assert raised.value.outcome == "not_started"
    assert step.execute_calls == 0


@pytest.mark.parametrize(
    ("step", "identity", "output_schema"),
    [
        (WrongOutputStep(), "tests.runtime.invoke.wrong-model", None),
        (
            SchemaMismatchStep(),
            "tests.runtime.invoke.schema-mismatch",
            {
                **copy.deepcopy(InvocationOutput.model_json_schema()),
                "properties": {
                    **copy.deepcopy(InvocationOutput.model_json_schema()["properties"]),
                    "value": {"type": "string", "maxLength": 2},
                },
            },
        ),
    ],
    ids=("wrong-output-model", "declared-schema-mismatch"),
)
def test_output_model_and_declared_schema_mismatches_have_no_wrapper(
    step: BaseStep[Any, Any], identity: str, output_schema: dict[str, Any] | None
) -> None:
    binding = _binding(step, identity, output_schema=output_schema)

    with pytest.raises(StepExecutionError) as raised:
        invoke(binding, {"value": "too-long"}, _context(binding))

    assert str(raised.value)
    assert "unexpected" not in str(raised.value)

    event = _terminal_event(binding, {"value": "too-long"})
    assert event.WhichOneof("event") == "failure"
    assert event.failure.code == "ABI_VIOLATION"
    assert event.failure.outcome == "unknown"


def test_non_finite_output_has_no_wrapper_or_container_result() -> None:
    step = NonFiniteStep()
    binding = _binding(step, "tests.runtime.invoke.non-finite")

    with pytest.raises(StepExecutionError):
        invoke(binding, {"value": "hello"}, _context(binding))

    event = _terminal_event(binding, {"value": "hello"})
    assert event.WhichOneof("event") == "failure"
    assert event.failure.code == "ABI_VIOLATION"
    assert event.failure.outcome == "unknown"
    assert "nan" not in event.failure.message.lower()


def test_output_serialization_failure_is_safe_and_has_no_wrapper() -> None:
    step = UnserializableStep()
    binding = _binding(step, "tests.runtime.invoke.serialization")

    with pytest.raises(StepExecutionError) as raised:
        invoke(binding, {"value": "hello"}, _context(binding))

    assert "serialization secret sentinel" not in str(raised.value)
    assert step.run_calls == 1


def test_large_result_is_returned_before_transport_mapping() -> None:
    step = LargeOutputStep()
    binding = _binding(step, "tests.runtime.invoke.oversized")

    wrapper = invoke(binding, {"value": "hello"}, _context(binding))

    assert wrapper.status == 0
    assert wrapper.output is not None
    assert len(wrapper.output["data"]) == 16 * 1_048_576 + 1
    assert step.run_calls == 1


def test_supplied_output_schema_digest_must_match_the_bound_descriptor() -> None:
    step = ActionEchoStep()
    binding = _binding(step, "tests.runtime.invoke.digest-mismatch")
    context = _context(binding, runtime_context={"outputSchemaDigest": "sha256:" + "0" * 64})

    with pytest.raises(StepExecutionError, match="digest") as raised:
        invoke(binding, {"value": "hello"}, context)

    assert raised.value.outcome == "not_started"
    assert step.run_calls == 0

    event = _terminal_event(
        binding,
        {"value": "hello"},
        runtime_context={"outputSchemaDigest": "sha256:" + "0" * 64},
    )
    assert event.WhichOneof("event") == "failure"
    assert event.failure.code == "ABI_VIOLATION"
    assert event.failure.outcome == "not_started"


def test_unprovenanced_binding_cannot_produce_a_wrapper() -> None:
    step = ActionEchoStep()
    descriptor = StepDescriptor.for_step("tests.runtime.invoke.unbound", step)
    binding = StepBinding(descriptor, step)

    with pytest.raises(StepExecutionError, match="trusted installed descriptor") as raised:
        invoke(binding, {"value": "hello"}, ExecutionContext(invocation_id="unbound"))

    assert raised.value.outcome == "not_started"
    assert step.run_calls == 0


@pytest.mark.parametrize("outcome", ["not_started", "unknown"])
def test_container_runtime_service_preserves_step_execution_outcome(outcome: str) -> None:
    binding = _binding(ActionEchoStep(), "tests.runtime.invoke.server-outcome")

    def fail(_invocation: ContainerInvocation) -> Any:
        raise StepExecutionError("A safe execution failure.", outcome=outcome)

    service = ContainerRuntimeService(
        StepRegistry({binding.descriptor.step_identity: fail})
    )
    invocation = _invocation(binding, {"value": "hello"})
    event = next(service.Execute(_request_from_invocation(invocation), _ServicerContext()))

    assert event.WhichOneof("event") == "failure"
    assert event.failure.outcome == outcome


class _AsyncServicerContext:
    def add_done_callback(self, callback: Any) -> None:
        del callback


@pytest.mark.parametrize("outcome", ["not_started", "unknown"])
def test_async_container_runtime_service_preserves_step_execution_outcome(
    outcome: str,
) -> None:
    binding = _binding(ActionEchoStep(), "tests.runtime.invoke.async-server-outcome")

    def fail(_invocation: ContainerInvocation) -> Any:
        raise StepExecutionError("A safe execution failure.", outcome=outcome)

    service = AsyncContainerRuntimeService(
        StepRegistry({binding.descriptor.step_identity: fail})
    )
    invocation = _invocation(binding, {"value": "hello"})

    async def read_event() -> Any:
        events = [
            event
            async for event in service.Execute(
                _request_from_invocation(invocation), _AsyncServicerContext()
            )
        ]
        return events[0]

    event = asyncio.run(read_event())

    assert event.WhichOneof("event") == "failure"
    assert event.failure.outcome == outcome


def test_trigger_input_uses_spec_input_and_never_configuration() -> None:
    step = TriggerConfigurationStep()
    binding = _binding(
        step,
        "tests.runtime.invoke.trigger-input",
        configuration_schema={
            "type": "object",
            "required": ["driver_mode"],
            "properties": {"driver_mode": {"const": "webhook"}},
        },
    )

    input_wrapper = invoke(binding, {"value": "event-payload"}, _context(binding))
    configuration_wrapper = invoke(
        binding,
        {"driver_mode": "webhook"},
        _context(binding, invocation_id="trigger-configuration"),
    )

    assert input_wrapper.status_code == 0
    assert input_wrapper.result["value"] == "event-payload"
    assert configuration_wrapper.status_code != 0
    assert step.run_calls == 1


@pytest.mark.parametrize(("step_type", "identity"), _SUCCESS_CASES, ids=("action", "control", "trigger"))
def test_direct_and_container_adapter_results_are_equivalent(
    step_type: type[BaseStep[Any, Any]], identity: str
) -> None:
    binding = _binding(step_type(), identity)
    context = _context(binding, invocation_id="direct-container-equivalence")
    inputs = {"value": "same-result"}

    direct = invoke(binding, inputs, context)
    container = container_step_for_binding(binding)(_invocation(
        binding,
        inputs,
        invocation_id=context.invocation_id,
        runtime_context=context.runtime_context,
    ))

    assert isinstance(container, ContainerResult)
    assert container.output_schema_digest == _schema_digest(binding)
    assert container.status_code == direct.status_code == 0
    assert container.status_message == direct.status_message
    assert _plain_json(container.output) == _plain_json(direct.result)


@pytest.mark.parametrize(("step_type", "identity"), _FAILURE_CASES, ids=("action", "control", "trigger"))
def test_direct_and_container_adapter_preserve_typed_failure_metadata(
    step_type: type[BaseStep[Any, Any]], identity: str
) -> None:
    binding = _binding(step_type(), identity)
    context = _context(binding, invocation_id="failure-equivalence")
    inputs = {"value": "rejected"}

    direct = invoke(binding, inputs, context)
    container = container_step_for_binding(binding)(_invocation(
        binding,
        inputs,
        invocation_id=context.invocation_id,
        runtime_context=context.runtime_context,
    ))

    error = _error_document(direct.error_message)
    assert isinstance(container, ContainerResult)
    assert container.status_code == direct.status_code == 73
    assert container.status_message == direct.status_message
    assert container.error_result == error
    assert container.output is None
    assert standard_output_from_container_result(
        container,
        output_schema=to_json_document(binding.descriptor.output_schema),
    ) == direct


def test_container_result_and_container_failure_remain_distinct() -> None:
    deliberate = _binding(
        ActionFailureStep(),
        "tests.runtime.invoke.container-deliberate-failure",
    )
    unexpected = _binding(
        UnexpectedStep(),
        "tests.runtime.invoke.container-unexpected-failure",
    )

    result_event = _terminal_event(deliberate, {"value": "x"})
    failure_event = _terminal_event(unexpected, {"value": "x"})

    assert result_event.WhichOneof("event") == "result"
    result = result_event.result
    assert result.status_code == 73
    assert b'"code":"REMOTE_REJECTED"' in result.error_result_json
    assert failure_event.WhichOneof("event") == "failure"
    assert failure_event.failure.outcome == "unknown"
    assert "unexpected failure secret sentinel" not in failure_event.failure.message


@pytest.mark.parametrize("asset", [False, True])
def test_custom_error_schema_preserves_complete_result_through_grpc(asset: bool) -> None:
    schema = {
        "type": "object",
        "required": ["provider"],
        "properties": {
            "provider": {
                "type": "object", "required": ["requestId", "attempts"],
                "properties": {"requestId": {"const": "req-1"}, "attempts": {"type": "array"}},
            }
        },
    }
    binding = _binding(DetailedFailureStep(), "tests.detailed-error", error_schema=schema, error_asset=asset)
    # Installation snapshots its schema, including asset-backed documents.
    schema["required"].append("changed")
    trusted_schema = to_json_document(binding.descriptor.error_schema)
    assert trusted_schema["required"] == ["provider"]
    direct = invoke(binding, {"value": "x"}, _context(binding))
    assert direct.error_result is not None
    error_document = direct.error_result.model_dump(mode="json")
    assert error_document["provider"] == {"requestId": "req-1", "attempts": [1, 2]}
    with pytest.raises(TypeError):
        direct.error_result.provider["requestId"] = "changed"

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    registry = StepRegistry({binding.descriptor.step_identity: container_step_for_binding(binding)})
    add_container_runtime_service(server, registry)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        client = ContainerClient(channel)
        result = client.execute(_invocation(binding, {"value": "x"}))
        assert result.error_result == error_document
        assert result.error_schema_digest == canonical_json_digest(trusted_schema)
        assert client.execute_wrapped(
            _invocation(binding, {"value": "x"}),
            output_schema=to_json_document(binding.descriptor.output_schema),
            error_schema=trusted_schema,
        ) == direct
        with pytest.raises(ContainerRemoteError):
            standard_output_from_container_result(
                result, output_schema=to_json_document(binding.descriptor.output_schema)
            )
        with pytest.raises(ContainerRemoteError):
            standard_output_from_container_result(
                result, output_schema=to_json_document(binding.descriptor.output_schema),
                error_schema={"type": "object"},
            )
    finally:
        channel.close()
        server.stop(grace=0).wait()


def test_default_error_schema_is_applied_and_digest_is_reported() -> None:
    binding = _binding(ActionFailureStep(), "tests.default-error")
    result = container_step_for_binding(binding)(_invocation(binding, {"value": "x"}))
    assert result.error_schema_digest == canonical_json_digest(error_result_schema())
    assert result.error_result is not None
    assert set(result.error_result) == {"category", "code", "message", "retryable"}


def test_default_error_schema_rejects_undeclared_step_fields() -> None:
    binding = _binding(DetailedFailureStep(), "tests.default-error-extra")
    with pytest.raises(StepExecutionError, match="invalid response"):
        invoke(binding, {"value": "x"}, _context(binding))


def test_error_result_subclass_fields_are_preserved_and_immutable() -> None:
    provider = {"attempts": ["first"]}
    error = TypedErrorResult(code="FAILED", message="Failed", provider=provider)
    provider["attempts"].append("second")
    assert error.model_dump(mode="json")["provider"] == {"attempts": ["first"]}
    with pytest.raises(TypeError):
        error.provider["attempts"] = []
    with pytest.raises(AttributeError):
        error.provider["attempts"].append("third")


def test_author_error_that_violates_installed_schema_has_no_valid_outcome() -> None:
    binding = _binding(
        DetailedFailureStep(), "tests.invalid-error",
        error_schema={"type": "object", "required": ["missing"]},
    )
    with pytest.raises(StepExecutionError, match="invalid response"):
        invoke(binding, {"value": "x"}, _context(binding))
    event = _terminal_event(binding, {"value": "x"})
    assert event.WhichOneof("event") == "failure"
    assert event.failure.code == "ABI_VIOLATION"


def test_reconstruction_checks_error_schema_even_with_matching_digest() -> None:
    schema = {"type": "object", "required": ["missing"]}
    result = ContainerResult(
        canonical_json_digest({"type": "object"}), status_code=1,
        error_result={"category": "business", "code": "FAILED", "message": "Failed", "retryable": False},
        error_schema_digest=canonical_json_digest(schema),
    )
    with pytest.raises(ContainerRemoteError):
        standard_output_from_container_result(result, output_schema={"type": "object"}, error_schema=schema)


@pytest.mark.parametrize("schema", [{"type": "not-a-type"}, {"$ref": "#/$defs/missing"}])
def test_invalid_installed_error_schema_is_rejected(schema: dict[str, Any]) -> None:
    with pytest.raises(StepBindingError, match="error_schema"):
        _binding(DetailedFailureStep(), "tests.bad-error-schema", error_schema=schema)


def test_changed_error_schema_invalidates_installed_binding() -> None:
    binding = _binding(DetailedFailureStep(), "tests.changed-error-schema")
    object.__setattr__(binding.descriptor, "error_schema", {"type": "object"})
    with pytest.raises(StepExecutionError, match="trusted installed descriptor"):
        invoke(binding, {"value": "x"}, _context(binding))


def test_direct_runtime_import_does_not_load_transport_modules() -> None:
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import sys
        sys.modules["grpc"] = None
        import syntara_plugin.runtime as runtime
        assert runtime.invoke is not None
        assert runtime.StepFailure is not None
        assert runtime.StandardOutputWrapper is not None
        assert "syntara_plugin.runtime.protocol.container_pb2" not in sys.modules
        assert "grpc" not in [name for name, module in sys.modules.items() if module is not None]
        """
    )
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
