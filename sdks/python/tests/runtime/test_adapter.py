"""Tests for dispatching container invocations to typed Python steps."""

from __future__ import annotations

import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import grpc
import pytest
from pydantic import BaseModel
from syntara_plugin.contracts import canonical_json_digest
from syntara_plugin.runtime import (
    ActionStep,
    BaseStep,
    ContainerClient,
    ContainerInvocation,
    ContainerInvocationError,
    ContainerProgress,
    ContainerRemoteError,
    ContainerResult,
    ControlStep,
    ErrorCategory,
    ErrorMessage,
    ExecutionContext,
    FailureCode,
    InstalledStepManifest,
    StandardOutputWrapper,
    StepBindings,
    StepDescriptor,
    StepRegistry,
    TriggerStep,
    add_container_runtime_service,
    container_step_for_bindings,
    container_step_for_installed_manifest,
    error_result_schema,
    invoke,
    standard_output_from_container_result,
    step_registry_from_bindings,
)
from syntara_plugin.runtime.adapter import _container_result, _partial_result_validator
from syntara_plugin.runtime.container import _new_partial_result_reporter


class EchoInput(BaseModel):
    message: str


class EchoOutput(BaseModel):
    message: str
    invocation_id: str
    execution_id: str
    cancelled: bool


class PartialResultOutput(BaseModel):
    job_id: str
    job_url: str
    status: str


class ActionEchoStep(ActionStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: Any) -> EchoOutput:
        return EchoOutput(
            message=inputs.message,
            invocation_id=context.invocation_id,
            execution_id=context.runtime_context.get("execution_id", ""),
            cancelled=context.cancelled,
        )


class PartialResultStep(ActionStep[EchoInput, PartialResultOutput]):
    last_context: Any | None = None

    def run(self, inputs: EchoInput, context: Any) -> PartialResultOutput:
        self.last_context = context
        context.report_partial_result(
            {"job_id": "123", "job_url": "https://aap.example/jobs/123"}
        )
        context.report_partial_result(
            {"job_id": "123", "job_url": "https://aap.example/jobs/123", "status": "running"},
            is_last=True,
        )
        return PartialResultOutput(
            job_id="123",
            job_url="https://aap.example/jobs/123",
            status="complete",
        )


class InvalidPartialResultStep(ActionStep[EchoInput, PartialResultOutput]):
    def run(self, inputs: EchoInput, context: Any) -> PartialResultOutput:
        context.report_partial_result({"status": 42})
        return PartialResultOutput(
            job_id="123",
            job_url="https://aap.example/jobs/123",
            status="complete",
        )


class ControlEchoStep(ControlStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: Any) -> EchoOutput:
        return EchoOutput(
            message=inputs.message,
            invocation_id=context.invocation_id,
            execution_id=context.runtime_context.get("execution_id", ""),
            cancelled=context.cancelled,
        )


class TriggerEchoStep(TriggerStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: Any) -> EchoOutput:
        return EchoOutput(
            message=inputs.message,
            invocation_id=context.invocation_id,
            execution_id=context.runtime_context.get("execution_id", ""),
            cancelled=context.cancelled,
        )


class BulkInput(BaseModel):
    items: list[int]


class BulkOutput(BaseModel):
    count: int


class BulkStep(ActionStep[BulkInput, BulkOutput]):
    def run(self, inputs: BulkInput, context: Any) -> BulkOutput:
        return BulkOutput(count=len(inputs.items))


class SecretOutput(BaseModel):
    secret: str


class LeakyStep(ActionStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: Any) -> EchoOutput:
        return SecretOutput(secret="leaked-secret-value")  # type: ignore[return-value]


class HugeOutput(BaseModel):
    data: str


class HugeStep(ActionStep[EchoInput, HugeOutput]):
    def run(self, inputs: EchoInput, context: Any) -> HugeOutput:
        return HugeOutput(data="x" * 1_048_577)


class RecordingObjectStore:
    """Test object store capturing the spilled payload and content digest."""

    def __init__(self, reference: str = "object-store:result-1") -> None:
        self.reference = reference
        self.payload: bytes | None = None
        self.content_digest: str | None = None

    def store(self, payload: bytes, *, content_digest: str) -> str:
        self.payload = payload
        self.content_digest = content_digest
        return self.reference


def _declared(schema: dict[str, object]) -> dict[str, object]:
    """Inject the Draft 2020-12 declaration authored test schemas need."""

    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}


def _permissive_output_schema() -> dict[str, object]:
    return _declared({"type": "object"})


def _expected_digest(output_schema: dict[str, object] | None = None) -> str:
    return canonical_json_digest(output_schema or _permissive_output_schema())


def _echo_input_schema() -> dict[str, object]:
    return _declared({
        "type": "object",
        "required": ["message"],
        "properties": {"message": {"type": "string"}},
        "additionalProperties": False,
    })


def _bulk_input_schema() -> dict[str, object]:
    return _declared({
        "type": "object",
        "required": ["items"],
        "properties": {"items": {"type": "array", "items": {"type": "integer"}}},
        "additionalProperties": False,
    })


def _installed_step(
    step_identity: str,
    input_schema: dict[str, object],
    output_schema: dict[str, object] | None = None,
    step_class: type[BaseStep[Any, Any]] = ActionEchoStep,
    output_schema_source: str = "inline",
) -> InstalledStepManifest:
    kind = (
        "Action"
        if issubclass(step_class, ActionStep)
        else "Control"
        if issubclass(step_class, ControlStep)
        else "Trigger"
    )
    resolved_output_schema = output_schema or _permissive_output_schema()
    output_document: dict[str, object] = (
        {"kind": "asset", "path": "schemas/output.json"}
        if output_schema_source == "asset"
        else {"kind": "inline", "value": resolved_output_schema}
    )
    specification: dict[str, object] = {
        "runtime": {
            "kind": "custom-runner",
            "entryPoint": f"{step_class.__module__}:{step_class.__qualname__}",
        },
        "input": {"kind": "inline", "value": input_schema},
        "output": output_document,
    }
    if kind == "Control":
        specification["invocation"] = {"kind": "orchestration"}
    elif kind == "Trigger":
        specification.update(
            {
                "driver": "webhook.v1",
                "configuration": {"kind": "inline", "value": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}},
            }
        )
    return InstalledStepManifest.from_manifest(
        step_identity,
        {
            "apiVersion": "syntara.io/v1alpha1",
            "kind": kind,
            "metadata": {
                "name": "echo",
                "displayName": "Echo",
                "description": "An echo step type.",
            },
            "spec": specification,
        },
        {"schemas/output.json": resolved_output_schema}
        if output_schema_source == "asset"
        else {},
    )


def _bind_step(
    bindings: StepBindings,
    step_identity: str,
    step: ActionEchoStep | BulkStep | LeakyStep,
    input_schema: dict[str, object],
) -> None:
    bindings.bind(
        StepDescriptor.for_installed_step(
            step,
            _installed_step(step_identity, input_schema, step_class=type(step)),
        ),
        step,
    )


def _bind_generic_step(
    bindings: StepBindings,
    step_identity: str,
    step: BaseStep[Any, Any],
    input_schema: dict[str, object],
) -> None:
    bindings.bind(
        StepDescriptor.for_installed_step(
            step,
            _installed_step(step_identity, input_schema, step_class=type(step)),
        ),
        step,
    )


def _invocation(
    step_revision_id: str = "acme.plugin.echo",
    *,
    inputs: dict[str, object] | None = None,
    runtime_context: dict[str, object] | None = None,
) -> ContainerInvocation:
    return ContainerInvocation(
        invocation_id="invocation-1",
        step_revision_id=step_revision_id,
        inputs=inputs or {"message": "hello"},
        timeout_seconds=30,
        runtime_context={"execution_id": "workflow-run-1", **(runtime_context or {"environment": "test"})},
    )


def test_container_handler_executes_a_typed_step() -> None:
    handler = container_step_for_installed_manifest(
        ActionEchoStep(),
        installed_step=_installed_step(
            "acme.plugin.echo", _echo_input_schema()
        ),
    )

    result = handler(_invocation())

    assert isinstance(result, ContainerResult)
    assert result.output_schema_digest == _expected_digest()
    assert result.status_code == 0
    assert result.status_message == "Completed"
    assert result.error_result is None
    assert result.error_schema_digest == ""
    assert result.output == {
        "message": "hello",
        "invocation_id": "invocation-1",
        "execution_id": "workflow-run-1",
        "cancelled": False,
    }
def test_direct_and_container_paths_return_identical_standard_output_wrappers() -> None:
    bindings = StepBindings()
    step = ActionEchoStep()
    _bind_step(bindings, "acme.plugin.echo", step, _echo_input_schema())
    binding = bindings.binding("acme.plugin.echo")
    context = ExecutionContext(
        invocation_id="invocation-1",

        runtime_context={"execution_id": "workflow-run-1", "environment": "test"},
    )

    direct = invoke(binding, {"message": "hello"}, context)

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(server, step_registry_from_bindings(bindings))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        container = ContainerClient(channel).execute_wrapped(
            _invocation(), output_schema=_permissive_output_schema()
        )
    finally:
        channel.close()
        server.stop(grace=0).wait()

    assert container == direct
    assert container.status_code == 0
    assert container.error_result is None


def test_step_can_stream_partial_result_snapshots_before_terminal_result() -> None:
    bindings = StepBindings()
    step = PartialResultStep()
    bindings.bind(
        StepDescriptor.for_installed_step(
            step,
            _installed_step(
                "acme.plugin.partial",
                _echo_input_schema(),
                output_schema=_declared(PartialResultOutput.model_json_schema()),
                step_class=PartialResultStep,
            ),
        ),
        step,
    )

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(server, step_registry_from_bindings(bindings))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        events = list(
            ContainerClient(channel).stream(
                _invocation("acme.plugin.partial")
            )
        )
    finally:
        channel.close()
        server.stop(grace=0).wait()

    assert len(events) == 3
    first, second, result = events
    assert isinstance(first, ContainerProgress)
    assert first.state == "progress"
    assert first.sequence == 1
    assert first.partial_result is not None
    assert first.partial_result.is_last is False
    assert first.partial_result.data == {
        "job_id": "123",
        "job_url": "https://aap.example/jobs/123",
    }
    assert isinstance(second, ContainerProgress)
    assert second.state == "progress"
    assert second.sequence == 2
    assert second.partial_result is not None
    assert second.partial_result.is_last is True
    assert second.partial_result.data == {
        "job_id": "123",
        "job_url": "https://aap.example/jobs/123",
        "status": "running",
    }
    assert isinstance(result, ContainerResult)
    assert result.output == {
        "job_id": "123",
        "job_url": "https://aap.example/jobs/123",
        "status": "complete",
    }
    reporter = step.last_context._partial_result_reporter
    assert reporter is not None
    assert reporter._closed
    step.last_context.report_partial_result({"late": True})


def test_partial_result_validator_accepts_object_subsets_and_checks_supplied_values() -> None:
    validator = _partial_result_validator(
        _declared(
            {
                "type": "object",
                "required": ["job_id", "details"],
                "minProperties": 2,
                "$defs": {
                    "ArrayItem": {
                        "type": "object",
                        "required": ["id"],
                        "properties": {"id": {"type": "string"}},
                        "additionalProperties": False,
                    }
                },
                "properties": {
                    "job_id": {"type": "string"},
                    "details": {
                        "type": "object",
                        "required": ["status", "count"],
                        "properties": {
                            "status": {"enum": ["running", "complete"]},
                            "count": {"type": "integer"},
                        },
                        "additionalProperties": False,
                    },
                    "items": {
                        "type": "array",
                        "minItems": 2,
                        "items": {"$ref": "#/$defs/ArrayItem"},
                    },
                },
                "additionalProperties": False,
            }
        )
    )

    validator({})
    validator({"details": {"status": "running"}})
    validator({"job_id": "123", "items": [{"id": "one"}, {"id": "two"}]})

    with pytest.raises(ValueError, match="incompatible with the output schema"):
        validator({"unexpected": "field"})
    with pytest.raises(ValueError, match="incompatible with the output schema"):
        validator({"details": {"status": 42}})
    with pytest.raises(ValueError, match="incompatible with the output schema"):
        validator({"items": [{"id": "one"}]})
    with pytest.raises(ValueError, match="incompatible with the output schema"):
        validator({"items": [{"id": "one"}, {}]})


def test_partial_schema_keeps_scalar_one_of_and_relaxes_object_branch_selection() -> None:
    scalar_union = _partial_result_validator(
        {
            "type": "object",
            "properties": {
                "status": {
                    "oneOf": [
                        {"type": "string"},
                        {"const": "running"},
                    ]
                }
            },
        }
    )
    with pytest.raises(ValueError, match="incompatible with the output schema"):
        scalar_union({"status": "running"})

    object_union = _partial_result_validator(
        {
            "$defs": {
                "Message": {
                    "type": "object",
                    "required": ["kind", "message"],
                    "properties": {"kind": {"const": "message"}, "message": {"type": "string"}},
                },
                "Count": {
                    "type": "object",
                    "required": ["kind", "count"],
                    "properties": {"kind": {"const": "count"}, "count": {"type": "integer"}},
                },
            },
            "oneOf": [
                {"$ref": "#/$defs/Message"},
                {"$ref": "#/$defs/Count"},
            ],
        }
    )
    object_union({})
    object_union({"kind": "message"})

    size_union = _partial_result_validator(
        {"oneOf": [{"minProperties": 1}, {"minProperties": 2}]}
    )
    size_union({})


def test_partial_schema_relaxes_recursive_object_references() -> None:
    validator = _partial_result_validator(
        {
            "$defs": {
                "Node": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {
                        "id": {"type": "string"},
                        "child": {"$ref": "#/$defs/Node"},
                    },
                    "additionalProperties": False,
                }
            },
            "$ref": "#/$defs/Node",
        }
    )

    validator({"child": {}})

    constrained_child = _partial_result_validator(
        {
            "$defs": {
                "Node": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {"id": {"type": "string"}},
                }
            },
            "type": "object",
            "properties": {
                "child": {
                    "$ref": "#/$defs/Node",
                    "maxProperties": 1,
                }
            },
        }
    )
    with pytest.raises(ValueError, match="incompatible with the output schema"):
        constrained_child({"child": {"id": "123", "other": True}})


def test_conditional_output_schema_requires_its_discriminator_for_branch_fields() -> None:
    validator = _partial_result_validator(
        {
            "type": "object",
            "if": {
                "required": ["kind"],
                "properties": {"kind": {"const": "message"}},
            },
            "then": {
                "required": ["kind", "message"],
                "properties": {
                    "kind": {"const": "message"},
                    "message": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "else": {
                "required": ["kind", "count"],
                "properties": {
                    "kind": {"const": "count"},
                    "count": {"type": "integer"},
                },
                "additionalProperties": False,
            },
        }
    )

    validator({})
    validator({"kind": "message", "message": "ready"})
    with pytest.raises(ValueError, match="incompatible with the output schema"):
        validator({"message": "ready"})


def test_step_cannot_report_partial_data_incompatible_with_output_schema() -> None:
    handler = container_step_for_installed_manifest(
        InvalidPartialResultStep(),
        installed_step=_installed_step(
            "acme.plugin.invalid-partial",
            _echo_input_schema(),
            output_schema=_declared(PartialResultOutput.model_json_schema()),
            step_class=InvalidPartialResultStep,
        ),
    )
    emitted: list[object] = []
    invocation = _invocation("acme.plugin.invalid-partial")
    object.__setattr__(
        invocation,
        "_partial_result_reporter",
        _new_partial_result_reporter(
            invocation.invocation_id,
            lambda event: emitted.append(event),
        ),
    )

    with pytest.raises(ContainerInvocationError):
        handler(invocation)

    assert emitted == []


def test_partial_result_validates_and_emits_the_same_json_snapshot() -> None:
    source = {"status": "running"}
    emitted: list[Any] = []
    reporter = _new_partial_result_reporter(
        "invocation-1",
        lambda event: emitted.append(event),
    )

    def mutate_source_after_validation(snapshot: Mapping[str, Any]) -> None:
        assert snapshot == {"status": "running"}
        source["status"] = 42

    reporter.set_validator(mutate_source_after_validation)
    reporter(source)

    assert json.loads(emitted[0].progress.partial_result.data_json) == {
        "status": "running"
    }


def test_container_handler_uses_the_public_runtime_invoke(monkeypatch: pytest.MonkeyPatch) -> None:
    import syntara_plugin.runtime.adapter as adapter_module

    original_invoke = adapter_module.invoke
    calls: list[tuple[Any, dict[str, object], Any]] = []

    def recording_invoke(binding: Any, inputs: dict[str, object], context: Any) -> Any:
        calls.append((binding, inputs, context))
        return original_invoke(binding, inputs, context)

    monkeypatch.setattr(adapter_module, "invoke", recording_invoke)
    handler = container_step_for_installed_manifest(
        ActionEchoStep(),
        installed_step=_installed_step("acme.plugin.echo", _echo_input_schema()),
    )
    invocation = _invocation()

    handler(invocation)

    assert len(calls) == 1
    binding, inputs, context = calls[0]
    assert binding.descriptor.step_identity == invocation.step_revision_id
    assert inputs == invocation.inputs
    assert context.invocation_id == invocation.invocation_id
    assert context.runtime_context.get("execution_id", "") == invocation.runtime_context.get("execution_id", "")


def test_matching_runtime_context_digest_is_accepted() -> None:
    handler = container_step_for_installed_manifest(
        ActionEchoStep(),
        installed_step=_installed_step(
            "acme.plugin.echo", _echo_input_schema()
        ),
    )
    invocation = _invocation(runtime_context={"outputSchemaDigest": _expected_digest()})

    result = handler(invocation)

    assert result.output_schema_digest == _expected_digest()


@pytest.mark.parametrize("output_schema_source", ["inline", "asset"])
def test_mismatched_runtime_context_digest_is_an_abi_violation(
    output_schema_source: str,
) -> None:
    handler = container_step_for_installed_manifest(
        ActionEchoStep(),
        installed_step=_installed_step(
            "acme.plugin.echo",
            _echo_input_schema(),
            output_schema_source=output_schema_source,
        ),
    )
    invocation = _invocation(runtime_context={"outputSchemaDigest": "sha256:" + "a" * 64})

    with pytest.raises(ContainerInvocationError) as raised:
        handler(invocation)

    assert raised.value.code is FailureCode.ABI_VIOLATION
    assert raised.value.outcome == "not_started"


@pytest.mark.parametrize("output_schema_source", ["inline", "asset"])
def test_output_violating_the_manifest_schema_is_an_abi_violation(
    output_schema_source: str,
) -> None:
    handler = container_step_for_installed_manifest(
        ActionEchoStep(),
        installed_step=_installed_step(
            "acme.plugin.echo",
            _echo_input_schema(),
            output_schema=_declared({"type": "object", "required": ["absent_field"]}),
            output_schema_source=output_schema_source,
        ),
    )
    invocation = _invocation()

    with pytest.raises(ContainerInvocationError) as raised:
        handler(invocation)

    assert raised.value.code is FailureCode.ABI_VIOLATION
    assert raised.value.outcome == "unknown"


def test_safe_sdk_execution_error_becomes_a_container_failure_event() -> None:
    handler = container_step_for_installed_manifest(
        LeakyStep(),
        installed_step=_installed_step(
            "acme.plugin.echo", _echo_input_schema(), step_class=LeakyStep
        ),
    )
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(server, StepRegistry({"acme.plugin.echo": handler}))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        client = ContainerClient(channel)
        invocation = _invocation()
        with pytest.raises(ContainerRemoteError) as raised:
            client.execute(invocation)
        wrapped_invocation = _invocation()
        output_schema = _permissive_output_schema()
        with pytest.raises(ContainerRemoteError):
            client.execute_wrapped(wrapped_invocation, output_schema=output_schema)
    finally:
        channel.close()
        server.stop(grace=0).wait()

    assert raised.value.code is FailureCode.ABI_VIOLATION
    assert raised.value.outcome == "unknown"
    assert raised.value.source == "workload"


def test_output_violating_a_format_assertion_is_an_abi_violation() -> None:
    handler = container_step_for_installed_manifest(
        ActionEchoStep(),
        installed_step=_installed_step(
            "acme.plugin.echo",
            _echo_input_schema(),
            output_schema=_declared(
                {
                    "type": "object",
                    "properties": {"message": {"type": "string", "format": "date-time"}},
                }
            ),
        ),
    )
    invocation = _invocation()

    with pytest.raises(ContainerInvocationError) as raised:
        handler(invocation)

    assert raised.value.code is FailureCode.ABI_VIOLATION
    assert raised.value.outcome == "unknown"


def test_oversized_output_is_externalized_through_the_object_store() -> None:
    store = RecordingObjectStore()
    handler = container_step_for_installed_manifest(
        HugeStep(),
        installed_step=_installed_step(
            "acme.plugin.huge", _echo_input_schema(), step_class=HugeStep
        ),
        object_store=store,
    )

    result = handler(_invocation("acme.plugin.huge"))

    assert result.status_code == 0
    assert result.output is None
    assert result.object_reference == "object-store:result-1"
    assert store.payload is not None
    assert len(store.payload) > 1_048_576
    assert store.content_digest is not None
    assert store.content_digest.startswith("sha256:")


def test_wrapper_output_on_a_deliberate_failure_uses_transport_externalization() -> None:
    from syntara_plugin.runtime.adapter import _container_result

    store = RecordingObjectStore(reference="object-store:failed-result-1")
    wrapper = StandardOutputWrapper.failure(
        ErrorMessage(
            category=ErrorCategory.DEPENDENCY,
            code="DEPENDENCY_FAILED",
            message="The upstream service rejected the request.",
            retryable=True,
        ),
        result={"details": "x" * 1_048_577},
    )

    result = _container_result(wrapper, _expected_digest(), canonical_json_digest(error_result_schema()), store)

    assert result.output is None
    assert result.object_reference == "object-store:failed-result-1"
    assert result.status_code == wrapper.status
    assert result.status_message == wrapper.status_message
    assert result.error_result == wrapper.error_result.model_dump(mode="json")
    assert store.payload is not None
    assert store.content_digest is not None


def test_oversized_output_without_an_object_store_is_an_abi_violation() -> None:
    handler = container_step_for_installed_manifest(
        HugeStep(),
        installed_step=_installed_step(
            "acme.plugin.huge", _echo_input_schema(), step_class=HugeStep
        ),
    )
    invocation = _invocation("acme.plugin.huge")

    with pytest.raises(ContainerInvocationError) as raised:
        handler(invocation)

    assert raised.value.code is FailureCode.ABI_VIOLATION
    assert raised.value.outcome == "unknown"


def test_object_store_failure_does_not_retain_the_backend_exception() -> None:
    sentinel = "object-store-backend-secret-sentinel"

    class FailingObjectStore:
        def store(self, payload: bytes, *, content_digest: str) -> str:
            raise OSError(sentinel)

    handler = container_step_for_installed_manifest(
        HugeStep(),
        installed_step=_installed_step(
            "acme.plugin.huge", _echo_input_schema(), step_class=HugeStep
        ),
        object_store=FailingObjectStore(),
    )
    invocation = _invocation("acme.plugin.huge")

    with pytest.raises(ContainerInvocationError) as raised:
        handler(invocation)

    assert sentinel not in str(raised.value)
    assert raised.value.__cause__ is None


def test_object_store_returning_an_invalid_reference_is_an_abi_violation() -> None:
    store = RecordingObjectStore(reference="not a valid identity!!")
    handler = container_step_for_installed_manifest(
        HugeStep(),
        installed_step=_installed_step(
            "acme.plugin.huge", _echo_input_schema(), step_class=HugeStep
        ),
        object_store=store,
    )
    invocation = _invocation("acme.plugin.huge")

    with pytest.raises(ContainerInvocationError) as raised:
        handler(invocation)

    assert raised.value.code is FailureCode.ABI_VIOLATION
    assert raised.value.outcome == "unknown"


def test_oversized_output_round_trips_as_a_reference_over_grpc() -> None:
    store = RecordingObjectStore(reference="object-store:huge-1")
    handler = container_step_for_installed_manifest(
        HugeStep(),
        installed_step=_installed_step(
            "acme.plugin.huge", _echo_input_schema(), step_class=HugeStep
        ),
        object_store=store,
    )

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(server, StepRegistry({"acme.plugin.huge": handler}))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        client = ContainerClient(channel)
        result = client.execute(_invocation("acme.plugin.huge"))
        wrapped_invocation = _invocation("acme.plugin.huge")
        output_schema = _permissive_output_schema()
        with pytest.raises(ValueError, match="resolver is required"):
            client.execute_wrapped(wrapped_invocation, output_schema=output_schema)
        unresolved_invocation = _invocation("acme.plugin.huge")
        with pytest.raises(ContainerRemoteError) as raised:
            client.execute_wrapped(
                unresolved_invocation,
                output_schema=output_schema,
                resolve_object_reference=lambda _reference: None,
            )
        assert raised.value.code is FailureCode.ABI_VIOLATION
        assert raised.value.source == "protocol"

        def resolve(reference: str) -> dict[str, Any]:
            assert reference == "object-store:huge-1"
            assert store.payload is not None
            return json.loads(store.payload)

        wrapper = client.execute_wrapped(
            _invocation("acme.plugin.huge"),
            output_schema=_permissive_output_schema(),
            resolve_object_reference=resolve,
        )
    finally:
        channel.close()
        server.stop(grace=0).wait()

    assert result.status_code == 0
    assert result.output is None
    assert result.object_reference == "object-store:huge-1"
    assert wrapper == StandardOutputWrapper.success({"data": "x" * 1_048_577})


@pytest.mark.parametrize(
    "wrapper",
    [
        StandardOutputWrapper.success({"value": 42}, status_message="Custom success"),
        StandardOutputWrapper.failure(
            ErrorMessage(category="dependency", code="DEPENDENCY_FAILED", message="Try again", retryable=True),
            status_code=7,
            status_message="A distinct status message",
            result={"partial": True},
        ),
        StandardOutputWrapper.failure(
            ErrorMessage(category="validation", code="INVALID_INPUT", message="Invalid input"),
            status_code=1,
        ),
    ],
)
def test_wrapper_round_trips_through_container_client(wrapper: StandardOutputWrapper) -> None:
    def handler(invocation: ContainerInvocation) -> ContainerResult:
        return _container_result(wrapper, _expected_digest(), canonical_json_digest(error_result_schema()), None)

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(server, StepRegistry({"acme.plugin.echo": handler}))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        client = ContainerClient(channel)
        assert client.execute_wrapped(
            _invocation(), output_schema=_permissive_output_schema()
        ) == wrapper
        assert standard_output_from_container_result(
            client.execute(_invocation()), output_schema=_permissive_output_schema()
        ) == wrapper
    finally:
        channel.close()
        server.stop(grace=0).wait()


@pytest.mark.parametrize("invalid", [None, [1], {"value": float("nan")}])
def test_reference_resolution_rejects_invalid_output(invalid: Any) -> None:
    result = ContainerResult(_expected_digest(), object_reference="host:result")
    output_schema = _permissive_output_schema()

    with pytest.raises(ContainerRemoteError) as raised:
        standard_output_from_container_result(
            result,
            output_schema=output_schema,
            resolve_object_reference=lambda _reference: invalid,
        )

    assert raised.value.code is FailureCode.ABI_VIOLATION
    assert raised.value.source == "protocol"
    assert raised.value.outcome == "unknown"


def test_reference_resolution_errors_are_sanitized() -> None:
    result = ContainerResult(_expected_digest(), object_reference="host:result")

    def fail(_reference: str) -> dict[str, Any]:
        raise LookupError("resolver backend secret detail")

    output_schema = _permissive_output_schema()
    with pytest.raises(ValueError, match="could not be resolved") as raised:
        standard_output_from_container_result(
            result,
            output_schema=output_schema,
            resolve_object_reference=fail,
        )

    assert "resolver backend secret detail" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_reference_resolution_preserves_failed_result_metadata() -> None:
    result = ContainerResult(
        _expected_digest(),
        object_reference="host:partial",
        status_code=3,
        status_message="Partial result",
        error_result={"category": "dependency", "code": "DEPENDENCY_FAILED", "message": "Rejected", "retryable": True},
        error_schema_digest=canonical_json_digest(error_result_schema()),
    )

    assert standard_output_from_container_result(
        result,
        output_schema=_permissive_output_schema(),
        resolve_object_reference=lambda reference: {"reference": reference},
    ) == StandardOutputWrapper.failure(
        ErrorMessage(
            category=ErrorCategory.DEPENDENCY,
            code="DEPENDENCY_FAILED",
            message="Rejected",
            retryable=True,
        ),
        status_code=3,
        status_message="Partial result",
        result={"reference": "host:partial"},
    )


def test_failed_container_result_empty_status_message_uses_error_message() -> None:
    result = ContainerResult(
        _expected_digest(),
        status_code=7,
        status_message="",
        error_result={
            "category": "dependency",
            "code": "DEPENDENCY_FAILED",
            "message": "Rejected",
            "retryable": False,
        },
        error_schema_digest=canonical_json_digest(error_result_schema()),
    )

    wrapper = standard_output_from_container_result(
        result,
        output_schema=_permissive_output_schema(),
    )

    assert wrapper.status_message == "Rejected"


def test_inline_container_result_reconstructs_the_standard_wrapper() -> None:
    result = ContainerResult(
        _expected_digest(),
        output={"message": "hello"},
        status_message="Completed",
    )

    assert standard_output_from_container_result(
        result, output_schema=_permissive_output_schema()
    ) == StandardOutputWrapper.success({"message": "hello"}, status_message="Completed")


def test_reconstruction_rejects_a_result_for_a_different_trusted_schema() -> None:
    result = ContainerResult(_expected_digest(), output={"value": "hello"})
    output_schema = {"type": "object", "required": ["value"]}
    with pytest.raises(ContainerRemoteError) as raised:
        standard_output_from_container_result(
            result,
            output_schema=output_schema,
        )

    assert raised.value.code is FailureCode.ABI_VIOLATION
    assert raised.value.source == "protocol"


@pytest.mark.parametrize("use_reference", [False, True], ids=["inline", "resolved-reference"])
def test_reconstruction_rejects_output_that_violates_the_trusted_schema(
    use_reference: bool,
) -> None:
    schema: dict[str, object] = {
        "type": "object",
        "required": ["expected"],
        "properties": {"expected": {"type": "string"}},
    }
    result = (
        ContainerResult(_expected_digest(schema), object_reference="host:result")
        if use_reference
        else ContainerResult(_expected_digest(schema), output={"unexpected": True})
    )
    output_schema = schema

    with pytest.raises(ContainerRemoteError) as raised:
        standard_output_from_container_result(
            result,
            output_schema=output_schema,
            resolve_object_reference=lambda _reference: {"unexpected": True},
        )

    assert raised.value.code is FailureCode.ABI_VIOLATION
    assert raised.value.source == "protocol"


def test_malformed_installed_output_schema_digest_is_a_safe_failure() -> None:
    handler = container_step_for_installed_manifest(
        ActionEchoStep(),
        installed_step=_installed_step(
            "acme.plugin.echo", _echo_input_schema()
        ),
    )
    invocation = _invocation(runtime_context={"outputSchemaDigest": "not-a-digest"})

    with pytest.raises(ContainerInvocationError) as raised:
        handler(invocation)

    assert raised.value.code is FailureCode.ABI_VIOLATION
    assert raised.value.outcome == "not_started"


def test_container_step_rejects_a_non_step_implementation() -> None:
    not_a_step = _expected_digest()
    installed_step = _installed_step(
        "acme.plugin.echo", _echo_input_schema()
    )

    with pytest.raises(TypeError, match="BaseStep"):
        container_step_for_installed_manifest(
            not_a_step,  # type: ignore[arg-type]
            installed_step=installed_step,
        )


def test_invalid_input_is_a_safe_invalid_input_failure() -> None:
    handler = container_step_for_installed_manifest(
        ActionEchoStep(),
        installed_step=_installed_step(
            "acme.plugin.echo", _echo_input_schema()
        ),
    )
    invocation = _invocation(inputs={"message": 42})

    result = handler(invocation)

    assert result.output is None
    assert result.status_code != 0
    assert result.error_result["code"] == FailureCode.INVALID_INPUT.value
    assert result.status_message == result.error_result["message"]
    assert result.error_result["retryable"] is False
    assert "message" in result.error_result["message"]


def test_validation_details_are_bounded_and_do_not_echo_values() -> None:
    handler = container_step_for_installed_manifest(
        BulkStep(),
        installed_step=_installed_step(
            "acme.plugin.bulk", _bulk_input_schema(), step_class=BulkStep
        ),
    )
    invocation = _invocation(
        "acme.plugin.bulk", inputs={"items": ["leaked-secret-value"] * 200}
    )

    result = handler(invocation)

    assert result.error_result["code"] == FailureCode.INVALID_INPUT.value
    assert len(result.error_result["message"]) <= 4096
    assert "and 190 more" in result.error_result["message"]
    assert "leaked-secret-value" not in result.error_result["message"]


def test_small_validation_failures_report_every_stable_code() -> None:
    handler = container_step_for_installed_manifest(
        ActionEchoStep(),
        installed_step=_installed_step("acme.plugin.echo", _echo_input_schema()),
    )
    invocation = _invocation(inputs={"message": 42})

    result = handler(invocation)

    assert "properties.message.type: type" in result.error_result["message"]
    assert "and " not in result.error_result["message"]


def test_undeclared_output_model_is_an_abi_violation() -> None:
    handler = container_step_for_installed_manifest(
        LeakyStep(),
        installed_step=_installed_step(
            "acme.plugin.echo", _echo_input_schema(), step_class=LeakyStep
        ),
    )
    invocation = _invocation()

    with pytest.raises(ContainerInvocationError) as raised:
        handler(invocation)

    assert raised.value.code is FailureCode.ABI_VIOLATION
    assert raised.value.outcome == "unknown"


def test_cancellation_flows_from_the_invocation_to_the_step_context() -> None:
    handler = container_step_for_installed_manifest(
        ActionEchoStep(),
        installed_step=_installed_step(
            "acme.plugin.echo", _echo_input_schema()
        ),
    )
    invocation = _invocation()
    invocation.cancellation.set()

    result = handler(invocation)

    assert result.output is not None
    assert result.output["cancelled"] is True


def test_bindings_step_handler_resolves_by_invocation_identity() -> None:
    bindings = StepBindings()
    step = ActionEchoStep()
    _bind_step(bindings, "acme.plugin.echo", step, _echo_input_schema())

    result = container_step_for_bindings(bindings)(_invocation())

    assert result.output is not None
    assert result.output["message"] == "hello"


def test_bindings_step_handler_reports_unknown_identities_as_not_found() -> None:
    bindings = StepBindings()
    step = ActionEchoStep()
    _bind_step(bindings, "acme.plugin.echo", step, _echo_input_schema())
    handler = container_step_for_bindings(bindings)
    invocation = _invocation("acme.plugin.missing")

    with pytest.raises(ContainerInvocationError) as raised:
        handler(invocation)

    assert raised.value.code is FailureCode.NOT_FOUND


def test_step_registry_from_bindings_advertises_exact_identities() -> None:
    bindings = StepBindings()
    step = ActionEchoStep()
    _bind_step(bindings, "acme.plugin.echo", step, _echo_input_schema())

    steps = step_registry_from_bindings(bindings)

    assert steps.step_revision_ids == ("acme.plugin.echo",)


def test_step_registry_from_bindings_rejects_an_empty_registry() -> None:
    bindings = StepBindings()

    with pytest.raises(ValueError, match="at least one step type"):
        step_registry_from_bindings(bindings)


@pytest.mark.parametrize(
    ("step_class", "step_identity"),
    [
        (ActionEchoStep, "acme.plugin.action-echo"),
        (ControlEchoStep, "acme.plugin.control-echo"),
        (TriggerEchoStep, "acme.plugin.trigger-echo"),
    ],
    ids=["action", "control", "trigger"],
)
def test_typed_steps_dispatch_end_to_end_over_the_container_abi(
    step_class: type[BaseStep[Any, Any]], step_identity: str
) -> None:
    """The container adapter, typed bindings, and gRPC service compose."""

    bindings = StepBindings()
    step = step_class()
    _bind_generic_step(bindings, step_identity, step, _echo_input_schema())

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(server, step_registry_from_bindings(bindings))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        client = ContainerClient(channel)
        assert client.health() == (step_identity,)
        result = client.execute(_invocation(step_identity))
    finally:
        channel.close()
        server.stop(grace=0).wait()

    assert result.output_schema_digest == _expected_digest()
    assert result.output == {
        "message": "hello",
        "invocation_id": "invocation-1",
        "execution_id": "workflow-run-1",
        "cancelled": False,
    }


def test_large_invalid_payloads_remain_invalid_input_over_grpc() -> None:
    """Bounded diagnostics must not degrade a guaranteed input failure."""

    bindings = StepBindings()
    step = BulkStep()
    _bind_step(bindings, "acme.plugin.bulk", step, _bulk_input_schema())

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(server, step_registry_from_bindings(bindings))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        client = ContainerClient(channel)
        invocation = ContainerInvocation(
            invocation_id="invocation-2",
            runtime_context={"execution_id": "workflow-run-2"},
            step_revision_id="acme.plugin.bulk",
            inputs={"items": ["leaked-secret-value"] * 200},
            timeout_seconds=30,
        )
        result = client.execute(invocation)
    finally:
        channel.close()
        server.stop(grace=0).wait()

    assert result.output is None
    assert result.status_code != 0
    assert result.error_result["code"] == FailureCode.INVALID_INPUT.value


def test_registry_dispatches_only_registered_binding_identities() -> None:
    bindings = StepBindings()
    step = ActionEchoStep()
    _bind_step(bindings, "acme.plugin.echo", step, _echo_input_schema())

    steps = step_registry_from_bindings(bindings)

    assert steps.step_revision_ids == ("acme.plugin.echo",)
    result = steps.resolve("acme.plugin.echo")(_invocation())
    assert result.output is not None
    assert result.output["message"] == "hello"
    with pytest.raises(ContainerInvocationError):
        steps.resolve("unknown")


@pytest.mark.parametrize("error, accepted", [
    ({"category": "validation", "code": "INVALID_INPUT", "message": "bad", "retryable": False}, True),
    ({"category": "business", "code": "BUSINESS_RULE_FAILED", "message": "bad", "retryable": False}, False),
    ({"category": "validation", "code": "INVALID_INPUT", "message": "bad", "retryable": False, "extra": "data"}, False),
])
def test_base_error_digest_only_accepts_sdk_validation(error, accepted):
    from syntara_plugin.runtime import error_result_base_schema
    result = ContainerResult(_expected_digest(), status_code=1, status_message="bad", error_result=error, error_schema_digest=canonical_json_digest(error_result_base_schema()))
    schema = {"type": "object", "required": ["reason"]}
    if accepted:
        output_schema = _permissive_output_schema()
        wrapper = standard_output_from_container_result(
            result, output_schema=output_schema, error_schema=schema
        )
        assert wrapper.error.code == "INVALID_INPUT"
    else:
        output_schema = _permissive_output_schema()
        with pytest.raises(ContainerRemoteError):
            standard_output_from_container_result(
                result, output_schema=output_schema, error_schema=schema
            )
