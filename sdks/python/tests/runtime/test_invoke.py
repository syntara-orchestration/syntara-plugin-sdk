"""Acceptance tests for the public raw-input runtime invocation boundary."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import BaseModel, field_serializer
from syntara_plugin.contracts import canonical_json_digest
from syntara_plugin.runtime import (
    ActionStep,
    BaseStep,
    ConformanceHarness,
    ContainerInvocation,
    ContainerResult,
    ContainerRuntimeService,
    ControlStep,
    ExecutionContext,
    InstalledStepManifest,
    StandardOutputWrapper,
    StepBinding,
    StepBindings,
    StepDescriptor,
    StepExecutionError,
    StepFailure,
    StepRegistry,
    TriggerStep,
    container_step_for_binding,
    invoke,
    standard_output_from_container_result,
    to_json_document,
)
from syntara_plugin.runtime.container import _request_from_invocation


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
        {},
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
    assert set(document) == {"result", "statusCode", "statusMessage", "errorMessage"}
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
    assert set(document) == {"result", "statusCode", "statusMessage", "errorMessage"}
    assert ConformanceHarness().validate_runtime(document).valid


def test_wrapper_rejects_invalid_status_and_result_combinations() -> None:
    with pytest.raises(ValueError, match="requires a result"):
        StandardOutputWrapper(
            result=None,
            status_code=0,
            status_message="Completed",
            error_message=None,
        )

    with pytest.raises(ValueError, match="structured error metadata"):
        StandardOutputWrapper(
            result=None,
            status_code=1,
            status_message="Failed",
            error_message=None,
        )

    with pytest.raises(ValueError, match="unsigned 32-bit"):
        StandardOutputWrapper(
            result={"value": "hello"},
            status_code=-1,
            status_message="Completed",
            error_message=None,
        )


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


def test_unexpected_exception_is_raised_as_a_safe_execution_error() -> None:
    step = UnexpectedStep()
    binding = _binding(step, "tests.runtime.invoke.unexpected")

    with pytest.raises(StepExecutionError) as raised:
        invoke(binding, {"value": "hello"}, _context(binding))

    assert "unexpected failure secret sentinel" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert step.run_calls == 1


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

    with pytest.raises(StepExecutionError, match="digest"):
        invoke(binding, {"value": "hello"}, context)

    assert step.run_calls == 0

    event = _terminal_event(
        binding,
        {"value": "hello"},
        runtime_context={"outputSchemaDigest": "sha256:" + "0" * 64},
    )
    assert event.WhichOneof("event") == "failure"
    assert event.failure.code == "ABI_VIOLATION"
    assert event.failure.outcome == "unknown"


def test_unprovenanced_binding_cannot_produce_a_wrapper() -> None:
    step = ActionEchoStep()
    descriptor = StepDescriptor.for_step("tests.runtime.invoke.unbound", step)
    binding = StepBinding(descriptor, step)

    with pytest.raises(StepExecutionError, match="trusted installed descriptor"):
        invoke(binding, {"value": "hello"}, ExecutionContext(invocation_id="unbound"))

    assert step.run_calls == 0


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
    assert container.error_code == error["code"]
    assert container.error_message == error["message"]
    assert container.retryable is error["retryable"]
    assert container.error_category == error["category"]
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
    assert result.error_code == "REMOTE_REJECTED"
    assert failure_event.WhichOneof("event") == "failure"
    assert failure_event.failure.outcome == "unknown"
    assert "unexpected failure secret sentinel" not in failure_event.failure.message


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
