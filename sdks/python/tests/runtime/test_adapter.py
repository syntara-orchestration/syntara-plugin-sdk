"""Tests for dispatching container invocations to typed Python steps."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import grpc
import pytest
from pydantic import BaseModel
from syntara_plugin.runtime import (
    ANY_STEP_REVISION,
    ActionRegistry,
    ContainerClient,
    ContainerInvocation,
    ContainerInvocationError,
    ContainerResult,
    FailureCode,
    StepDescriptor,
    StepRegistry,
    TaskStep,
    action_registry_from_steps,
    add_container_runtime_service,
    container_action_for_node,
    container_action_for_registry,
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


def _expected_digest() -> str:
    schema = EchoOutput.model_json_schema()
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _invocation(step_revision_id: str = "acme.plugin.echo") -> ContainerInvocation:
    return ContainerInvocation(
        invocation_id="invocation-1",
        step_revision_id=step_revision_id,
        inputs={"message": "hello"},
        credential_file_paths={},
        timeout_seconds=30,
        runtime_context={"environment": "test"},
    )


def test_container_action_executes_a_typed_node() -> None:
    action = container_action_for_node(TaskEchoStep())

    result = action(_invocation())

    assert isinstance(result, ContainerResult)
    assert result.output_schema_digest == _expected_digest()
    assert result.output == {
        "message": "hello",
        "invocation_id": "invocation-1",
        "cancelled": False,
    }


def test_container_action_rejects_a_non_node_implementation() -> None:
    with pytest.raises(TypeError, match="BaseNode"):
        container_action_for_node(_expected_digest)  # type: ignore[arg-type]


def test_invalid_input_is_a_safe_invalid_input_failure() -> None:
    action = container_action_for_node(TaskEchoStep())
    invocation = _invocation()
    object.__setattr__(invocation, "inputs", {"message": 42})

    with pytest.raises(ContainerInvocationError) as raised:
        action(invocation)

    assert raised.value.code is FailureCode.INVALID_INPUT
    assert raised.value.outcome == "not_started"
    assert "message" in raised.value.safe_message


def test_cancellation_flows_from_the_invocation_to_the_step_context() -> None:
    action = container_action_for_node(TaskEchoStep())
    invocation = _invocation()
    invocation.cancellation.set()

    result = action(invocation)

    assert result.output is not None
    assert result.output["cancelled"] is True


def test_registry_action_resolves_by_invocation_identity() -> None:
    registry = StepRegistry()
    node = TaskEchoStep()
    registry.register(StepDescriptor.for_node("acme.plugin.echo", node), node)

    result = container_action_for_registry(registry)(_invocation())

    assert result.output is not None
    assert result.output["message"] == "hello"


def test_registry_action_reports_unknown_identities_as_not_found() -> None:
    registry = StepRegistry()
    node = TaskEchoStep()
    registry.register(StepDescriptor.for_node("acme.plugin.echo", node), node)

    with pytest.raises(ContainerInvocationError) as raised:
        container_action_for_registry(registry)(_invocation("acme.plugin.missing"))

    assert raised.value.code is FailureCode.NOT_FOUND


def test_action_registry_from_steps_advertises_exact_identities() -> None:
    registry = StepRegistry()
    node = TaskEchoStep()
    registry.register(StepDescriptor.for_node("acme.plugin.echo", node), node)

    actions = action_registry_from_steps(registry)

    assert actions.step_revision_ids == ("acme.plugin.echo",)


def test_action_registry_from_steps_rejects_an_empty_registry() -> None:
    with pytest.raises(ValueError, match="at least one typed step"):
        action_registry_from_steps(StepRegistry())


def test_typed_steps_dispatch_end_to_end_over_the_container_abi() -> None:
    """The container adapter, typed registry, and gRPC service compose."""

    registry = StepRegistry()
    node = TaskEchoStep()
    registry.register(StepDescriptor.for_node("acme.plugin.echo", node), node)

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(server, action_registry_from_steps(registry))
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


def test_generic_runner_can_dispatch_a_registry_fallback_action() -> None:
    registry = StepRegistry()
    node = TaskEchoStep()
    registry.register(StepDescriptor.for_node("acme.plugin.echo", node), node)

    actions = ActionRegistry({}, fallback_action=container_action_for_registry(registry))

    assert actions.step_revision_ids == (ANY_STEP_REVISION,)
    result = actions.resolve("acme.plugin.echo")(_invocation())
    assert result.output is not None
    assert result.output["message"] == "hello"
