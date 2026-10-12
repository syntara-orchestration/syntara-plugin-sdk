from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any

import grpc
import pytest
from syntara_plugin.runtime import (
    ANY_STEP_REVISION,
    ActionRegistry,
    ContainerClient,
    ContainerInvocation,
    ContainerRemoteError,
    ContainerResult,
    FailureCode,
    add_container_runtime_service,
)
from syntara_plugin.runtime.protocol import container_pb2, container_pb2_grpc

OUTPUT_SCHEMA_DIGEST = "sha256:" + "a" * 64


@pytest.fixture
def runtime() -> tuple[ContainerClient, container_pb2_grpc.ContainerServiceStub, dict[str, Any]]:
    calls: dict[str, Any] = {}

    def create_issue(invocation: ContainerInvocation) -> ContainerResult:
        calls["create"] = invocation
        return ContainerResult(OUTPUT_SCHEMA_DIGEST, output={"issue": 42})

    def close_issue(invocation: ContainerInvocation) -> ContainerResult:
        calls["close"] = invocation
        return ContainerResult(OUTPUT_SCHEMA_DIGEST, output={"closed": True})

    def unsafe_failure(invocation: ContainerInvocation) -> ContainerResult:
        raise RuntimeError("github-token=plaintext-value")

    server = grpc.server(ThreadPoolExecutor(max_workers=4))
    add_container_runtime_service(
        server,
        ActionRegistry(
            {
                "github.create-issue.v1": create_issue,
                "github.close-issue.v1": close_issue,
                "github.unsafe-failure.v1": unsafe_failure,
            }
        ),
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield ContainerClient(channel), container_pb2_grpc.ContainerServiceStub(channel), calls
    finally:
        channel.close()
        server.stop(grace=0).wait()


def _invocation(step_revision_id: str) -> ContainerInvocation:
    return ContainerInvocation(
        invocation_id="invocation-1",
        step_revision_id=step_revision_id,
        inputs={"repository": "syntara-orchestration/syntara"},
        credential_file_paths={"github-token": "/var/run/syntara/secrets/github-token"},
        timeout_seconds=30,
        runtime_context={
            "driver": "http.v1",
            "operation": {"method": "POST", "path": "/issues"},
            "outputSchemaDigest": OUTPUT_SCHEMA_DIGEST,
        },
    )


def test_one_image_dispatches_the_selected_step_revision(
    runtime: tuple[ContainerClient, container_pb2_grpc.ContainerServiceStub, dict[str, Any]],
) -> None:
    client, _stub, calls = runtime

    assert client.health() == (
        "github.close-issue.v1",
        "github.create-issue.v1",
        "github.unsafe-failure.v1",
    )
    result = client.execute(_invocation("github.create-issue.v1"))

    assert result.output == {"issue": 42}
    assert set(calls) == {"create"}
    invocation = calls["create"]
    assert invocation.inputs == {"repository": "syntara-orchestration/syntara"}
    assert invocation.credential_file_paths == {
        "github-token": "/var/run/syntara/secrets/github-token"
    }
    assert invocation.runtime_context == {
        "driver": "http.v1",
        "operation": {"method": "POST", "path": "/issues"},
        "outputSchemaDigest": OUTPUT_SCHEMA_DIGEST,
    }


def test_unsupported_action_is_a_safe_not_found_failure(
    runtime: tuple[ContainerClient, container_pb2_grpc.ContainerServiceStub, dict[str, Any]],
) -> None:
    client, _stub, calls = runtime

    with pytest.raises(ContainerRemoteError) as raised:
        client.execute(_invocation("github.delete-organization.v1"))

    assert raised.value.code is FailureCode.NOT_FOUND
    assert "delete-organization" not in str(raised.value)
    assert calls == {}


def test_platform_runner_can_advertise_and_dispatch_a_control_plane_selected_action() -> None:
    """A generic runner does not need one image build for every installed action."""
    calls: list[ContainerInvocation] = []

    def runner(invocation: ContainerInvocation) -> ContainerResult:
        calls.append(invocation)
        return ContainerResult(OUTPUT_SCHEMA_DIGEST, output={"handled": True})

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(server, ActionRegistry({}, fallback_action=runner))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    client = ContainerClient(channel)
    try:
        assert client.health() == (ANY_STEP_REVISION,)
        result = client.execute(_invocation("syntara.github.create-issue:" + "b" * 64))
    finally:
        channel.close()
        server.stop(grace=0).wait()

    assert result.output == {"handled": True}
    assert calls[0].step_revision_id == "syntara.github.create-issue:" + "b" * 64


def test_unhandled_action_exception_never_exposes_its_message(
    runtime: tuple[ContainerClient, container_pb2_grpc.ContainerServiceStub, dict[str, Any]],
) -> None:
    client, _stub, _calls = runtime

    with pytest.raises(ContainerRemoteError) as raised:
        client.execute(_invocation("github.unsafe-failure.v1"))

    assert raised.value.code is FailureCode.OUTCOME_UNKNOWN
    assert "plaintext-value" not in str(raised.value)


def test_invalid_abi_is_returned_as_a_terminal_failure(
    runtime: tuple[ContainerClient, container_pb2_grpc.ContainerServiceStub, dict[str, Any]],
) -> None:
    _client, stub, _calls = runtime
    request = container_pb2.ExecuteRequest(
        invocation_id="invocation-1",
        step_revision_id="github.create-issue.v1",
        runtime_abi="syntara.container/v999",
        inputs_json=b"{}",
        credential_file_paths_json=b"{}",
        timeout_seconds=30,
    )

    events = list(stub.Execute(request))

    assert len(events) == 1
    assert events[0].failure.code == FailureCode.ABI_VIOLATION.value
    assert "v999" not in events[0].failure.message


def test_credential_file_paths_must_use_the_approved_secret_mount(
    runtime: tuple[ContainerClient, container_pb2_grpc.ContainerServiceStub, dict[str, Any]],
) -> None:
    _client, stub, _calls = runtime
    request = container_pb2.ExecuteRequest(
        invocation_id="invocation-1",
        step_revision_id="github.create-issue.v1",
        runtime_abi="syntara.container/v1alpha1",
        inputs_json=b"{}",
        credential_file_paths_json=b'{"github-token":"/tmp/github-token"}',
        timeout_seconds=30,
    )

    events = list(stub.Execute(request))

    assert len(events) == 1
    assert events[0].failure.code == FailureCode.INVALID_INPUT.value
    assert "/tmp" not in events[0].failure.message


def test_cancellation_reaches_the_selected_action() -> None:
    started = Event()

    def waiting_action(invocation: ContainerInvocation) -> ContainerResult:
        started.set()
        invocation.cancellation.wait(timeout=1)
        if invocation.cancelled:
            return ContainerResult(OUTPUT_SCHEMA_DIGEST, output={"ignored": True})
        raise RuntimeError("test action was not cancelled")

    server = grpc.server(ThreadPoolExecutor(max_workers=4))
    add_container_runtime_service(server, ActionRegistry({"github.wait.v1": waiting_action}))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    client = ContainerClient(channel)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(client.execute, _invocation("github.wait.v1"))
            assert started.wait(timeout=1)
            assert client.cancel("invocation-1") is True
            with pytest.raises(ContainerRemoteError) as raised:
                pending.result(timeout=1)
    finally:
        channel.close()
        server.stop(grace=0).wait()

    assert raised.value.code is FailureCode.CANCELLED


def test_invocation_timeout_sets_the_action_cancellation_signal() -> None:
    """The required invocation timeout is the default gRPC deadline."""
    started = Event()
    cancelled = Event()

    def waiting_action(invocation: ContainerInvocation) -> ContainerResult:
        started.set()
        invocation.cancellation.wait(timeout=2)
        if invocation.cancelled:
            cancelled.set()
        return ContainerResult(OUTPUT_SCHEMA_DIGEST, output={"ignored": True})

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(server, ActionRegistry({"github.wait.v1": waiting_action}))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    client = ContainerClient(channel)
    invocation = _invocation("github.wait.v1")
    invocation = ContainerInvocation(
        invocation_id=invocation.invocation_id,
        step_revision_id=invocation.step_revision_id,
        inputs=invocation.inputs,
        credential_file_paths=invocation.credential_file_paths,
        timeout_seconds=1,
        runtime_context=invocation.runtime_context,
    )
    try:
        with pytest.raises(ContainerRemoteError) as raised:
            client.execute(invocation)
        assert cancelled.wait(timeout=1)
    finally:
        channel.close()
        server.stop(grace=0).wait()

    assert started.is_set()
    assert raised.value.code is FailureCode.TIMEOUT
