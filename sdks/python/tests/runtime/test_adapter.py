"""Tests for dispatching container invocations to typed Python steps."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import grpc
import pytest
from pydantic import BaseModel
from syntara_plugin.contracts import canonical_json_digest
from syntara_plugin.runtime import (
    ANY_STEP_REVISION,
    ActionStep,
    BaseStep,
    ContainerClient,
    ContainerInvocation,
    ContainerInvocationError,
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
from syntara_plugin.runtime.adapter import _container_result


class EchoInput(BaseModel):
    message: str


class EchoOutput(BaseModel):
    message: str
    invocation_id: str
    workflow_run_id: str
    cancelled: bool


class ActionEchoStep(ActionStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: Any) -> EchoOutput:
        return EchoOutput(
            message=inputs.message,
            invocation_id=context.invocation_id,
            workflow_run_id=context.workflow_run_id,
            cancelled=context.cancelled,
        )


class ControlEchoStep(ControlStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: Any) -> EchoOutput:
        return EchoOutput(
            message=inputs.message,
            invocation_id=context.invocation_id,
            workflow_run_id=context.workflow_run_id,
            cancelled=context.cancelled,
        )


class TriggerEchoStep(TriggerStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: Any) -> EchoOutput:
        return EchoOutput(
            message=inputs.message,
            invocation_id=context.invocation_id,
            workflow_run_id=context.workflow_run_id,
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


def _permissive_output_schema() -> dict[str, object]:
    return {"type": "object"}


def _expected_digest(output_schema: dict[str, object] | None = None) -> str:
    return canonical_json_digest(output_schema or _permissive_output_schema())


def _echo_input_schema() -> dict[str, object]:
    return {
        "type": "object",
        "required": ["message"],
        "properties": {"message": {"type": "string"}},
        "additionalProperties": False,
    }


def _bulk_input_schema() -> dict[str, object]:
    return {
        "type": "object",
        "required": ["items"],
        "properties": {"items": {"type": "array", "items": {"type": "integer"}}},
        "additionalProperties": False,
    }


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
                "configuration": {"kind": "inline", "value": {"type": "object"}},
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
                "description": "An echo Step type.",
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
        workflow_run_id="workflow-run-1",
        step_revision_id=step_revision_id,
        inputs=inputs or {"message": "hello"},
        credential_file_paths={},
        timeout_seconds=30,
        runtime_context=runtime_context or {"environment": "test"},
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
        "workflow_run_id": "workflow-run-1",
        "cancelled": False,
    }


def test_direct_and_container_paths_return_identical_standard_output_wrappers() -> None:
    bindings = StepBindings()
    step = ActionEchoStep()
    _bind_step(bindings, "acme.plugin.echo", step, _echo_input_schema())
    binding = bindings.binding("acme.plugin.echo")
    context = ExecutionContext(
        invocation_id="invocation-1",
        workflow_run_id="workflow-run-1",
        runtime_context={"environment": "test"},
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
    assert container.error_message is None


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
    assert context.workflow_run_id == invocation.workflow_run_id


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
            output_schema={"type": "object", "required": ["absent_field"]},
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
        with pytest.raises(ContainerRemoteError) as raised:
            client.execute(_invocation())
        with pytest.raises(ContainerRemoteError):
            client.execute_wrapped(_invocation(), output_schema=_permissive_output_schema())
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
            output_schema={
                "type": "object",
                "properties": {"message": {"type": "string", "format": "ipv4"}},
            },
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
            code="REMOTE_REJECTED",
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

    with pytest.raises(ContainerInvocationError) as raised:
        handler(_invocation("acme.plugin.huge"))

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
        with pytest.raises(ValueError, match="resolver is required"):
            client.execute_wrapped(
                _invocation("acme.plugin.huge"), output_schema=_permissive_output_schema()
            )
        with pytest.raises(ContainerRemoteError) as raised:
            client.execute_wrapped(
                _invocation("acme.plugin.huge"),
                output_schema=_permissive_output_schema(),
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
            ErrorMessage(code="REMOTE_REJECTED", message="Try again", retryable=True),
            status_code=7,
            status_message="A distinct status message",
            result={"partial": True},
        ),
        StandardOutputWrapper.failure(
            ErrorMessage(code="INVALID_INPUT", message="Invalid input"),
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

    with pytest.raises(ContainerRemoteError) as raised:
        standard_output_from_container_result(
            result,
            output_schema=_permissive_output_schema(),
            resolve_object_reference=lambda _reference: invalid,
        )

    assert raised.value.code is FailureCode.ABI_VIOLATION
    assert raised.value.source == "protocol"
    assert raised.value.outcome == "unknown"


def test_reference_resolution_errors_are_sanitized() -> None:
    result = ContainerResult(_expected_digest(), object_reference="host:result")

    def fail(_reference: str) -> dict[str, Any]:
        raise LookupError("resolver backend secret detail")

    with pytest.raises(ValueError, match="could not be resolved") as raised:
        standard_output_from_container_result(
            result,
            output_schema=_permissive_output_schema(),
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
        error_result={"category": "dependency", "code": "REMOTE_REJECTED", "message": "Rejected", "retryable": True},
        error_schema_digest=canonical_json_digest(error_result_schema()),
    )

    assert standard_output_from_container_result(
        result,
        output_schema=_permissive_output_schema(),
        resolve_object_reference=lambda reference: {"reference": reference},
    ) == StandardOutputWrapper.failure(
        ErrorMessage(
            category=ErrorCategory.DEPENDENCY,
            code="REMOTE_REJECTED",
            message="Rejected",
            retryable=True,
        ),
        status_code=3,
        status_message="Partial result",
        result={"reference": "host:partial"},
    )


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
    with pytest.raises(ContainerRemoteError) as raised:
        standard_output_from_container_result(
            ContainerResult(_expected_digest(), output={"value": "hello"}),
            output_schema={"type": "object", "required": ["value"]},
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

    with pytest.raises(ContainerRemoteError) as raised:
        standard_output_from_container_result(
            result,
            output_schema=schema,
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

    with pytest.raises(ValueError, match="at least one typed step"):
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
        "workflow_run_id": "workflow-run-1",
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
            workflow_run_id="workflow-run-2",
            step_revision_id="acme.plugin.bulk",
            inputs={"items": ["leaked-secret-value"] * 200},
            credential_file_paths={},
            timeout_seconds=30,
        )
        result = client.execute(invocation)
    finally:
        channel.close()
        server.stop(grace=0).wait()

    assert result.output is None
    assert result.status_code != 0
    assert result.error_result["code"] == FailureCode.INVALID_INPUT.value


def test_generic_runner_can_dispatch_a_bindings_fallback_step() -> None:
    bindings = StepBindings()
    step = ActionEchoStep()
    _bind_step(bindings, "acme.plugin.echo", step, _echo_input_schema())

    steps = StepRegistry({}, fallback_step=container_step_for_bindings(bindings))

    assert steps.step_revision_ids == (ANY_STEP_REVISION,)
    result = steps.resolve("acme.plugin.echo")(_invocation())
    assert result.output is not None
    assert result.output["message"] == "hello"
