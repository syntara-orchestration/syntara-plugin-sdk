"""Tests for dispatching container invocations to typed Python steps."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import grpc
import pytest
from pydantic import BaseModel
from syntara_plugin.contracts import canonical_json_digest
from syntara_plugin.runtime import (
    ANY_STEP_REVISION,
    ActionRegistry,
    ContainerClient,
    ContainerInvocation,
    ContainerInvocationError,
    ContainerRemoteError,
    ContainerResult,
    FailureCode,
    StepBindings,
    StepDescriptor,
    TaskStep,
    action_registry_from_steps,
    add_container_runtime_service,
    container_action_for_bindings,
    container_action_for_step,
)


class EchoInput(BaseModel):
    message: str


class EchoOutput(BaseModel):
    message: str
    invocation_id: str
    cancelled: bool


class TaskEchoStep(TaskStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: Any) -> EchoOutput:
        return EchoOutput(
            message=inputs.message,
            invocation_id=context.invocation_id,
            cancelled=context.cancelled,
        )


class BulkInput(BaseModel):
    items: list[int]


class BulkOutput(BaseModel):
    count: int


class BulkStep(TaskStep[BulkInput, BulkOutput]):
    def run(self, inputs: BulkInput, context: Any) -> BulkOutput:
        return BulkOutput(count=len(inputs.items))


class SecretOutput(BaseModel):
    secret: str


class LeakyStep(TaskStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: Any) -> EchoOutput:
        return SecretOutput(secret="leaked-secret-value")  # type: ignore[return-value]


def _expected_digest() -> str:
    return canonical_json_digest(EchoOutput.model_json_schema())


def _invocation(step_revision_id: str = "acme.plugin.echo") -> ContainerInvocation:
    return ContainerInvocation(
        invocation_id="invocation-1",
        step_revision_id=step_revision_id,
        inputs={"message": "hello"},
        credential_file_paths={},
        timeout_seconds=30,
        runtime_context={"environment": "test"},
    )


def test_container_action_executes_a_typed_step() -> None:
    action = container_action_for_step(TaskEchoStep())

    result = action(_invocation())

    assert isinstance(result, ContainerResult)
    assert result.output_schema_digest == _expected_digest()
    assert result.output == {
        "message": "hello",
        "invocation_id": "invocation-1",
        "cancelled": False,
    }


def test_container_action_echoes_the_installed_output_schema_digest() -> None:
    action = container_action_for_step(TaskEchoStep())
    invocation = _invocation()
    object.__setattr__(
        invocation,
        "runtime_context",
        {"outputSchemaDigest": "sha256:" + "a" * 64},
    )

    result = action(invocation)

    assert result.output_schema_digest == "sha256:" + "a" * 64


def test_malformed_installed_output_schema_digest_is_a_safe_failure() -> None:
    action = container_action_for_step(TaskEchoStep())
    invocation = _invocation()
    object.__setattr__(invocation, "runtime_context", {"outputSchemaDigest": "not-a-digest"})

    with pytest.raises(ContainerInvocationError) as raised:
        action(invocation)

    assert raised.value.code is FailureCode.ABI_VIOLATION
    assert raised.value.outcome == "not_started"


def test_container_action_rejects_a_non_step_implementation() -> None:
    not_a_step = _expected_digest()

    with pytest.raises(TypeError, match="BaseStep"):
        container_action_for_step(not_a_step)  # type: ignore[arg-type]


def test_invalid_input_is_a_safe_invalid_input_failure() -> None:
    action = container_action_for_step(TaskEchoStep())
    invocation = _invocation()
    object.__setattr__(invocation, "inputs", {"message": 42})

    with pytest.raises(ContainerInvocationError) as raised:
        action(invocation)

    assert raised.value.code is FailureCode.INVALID_INPUT
    assert raised.value.outcome == "not_started"
    assert "message" in raised.value.safe_message


def test_validation_details_are_bounded_and_do_not_echo_values() -> None:
    action = container_action_for_step(BulkStep())
    invocation = _invocation()
    object.__setattr__(
        invocation, "inputs", {"items": ["leaked-secret-value"] * 200}
    )

    with pytest.raises(ContainerInvocationError) as raised:
        action(invocation)

    error = raised.value
    assert error.code is FailureCode.INVALID_INPUT
    assert error.outcome == "not_started"
    assert len(error.safe_message) <= 4096
    assert "and 190 more" in error.safe_message
    assert "leaked-secret-value" not in error.safe_message


def test_small_validation_failures_report_every_stable_code() -> None:
    action = container_action_for_step(TaskEchoStep())
    invocation = _invocation()
    object.__setattr__(invocation, "inputs", {"message": 42})

    with pytest.raises(ContainerInvocationError) as raised:
        action(invocation)

    assert "message: string_type" in raised.value.safe_message
    assert "and " not in raised.value.safe_message


def test_undeclared_output_model_is_an_abi_violation() -> None:
    action = container_action_for_step(LeakyStep())
    invocation = _invocation()

    with pytest.raises(ContainerInvocationError) as raised:
        action(invocation)

    assert raised.value.code is FailureCode.ABI_VIOLATION
    assert raised.value.outcome == "unknown"


def test_cancellation_flows_from_the_invocation_to_the_step_context() -> None:
    action = container_action_for_step(TaskEchoStep())
    invocation = _invocation()
    invocation.cancellation.set()

    result = action(invocation)

    assert result.output is not None
    assert result.output["cancelled"] is True


def test_bindings_action_resolves_by_invocation_identity() -> None:
    bindings = StepBindings()
    step = TaskEchoStep()
    bindings.bind(StepDescriptor.for_step("acme.plugin.echo", step), step)

    result = container_action_for_bindings(bindings)(_invocation())

    assert result.output is not None
    assert result.output["message"] == "hello"


def test_bindings_action_reports_unknown_identities_as_not_found() -> None:
    bindings = StepBindings()
    step = TaskEchoStep()
    bindings.bind(StepDescriptor.for_step("acme.plugin.echo", step), step)
    action = container_action_for_bindings(bindings)
    invocation = _invocation("acme.plugin.missing")

    with pytest.raises(ContainerInvocationError) as raised:
        action(invocation)

    assert raised.value.code is FailureCode.NOT_FOUND


def test_action_registry_from_steps_advertises_exact_identities() -> None:
    bindings = StepBindings()
    step = TaskEchoStep()
    bindings.bind(StepDescriptor.for_step("acme.plugin.echo", step), step)

    actions = action_registry_from_steps(bindings)

    assert actions.step_revision_ids == ("acme.plugin.echo",)


def test_action_registry_from_steps_rejects_an_empty_registry() -> None:
    bindings = StepBindings()

    with pytest.raises(ValueError, match="at least one typed step"):
        action_registry_from_steps(bindings)


def test_typed_steps_dispatch_end_to_end_over_the_container_abi() -> None:
    """The container adapter, typed bindings, and gRPC service compose."""

    bindings = StepBindings()
    step = TaskEchoStep()
    bindings.bind(StepDescriptor.for_step("acme.plugin.echo", step), step)

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(server, action_registry_from_steps(bindings))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        client = ContainerClient(channel)
        assert client.health() == ("acme.plugin.echo",)
        result = client.execute(_invocation())
    finally:
        channel.close()
        server.stop(grace=0).wait()

    assert result.output_schema_digest == _expected_digest()
    assert result.output == {
        "message": "hello",
        "invocation_id": "invocation-1",
        "cancelled": False,
    }


def test_large_invalid_payloads_remain_invalid_input_over_grpc() -> None:
    """Bounded diagnostics must not degrade a guaranteed input failure."""

    bindings = StepBindings()
    step = BulkStep()
    bindings.bind(StepDescriptor.for_step("acme.plugin.bulk", step), step)

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(server, action_registry_from_steps(bindings))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        client = ContainerClient(channel)
        invocation = ContainerInvocation(
            invocation_id="invocation-2",
            step_revision_id="acme.plugin.bulk",
            inputs={"items": ["leaked-secret-value"] * 200},
            credential_file_paths={},
            timeout_seconds=30,
        )
        with pytest.raises(ContainerRemoteError) as raised:
            client.execute(invocation)
    finally:
        channel.close()
        server.stop(grace=0).wait()

    assert raised.value.code is FailureCode.INVALID_INPUT
    assert raised.value.outcome == "not_started"


def test_generic_runner_can_dispatch_a_bindings_fallback_action() -> None:
    bindings = StepBindings()
    step = TaskEchoStep()
    bindings.bind(StepDescriptor.for_step("acme.plugin.echo", step), step)

    actions = ActionRegistry({}, fallback_action=container_action_for_bindings(bindings))

    assert actions.step_revision_ids == (ANY_STEP_REVISION,)
    result = actions.resolve("acme.plugin.echo")(_invocation())
    assert result.output is not None
    assert result.output["message"] == "hello"
