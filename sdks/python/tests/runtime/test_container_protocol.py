from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any

import grpc
import pytest
from syntara_plugin.runtime import (
    ANY_STEP_REVISION,
    CONTAINER_ABI,
    DEFAULT_MAX_OUTPUT_BYTES,
    MAX_OUTPUT_BYTES_CAPABILITY,
    ContainerClient,
    ContainerInvocation,
    ContainerRemoteError,
    ContainerResult,
    FailureCode,
    StepRegistry,
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
        StepRegistry(
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
        workflow_run_id="workflow-run-1",
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
    assert invocation.max_output_bytes == DEFAULT_MAX_OUTPUT_BYTES
    assert invocation.workflow_run_id == "workflow-run-1"
    assert invocation.inputs == {"repository": "syntara-orchestration/syntara"}
    assert invocation.credential_file_paths == {
        "github-token": "/var/run/syntara/secrets/github-token"
    }
    assert invocation.runtime_context == {
        "driver": "http.v1",
        "operation": {"method": "POST", "path": "/issues"},
        "outputSchemaDigest": OUTPUT_SCHEMA_DIGEST,
    }


@pytest.mark.parametrize("budget", [1, 137, 2_097_152, 4_294_967_295])
def test_capture_budget_survives_generated_protobuf_and_real_grpc(
    runtime: tuple[ContainerClient, container_pb2_grpc.ContainerServiceStub, dict[str, Any]],
    budget: int,
) -> None:
    from syntara_plugin.runtime.container import _request_from_invocation

    client, stub, calls = runtime
    invocation = replace(_invocation("github.create-issue.v1"), max_output_bytes=budget)
    request = _request_from_invocation(invocation)
    decoded = container_pb2.ExecuteRequest.FromString(request.SerializeToString())
    assert decoded.HasField("max_output_bytes")
    assert decoded.max_output_bytes == budget
    assert MAX_OUTPUT_BYTES_CAPABILITY in stub.Health(container_pb2.HealthRequest()).capabilities

    assert client.execute(invocation).output == {"issue": 42}
    assert calls["create"].max_output_bytes == budget


def test_legacy_wire_omission_defaults_but_explicit_zero_never_starts_the_handler(
    runtime: tuple[ContainerClient, container_pb2_grpc.ContainerServiceStub, dict[str, Any]],
) -> None:
    from syntara_plugin.runtime.container import _request_from_invocation

    _client, stub, calls = runtime
    request = _request_from_invocation(_invocation("github.create-issue.v1"))
    request.ClearField("max_output_bytes")
    assert not request.HasField("max_output_bytes")
    assert tuple(stub.Execute(request))[0].WhichOneof("event") == "result"
    assert calls["create"].max_output_bytes == DEFAULT_MAX_OUTPUT_BYTES

    calls.clear()
    request.max_output_bytes = 0
    assert request.HasField("max_output_bytes")
    events = tuple(stub.Execute(request))
    assert len(events) == 1
    assert events[0].failure.code == FailureCode.INVALID_INPUT.value
    assert events[0].failure.outcome == "not_started"
    assert not events[0].failure.retryable
    assert calls == {}


@pytest.mark.parametrize("budget", [-1, 4_294_967_296, 1.5, "1024"])
def test_generated_uint32_binding_rejects_unrepresentable_budgets(budget: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        container_pb2.ExecuteRequest(max_output_bytes=budget)


@pytest.mark.parametrize("budget", [False, True, 0, -1, 1.0, 1.5, "1024", None, 4_294_967_296])
def test_python_invocations_reject_invalid_capture_budgets(budget: Any) -> None:
    with pytest.raises(ValueError, match="max_output_bytes"):
        replace(_invocation("github.create-issue.v1"), max_output_bytes=budget)


def test_bindings_match_the_canonical_proto(tmp_path: Path) -> None:
    root = Path(__file__).parents[4]
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"--proto_path={root / 'contracts/proto'}",
            f"--python_out={tmp_path}",
            f"--grpc_python_out={tmp_path}",
            "syntara_plugin/runtime/protocol/container.proto",
        ],
        check=True,
        capture_output=True,
    )
    for name in ("container_pb2.py", "container_pb2_grpc.py"):
        generated = tmp_path / "syntara_plugin/runtime/protocol" / name
        checked_in = Path(container_pb2.__file__).with_name(name)
        assert generated.read_bytes() == checked_in.read_bytes()


def test_invocation_keeps_existing_positional_arguments() -> None:
    cancellation = Event()
    invocation = ContainerInvocation("i", "w", "s", {}, {}, 30, {"key": "value"}, cancellation)
    assert invocation.runtime_context == {"key": "value"}
    assert invocation.cancellation is cancellation
    assert invocation.max_output_bytes == DEFAULT_MAX_OUTPUT_BYTES


@pytest.mark.parametrize("capabilities", [[], ["unknown.v1"], [MAX_OUTPUT_BYTES_CAPABILITY]])
def test_client_negotiates_before_dispatch_and_never_replays(capabilities: list[str]) -> None:
    calls: list[str] = []

    class Peer(container_pb2_grpc.ContainerServiceServicer):
        def Health(
            self, request: container_pb2.HealthRequest, context: grpc.ServicerContext
        ) -> container_pb2.HealthResponse:
            calls.append("health")
            # Empty capabilities models the old server's Health wire response.
            return container_pb2.HealthResponse(
                ready=True,
                runtime_abi=CONTAINER_ABI,
                step_revision_ids=["github.create-issue.v1"],
                capabilities=capabilities,
            )

        def Execute(
            self, request: container_pb2.ExecuteRequest, context: grpc.ServicerContext
        ) -> None:
            calls.append("execute")
            context.abort(grpc.StatusCode.UNAVAILABLE, "connection lost after dispatch")

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    container_pb2_grpc.add_ContainerServiceServicer_to_server(Peer(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        client = ContainerClient(channel)
        with pytest.raises(ContainerRemoteError) as raised:
            client.execute(replace(_invocation("github.create-issue.v1"), max_output_bytes=137))
        if MAX_OUTPUT_BYTES_CAPABILITY in capabilities:
            assert calls == ["health", "execute"]
            assert raised.value.source == "transport"
        else:
            assert calls == ["health"]
            assert raised.value.code is FailureCode.ABI_VIOLATION
            assert raised.value.source == "protocol"
            assert raised.value.outcome == "not_started"
            assert not raised.value.retryable
            with pytest.raises(ContainerRemoteError):
                client.health()
            assert calls == ["health", "health"]
    finally:
        channel.close()
        server.stop(grace=0).wait()


def test_nonstandard_json_numbers_are_rejected_at_the_container_boundary(
    runtime: tuple[ContainerClient, container_pb2_grpc.ContainerServiceStub, dict[str, Any]],
) -> None:
    _client, stub, calls = runtime
    request = container_pb2.ExecuteRequest(
        invocation_id="invocation-1",
        workflow_run_id="workflow-run-1",
        step_revision_id="github.create-issue.v1",
        runtime_abi=CONTAINER_ABI,
        inputs_json=b'{"value": NaN}',
        credential_file_paths_json=b"{}",
        timeout_seconds=30,
        runtime_context_json=b"{}",
    )

    events = tuple(stub.Execute(request))

    assert len(events) == 1
    assert events[0].failure.code == FailureCode.INVALID_INPUT.value
    assert calls == {}


def test_unsupported_step_is_a_safe_not_found_failure(
    runtime: tuple[ContainerClient, container_pb2_grpc.ContainerServiceStub, dict[str, Any]],
) -> None:
    client, _stub, calls = runtime

    with pytest.raises(ContainerRemoteError) as raised:
        client.execute(_invocation("github.delete-organization.v1"))

    assert raised.value.code is FailureCode.NOT_FOUND
    assert "delete-organization" not in str(raised.value)
    assert calls == {}


def test_platform_runner_can_advertise_and_dispatch_a_control_plane_selected_step() -> None:
    """A generic runner does not need one image build for every installed step."""
    calls: list[ContainerInvocation] = []

    def runner(invocation: ContainerInvocation) -> ContainerResult:
        calls.append(invocation)
        return ContainerResult(OUTPUT_SCHEMA_DIGEST, output={"handled": True})

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(server, StepRegistry({}, fallback_step=runner))
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


def test_unhandled_step_exception_never_exposes_its_message(
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
        workflow_run_id="workflow-run-1",
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


def test_missing_workflow_run_id_is_invalid_input(
    runtime: tuple[ContainerClient, container_pb2_grpc.ContainerServiceStub, dict[str, Any]],
) -> None:
    _client, stub, calls = runtime
    request = container_pb2.ExecuteRequest(
        invocation_id="invocation-1",
        step_revision_id="github.create-issue.v1",
        runtime_abi="syntara.container/v1alpha1",
        inputs_json=b"{}",
        credential_file_paths_json=b"{}",
        timeout_seconds=30,
    )

    events = list(stub.Execute(request))

    assert len(events) == 1
    assert events[0].failure.code == FailureCode.INVALID_INPUT.value
    assert calls == {}


def test_credential_file_paths_must_use_the_approved_secret_mount(
    runtime: tuple[ContainerClient, container_pb2_grpc.ContainerServiceStub, dict[str, Any]],
) -> None:
    _client, stub, _calls = runtime
    request = container_pb2.ExecuteRequest(
        invocation_id="invocation-1",
        workflow_run_id="workflow-run-1",
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


def test_cancellation_reaches_the_selected_step() -> None:
    started = Event()

    def waiting_handler(invocation: ContainerInvocation) -> ContainerResult:
        started.set()
        invocation.cancellation.wait(timeout=1)
        if invocation.cancelled:
            return ContainerResult(OUTPUT_SCHEMA_DIGEST, output={"ignored": True})
        raise RuntimeError("test step was not cancelled")

    server = grpc.server(ThreadPoolExecutor(max_workers=4))
    add_container_runtime_service(server, StepRegistry({"github.wait.v1": waiting_handler}))
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


def test_invocation_timeout_sets_the_step_cancellation_signal() -> None:
    """The required invocation timeout is the default gRPC deadline."""
    started = Event()
    cancelled = Event()

    def waiting_handler(invocation: ContainerInvocation) -> ContainerResult:
        started.set()
        invocation.cancellation.wait(timeout=2)
        if invocation.cancelled:
            cancelled.set()
        return ContainerResult(OUTPUT_SCHEMA_DIGEST, output={"ignored": True})

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(server, StepRegistry({"github.wait.v1": waiting_handler}))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    client = ContainerClient(channel)
    invocation = _invocation("github.wait.v1")
    invocation = ContainerInvocation(
        invocation_id=invocation.invocation_id,
        workflow_run_id=invocation.workflow_run_id,
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


def test_container_result_enforces_terminal_invariants() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        ContainerResult(
            OUTPUT_SCHEMA_DIGEST,
            output={"issue": 1},
            object_reference="object-store:result-1",
        )

    with pytest.raises(ValueError, match="successful result requires"):
        ContainerResult(OUTPUT_SCHEMA_DIGEST)

    failure = ContainerResult(
        OUTPUT_SCHEMA_DIGEST,
        status_code=1,
        status_message="The upstream service rejected the request.",
        error_code="REMOTE_REJECTED",
        error_message="The upstream service rejected the request.",
    )
    assert failure.output is None
    assert failure.object_reference is None

    with pytest.raises(ValueError, match="uppercase failure category"):
        ContainerResult(
            OUTPUT_SCHEMA_DIGEST,
            status_code=1,
            error_code="not-a-code",
            error_message="m",
        )

    with pytest.raises(ValueError, match="successful result must not carry error metadata"):
        ContainerResult(OUTPUT_SCHEMA_DIGEST, output={"issue": 1}, error_code="REMOTE_REJECTED")

    with pytest.raises(ValueError, match="uint32"):
        ContainerResult(
            OUTPUT_SCHEMA_DIGEST,
            status_code=4_294_967_296,
            error_code="X",
            error_message="m",
        )

    with pytest.raises(ValueError, match="failed result requires an error"):
        ContainerResult(OUTPUT_SCHEMA_DIGEST, status_code=7)

    with pytest.raises(ValueError, match="failed result requires an error"):
        ContainerResult(OUTPUT_SCHEMA_DIGEST, status_code=7, error_code="REMOTE_REJECTED")


def test_deliberate_step_failure_round_trips_as_a_result_event() -> None:
    from syntara_plugin.runtime.container import _result_event, _result_from_event

    failure = ContainerResult(
        OUTPUT_SCHEMA_DIGEST,
        status_code=1,
        status_message="The upstream service rejected the request.",
        error_code="REMOTE_REJECTED",
        error_message="The upstream service rejected the request.",
        retryable=True,
    )
    event = _result_event("invocation-1", failure)
    decoded = _result_from_event(event)

    assert decoded.status_code == 1
    assert decoded.output is None
    assert decoded.object_reference is None
    assert decoded.error_code == "REMOTE_REJECTED"
    assert decoded.error_message == "The upstream service rejected the request."
    assert decoded.retryable is True


def test_object_reference_result_round_trips() -> None:
    from syntara_plugin.runtime.container import _result_event, _result_from_event

    result = ContainerResult(OUTPUT_SCHEMA_DIGEST, object_reference="object-store:large-1")
    decoded = _result_from_event(_result_event("invocation-1", result))

    assert decoded.status_code == 0
    assert decoded.output is None
    assert decoded.object_reference == "object-store:large-1"


def test_legacy_result_without_status_fields_decodes_as_success() -> None:
    from syntara_plugin.runtime.container import _result_from_event

    event = container_pb2.ExecutionEvent(
        invocation_id="invocation-1",
        result=container_pb2.Result(
            output_schema_digest=OUTPUT_SCHEMA_DIGEST,
            output_json=b'{"legacy": true}',
        ),
    )
    decoded = _result_from_event(event)

    assert decoded.status_code == 0
    assert decoded.output == {"legacy": True}


def test_failed_result_without_error_metadata_is_a_protocol_error() -> None:
    from syntara_plugin.runtime.container import _result_from_event

    event = container_pb2.ExecutionEvent(
        invocation_id="invocation-1",
        result=container_pb2.Result(
            output_schema_digest=OUTPUT_SCHEMA_DIGEST,
            status_code=7,
        ),
    )
    with pytest.raises(ContainerRemoteError) as raised:
        _result_from_event(event)

    assert raised.value.code is FailureCode.ABI_VIOLATION
    assert raised.value.source == "protocol"


def test_success_result_with_error_metadata_is_a_protocol_error() -> None:
    from syntara_plugin.runtime.container import _result_from_event

    event = container_pb2.ExecutionEvent(
        invocation_id="invocation-1",
        result=container_pb2.Result(
            output_schema_digest=OUTPUT_SCHEMA_DIGEST,
            output_json=b'{"issue": 42}',
            error_code="REMOTE_REJECTED",
        ),
    )
    with pytest.raises(ContainerRemoteError) as raised:
        _result_from_event(event)

    assert raised.value.source == "protocol"
