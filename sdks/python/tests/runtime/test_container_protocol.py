from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import grpc
import pytest
from syntara_plugin.runtime import (
    CONTAINER_ABI,
    ContainerClient,
    ContainerInvocation,
    ContainerRemoteError,
    ContainerResult,
    FailureCode,
    StepRegistry,
    add_container_runtime_service,
)
from syntara_plugin.runtime.container import _request_from_invocation
from syntara_plugin.runtime.protocol import container_pb2, container_pb2_grpc

OUTPUT_SCHEMA_DIGEST = "sha256:" + "a" * 64
ERROR_SCHEMA_DIGEST = "sha256:" + "b" * 64
ERROR_RESULT = {"category": "dependency", "code": "REMOTE_REJECTED", "message": "Rejected", "retryable": True, "details": {"attempts": [1, 2]}}


def test_generated_protocol_package_matches_container_abi() -> None:
    assert container_pb2.DESCRIPTOR.package == CONTAINER_ABI.replace("/", ".")


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

    wrapped_invocation = _invocation("github.delete-organization.v1")
    with pytest.raises(ContainerRemoteError) as wrapped_error:
        client.execute_wrapped(
            wrapped_invocation, output_schema={"type": "object"}
        )
    assert wrapped_error.value.code is FailureCode.NOT_FOUND
    assert wrapped_error.value.source == "workload"


def test_runner_advertises_and_dispatches_only_the_selected_step() -> None:
    calls: list[ContainerInvocation] = []

    def runner(invocation: ContainerInvocation) -> ContainerResult:
        calls.append(invocation)
        return ContainerResult(OUTPUT_SCHEMA_DIGEST, output={"handled": True})

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    revision = "syntara.github.create-issue:" + "b" * 64
    add_container_runtime_service(server, StepRegistry({revision: runner}))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    client = ContainerClient(channel)
    try:
        assert client.health() == (revision,)
        result = client.execute(_invocation(revision))
        unknown_invocation = _invocation("unknown")
        with pytest.raises(ContainerRemoteError) as raised:
            client.execute(unknown_invocation)
        assert raised.value.code is FailureCode.NOT_FOUND
    finally:
        channel.close()
        server.stop(grace=0).wait()

    assert result.output == {"handled": True}
    assert calls[0].step_revision_id == revision


def test_unhandled_step_exception_never_exposes_its_message(
    runtime: tuple[ContainerClient, container_pb2_grpc.ContainerServiceStub, dict[str, Any]],
) -> None:
    client, _stub, _calls = runtime

    with pytest.raises(ContainerRemoteError) as raised:
        client.execute(_invocation("github.unsafe-failure.v1"))

    assert raised.value.code is FailureCode.OUTCOME_UNKNOWN
    assert "plaintext-value" not in str(raised.value)


def test_normalized_transport_error_does_not_retain_the_rpc_exception() -> None:
    sentinel = "transport-backend-secret-sentinel"

    class SentinelRpcError(grpc.RpcError):
        def code(self) -> grpc.StatusCode:
            return grpc.StatusCode.UNAVAILABLE

        def details(self) -> str:
            return sentinel

    class FailingStub:
        def Execute(self, *_args: Any, **_kwargs: Any) -> Any:
            raise SentinelRpcError()

    client = ContainerClient.__new__(ContainerClient)
    client._stub = FailingStub()
    invocation = _invocation("github.create-issue.v1")

    with pytest.raises(ContainerRemoteError) as raised:
        client.execute(invocation)

    assert raised.value.source == "transport"
    assert raised.value.code is FailureCode.UPSTREAM_TRANSIENT
    assert sentinel not in str(raised.value)
    assert raised.value.__cause__ is None


def test_invalid_abi_is_returned_as_a_terminal_failure(
    runtime: tuple[ContainerClient, container_pb2_grpc.ContainerServiceStub, dict[str, Any]],
) -> None:
    _client, stub, _calls = runtime
    request = container_pb2.ExecuteRequest(
        invocation_id="invocation-1",
        workflow_run_id="workflow-run-1",
        step_revision_id="github.create-issue.v1",
        runtime_abi="syntara.provider/v1alpha1",
        inputs_json=b"{}",
        credential_file_paths_json=b"{}",
        timeout_seconds=30,
    )

    events = list(stub.Execute(request))

    assert len(events) == 1
    assert events[0].failure.code == FailureCode.ABI_VIOLATION.value
    assert "v1alpha1" not in events[0].failure.message


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


def _credential_request(path: str) -> container_pb2.ExecuteRequest:
    return container_pb2.ExecuteRequest(
        invocation_id="invocation-1",
        workflow_run_id="workflow-run-1",
        step_revision_id="github.create-issue.v1",
        runtime_abi="syntara.container/v1alpha1",
        inputs_json=b"{}",
        credential_file_paths_json=json.dumps({"github-token": path}).encode("utf-8"),
        timeout_seconds=30,
    )


def test_a_local_credential_directory_is_refused_unless_the_runtime_opts_in(
    runtime: tuple[ContainerClient, container_pb2_grpc.ContainerServiceStub, dict[str, Any]],
    tmp_path: Path,
) -> None:
    """The production rule is the default: opting in is the only way past it."""

    _client, stub, _calls = runtime
    token = tmp_path / "github-token"
    token.write_text("{}", encoding="utf-8")

    events = list(stub.Execute(_credential_request(str(token))))

    assert len(events) == 1
    assert events[0].failure.code == FailureCode.INVALID_INPUT.value
    assert str(tmp_path) not in events[0].failure.message


def test_an_opted_in_runtime_accepts_a_file_in_its_local_credential_directory(
    tmp_path: Path,
) -> None:
    calls: dict[str, Any] = {}

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        calls["invocation"] = invocation
        return ContainerResult(OUTPUT_SCHEMA_DIGEST, output={"issue": 42})

    token = tmp_path / "github-token"
    token.write_text("{}", encoding="utf-8")
    server = grpc.server(ThreadPoolExecutor(max_workers=1))
    add_container_runtime_service(
        server, StepRegistry({"github.create-issue.v1": handler}), tmp_path
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        stub = container_pb2_grpc.ContainerServiceStub(channel)
        events = list(stub.Execute(_credential_request(str(token))))
    finally:
        server.stop(None)

    assert [event.WhichOneof("event") for event in events] == ["result"]
    assert calls["invocation"].credential_file_paths == {"github-token": str(token)}


@pytest.mark.parametrize(
    "name",
    ["escapes-the-directory", "reaches-a-nested-file", "leaves-through-a-symlink"],
)
def test_an_opted_in_runtime_still_refuses_paths_outside_its_directory(
    tmp_path: Path, name: str
) -> None:
    """The opt-in names one directory, not a way around the rule."""

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "github-token").write_text("{}", encoding="utf-8")
    if name == "escapes-the-directory":
        path = str(allowed / ".." / "elsewhere" / "github-token")
    elif name == "reaches-a-nested-file":
        nested = allowed / "nested"
        nested.mkdir()
        (nested / "github-token").write_text("{}", encoding="utf-8")
        path = str(nested / "github-token")
    else:
        link = allowed / "github-token"
        link.symlink_to(elsewhere / "github-token")
        path = str(link)
    server = grpc.server(ThreadPoolExecutor(max_workers=1))
    add_container_runtime_service(server, StepRegistry({"github.create-issue.v1": _ok}), allowed)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        stub = container_pb2_grpc.ContainerServiceStub(channel)
        events = list(stub.Execute(_credential_request(path)))
    finally:
        server.stop(None)

    assert len(events) == 1
    assert events[0].failure.code == FailureCode.INVALID_INPUT.value


def test_an_opted_in_runtime_still_accepts_the_approved_secret_mount(tmp_path: Path) -> None:
    calls: dict[str, Any] = {}

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        calls["invocation"] = invocation
        return ContainerResult(OUTPUT_SCHEMA_DIGEST, output={"issue": 42})

    server = grpc.server(ThreadPoolExecutor(max_workers=1))
    add_container_runtime_service(
        server, StepRegistry({"github.create-issue.v1": handler}), tmp_path
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        stub = container_pb2_grpc.ContainerServiceStub(channel)
        events = list(stub.Execute(_credential_request("/var/run/syntara/secrets/github-token")))
    finally:
        server.stop(None)

    assert [event.WhichOneof("event") for event in events] == ["result"]
    assert calls["invocation"].credential_file_paths == {
        "github-token": "/var/run/syntara/secrets/github-token"
    }


def test_a_client_sends_a_local_credential_path_only_when_it_opts_in(tmp_path: Path) -> None:
    """The client enforces the same rule, so a bad path never reaches the wire."""

    token = tmp_path / "github-token"
    token.write_text("{}", encoding="utf-8")
    invocation = ContainerInvocation(
        invocation_id="invocation-1",
        workflow_run_id="workflow-run-1",
        step_revision_id="github.create-issue.v1",
        inputs={},
        credential_file_paths={"github-token": str(token)},
        timeout_seconds=30,
    )

    with pytest.raises(ValueError, match="approved secret mount paths"):
        _request_from_invocation(invocation)

    request = _request_from_invocation(invocation, tmp_path)

    assert json.loads(request.credential_file_paths_json) == {"github-token": str(token)}


def _ok(_invocation: ContainerInvocation) -> ContainerResult:
    return ContainerResult(OUTPUT_SCHEMA_DIGEST, output={"issue": 42})


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
        error_result=ERROR_RESULT,
        error_schema_digest=ERROR_SCHEMA_DIGEST,
    )
    assert failure.output is None
    assert failure.object_reference is None

    with pytest.raises(ValueError, match="SDK base schema"):
        ContainerResult(
            OUTPUT_SCHEMA_DIGEST,
            status_code=1,
            error_result={**ERROR_RESULT, "code": "not-a-code"},
            error_schema_digest=ERROR_SCHEMA_DIGEST,
        )

    with pytest.raises(ValueError, match="successful result must not carry error metadata"):
        ContainerResult(OUTPUT_SCHEMA_DIGEST, output={"issue": 1}, error_result=ERROR_RESULT)

    with pytest.raises(ValueError, match="uint32"):
        ContainerResult(
            OUTPUT_SCHEMA_DIGEST,
            status_code=4_294_967_296,
        )

    with pytest.raises(ValueError, match="failed result requires an error"):
        ContainerResult(OUTPUT_SCHEMA_DIGEST, status_code=7)

    with pytest.raises(ValueError, match="failed result requires an error"):
        ContainerResult(OUTPUT_SCHEMA_DIGEST, status_code=7, error_result=ERROR_RESULT)

    invalid_error_result = {
        key: value for key, value in ERROR_RESULT.items() if key != "category"
    }
    with pytest.raises(ValueError, match="SDK base schema"):
        ContainerResult(
            OUTPUT_SCHEMA_DIGEST,
            status_code=7,
            error_result=invalid_error_result,
            error_schema_digest=ERROR_SCHEMA_DIGEST,
        )

    with pytest.raises(ValueError, match="SDK base schema"):
        ContainerResult(
            OUTPUT_SCHEMA_DIGEST,
            status_code=7,
            error_result={**ERROR_RESULT, "category": "unknown"},
            error_schema_digest=ERROR_SCHEMA_DIGEST,
        )


def test_deliberate_step_failure_round_trips_as_a_result_event() -> None:
    from syntara_plugin.runtime.container import _result_event, _result_from_event

    failure = ContainerResult(
        OUTPUT_SCHEMA_DIGEST,
        output={"rejected": True},
        status_code=1,
        status_message="The upstream service rejected the request.",
        error_result=ERROR_RESULT,
        error_schema_digest=ERROR_SCHEMA_DIGEST,
    )
    event = _result_event("invocation-1", failure)
    decoded = _result_from_event(event)

    assert decoded.status_code == 1
    assert decoded.status_message == "The upstream service rejected the request."
    assert decoded.output == {"rejected": True}
    assert decoded.object_reference is None
    assert decoded.error_result == ERROR_RESULT
    assert decoded.error_schema_digest == ERROR_SCHEMA_DIGEST


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
            error_result_json=b'{}',
        ),
    )
    with pytest.raises(ContainerRemoteError) as raised:
        _result_from_event(event)

    assert raised.value.source == "protocol"


def test_failure_event_message_surfaces_on_the_remote_error() -> None:
    """The client keeps the workload's safe failure message for display."""

    event = container_pb2.ExecutionEvent(
        invocation_id="invocation-1",
        failure=container_pb2.Failure(
            code="INVALID_INPUT",
            message="The tracker rejected the request.",
            retryable=False,
            outcome="not_started",
        ),
    )
    from syntara_plugin.runtime.container import _remote_failure

    error = _remote_failure(event)

    assert error.code is FailureCode.INVALID_INPUT
    assert error.source == "workload"
    assert error.retryable is False
    assert error.message == "The tracker rejected the request."


def test_oversized_failure_message_is_a_protocol_error() -> None:
    """The failure message is bounded like every other remote string."""

    from syntara_plugin.runtime.container import _remote_failure

    event = container_pb2.ExecutionEvent(
        invocation_id="invocation-1",
        failure=container_pb2.Failure(
            code="REMOTE_REJECTED",
            message="x" * 5_000,
            outcome="not_completed",
        ),
    )
    with pytest.raises(ContainerRemoteError) as raised:
        _remote_failure(event)

    assert raised.value.source == "protocol"


def test_empty_failure_message_is_a_protocol_error() -> None:
    """A terminal failure must carry a bounded, nonempty message."""

    from syntara_plugin.runtime.container import _remote_failure

    event = container_pb2.ExecutionEvent(
        invocation_id="invocation-1",
        failure=container_pb2.Failure(
            code="REMOTE_REJECTED",
            outcome="not_completed",
        ),
    )

    with pytest.raises(ContainerRemoteError) as raised:
        _remote_failure(event)

    assert raised.value.source == "protocol"
