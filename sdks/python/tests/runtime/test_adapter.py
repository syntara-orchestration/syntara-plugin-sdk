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
    ActionStep,
    ContainerClient,
    ContainerInvocation,
    ContainerInvocationError,
    ContainerResult,
    FailureCode,
    InstalledActionManifest,
    StepBindings,
    StepDescriptor,
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


def _expected_digest() -> str:
    return canonical_json_digest(EchoOutput.model_json_schema())


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


def _installed_action(
    step_identity: str,
    input_schema: dict[str, object],
) -> InstalledActionManifest:
    return InstalledActionManifest.from_manifest(
        step_identity,
        {
            "apiVersion": "syntara.io/v1alpha1",
            "kind": "Action",
            "spec": {"input": {"kind": "inline", "value": input_schema}},
        },
        {},
    )


def _bind_step(
    bindings: StepBindings,
    step_identity: str,
    step: ActionEchoStep | BulkStep | LeakyStep,
    input_schema: dict[str, object],
) -> None:
    bindings.bind(
        StepDescriptor.for_installed_action(
            step,
            _installed_action(step_identity, input_schema),
        ),
        step,
    )


def _invocation(step_revision_id: str = "acme.plugin.echo") -> ContainerInvocation:
    return ContainerInvocation(
        invocation_id="invocation-1",
        workflow_run_id="workflow-run-1",
        step_revision_id=step_revision_id,
        inputs={"message": "hello"},
        credential_file_paths={},
        timeout_seconds=30,
        runtime_context={"environment": "test"},
    )


def test_container_action_executes_a_typed_step() -> None:
    action = container_action_for_step(
        ActionEchoStep(),
        installed_action=_installed_action(
            "acme.plugin.echo", _echo_input_schema()
        ),
    )

    result = action(_invocation())

    assert isinstance(result, ContainerResult)
    assert result.output_schema_digest == _expected_digest()
    assert result.output == {
        "message": "hello",
        "invocation_id": "invocation-1",
        "workflow_run_id": "workflow-run-1",
        "cancelled": False,
    }


def test_container_action_echoes_the_installed_output_schema_digest() -> None:
    action = container_action_for_step(
        ActionEchoStep(),
        installed_action=_installed_action(
            "acme.plugin.echo", _echo_input_schema()
        ),
    )
    invocation = _invocation()
    object.__setattr__(
        invocation,
        "runtime_context",
        {"outputSchemaDigest": "sha256:" + "a" * 64},
    )

    result = action(invocation)

    assert result.output_schema_digest == "sha256:" + "a" * 64


def test_malformed_installed_output_schema_digest_is_a_safe_failure() -> None:
    action = container_action_for_step(
        ActionEchoStep(),
        installed_action=_installed_action(
            "acme.plugin.echo", _echo_input_schema()
        ),
    )
    invocation = _invocation()
    object.__setattr__(invocation, "runtime_context", {"outputSchemaDigest": "not-a-digest"})

    with pytest.raises(ContainerInvocationError) as raised:
        action(invocation)

    assert raised.value.code is FailureCode.ABI_VIOLATION
    assert raised.value.outcome == "not_started"


def test_container_action_rejects_a_non_step_implementation() -> None:
    not_a_step = _expected_digest()
    installed_action = _installed_action(
        "acme.plugin.echo", _echo_input_schema()
    )

    with pytest.raises(TypeError, match="BaseStep"):
        container_action_for_step(
            not_a_step,  # type: ignore[arg-type]
            installed_action=installed_action,
        )


def test_invalid_input_is_a_safe_invalid_input_failure() -> None:
    action = container_action_for_step(
        ActionEchoStep(),
        installed_action=_installed_action(
            "acme.plugin.echo", _echo_input_schema()
        ),
    )
    invocation = _invocation()
    object.__setattr__(invocation, "inputs", {"message": 42})

    result = action(invocation)

    assert result.output is not None
    assert result.output["status"] != 0
    assert result.output["error"]["code"] == FailureCode.INVALID_INPUT.value
    assert "message" in result.output["error"]["message"]


def test_validation_details_are_bounded_and_do_not_echo_values() -> None:
    action = container_action_for_step(
        BulkStep(),
        installed_action=_installed_action(
            "acme.plugin.bulk", _bulk_input_schema()
        ),
    )
    invocation = _invocation("acme.plugin.bulk")
    object.__setattr__(
        invocation, "inputs", {"items": ["leaked-secret-value"] * 200}
    )

    result = action(invocation)

    assert result.output is not None
    error = result.output["error"]
    assert error["code"] == FailureCode.INVALID_INPUT.value
    assert len(error["message"]) <= 4096
    assert "and 190 more" in error["message"]
    assert "leaked-secret-value" not in error["message"]


def test_small_validation_failures_report_every_stable_code() -> None:
    action = container_action_for_step(
        ActionEchoStep(),
        installed_action=_installed_action(
            "acme.plugin.echo", _echo_input_schema()
        ),
    )
    invocation = _invocation()
    object.__setattr__(invocation, "inputs", {"message": 42})

    result = action(invocation)

    assert result.output is not None
    assert "properties.message.type: type" in result.output["error"]["message"]
    assert "and " not in result.output["error"]["message"]


def test_undeclared_output_model_is_an_abi_violation() -> None:
    action = container_action_for_step(
        LeakyStep(),
        installed_action=_installed_action(
            "acme.plugin.echo", _echo_input_schema()
        ),
    )
    invocation = _invocation()

    with pytest.raises(ContainerInvocationError) as raised:
        action(invocation)

    assert raised.value.code is FailureCode.ABI_VIOLATION
    assert raised.value.outcome == "unknown"


def test_cancellation_flows_from_the_invocation_to_the_step_context() -> None:
    action = container_action_for_step(
        ActionEchoStep(),
        installed_action=_installed_action(
            "acme.plugin.echo", _echo_input_schema()
        ),
    )
    invocation = _invocation()
    invocation.cancellation.set()

    result = action(invocation)

    assert result.output is not None
    assert result.output["cancelled"] is True


def test_bindings_action_resolves_by_invocation_identity() -> None:
    bindings = StepBindings()
    step = ActionEchoStep()
    _bind_step(bindings, "acme.plugin.echo", step, _echo_input_schema())

    result = container_action_for_bindings(bindings)(_invocation())

    assert result.output is not None
    assert result.output["message"] == "hello"


def test_bindings_action_reports_unknown_identities_as_not_found() -> None:
    bindings = StepBindings()
    step = ActionEchoStep()
    _bind_step(bindings, "acme.plugin.echo", step, _echo_input_schema())
    action = container_action_for_bindings(bindings)
    invocation = _invocation("acme.plugin.missing")

    with pytest.raises(ContainerInvocationError) as raised:
        action(invocation)

    assert raised.value.code is FailureCode.NOT_FOUND


def test_action_registry_from_steps_advertises_exact_identities() -> None:
    bindings = StepBindings()
    step = ActionEchoStep()
    _bind_step(bindings, "acme.plugin.echo", step, _echo_input_schema())

    actions = action_registry_from_steps(bindings)

    assert actions.step_revision_ids == ("acme.plugin.echo",)


def test_action_registry_from_steps_rejects_an_empty_registry() -> None:
    bindings = StepBindings()

    with pytest.raises(ValueError, match="at least one typed step"):
        action_registry_from_steps(bindings)


def test_typed_steps_dispatch_end_to_end_over_the_container_abi() -> None:
    """The container adapter, typed bindings, and gRPC service compose."""

    bindings = StepBindings()
    step = ActionEchoStep()
    _bind_step(bindings, "acme.plugin.echo", step, _echo_input_schema())

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
        "workflow_run_id": "workflow-run-1",
        "cancelled": False,
    }


def test_large_invalid_payloads_remain_invalid_input_over_grpc() -> None:
    """Bounded diagnostics must not degrade a guaranteed input failure."""

    bindings = StepBindings()
    step = BulkStep()
    _bind_step(bindings, "acme.plugin.bulk", step, _bulk_input_schema())

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(server, action_registry_from_steps(bindings))
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

    assert result.output is not None
    assert result.output["status"] != 0
    assert result.output["error"]["code"] == FailureCode.INVALID_INPUT.value


def test_generic_runner_can_dispatch_a_bindings_fallback_action() -> None:
    bindings = StepBindings()
    step = ActionEchoStep()
    _bind_step(bindings, "acme.plugin.echo", step, _echo_input_schema())

    actions = ActionRegistry({}, fallback_action=container_action_for_bindings(bindings))

    assert actions.step_revision_ids == (ANY_STEP_REVISION,)
    result = actions.resolve("acme.plugin.echo")(_invocation())
    assert result.output is not None
    assert result.output["message"] == "hello"
