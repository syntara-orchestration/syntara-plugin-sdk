"""Tests for the in-container runtime server bootstrap."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import threading
from pathlib import Path

import grpc
import pytest
from pydantic import BaseModel
from syntara_plugin.runtime import (
    ActionStep,
    BaseStep,
    ContainerClient,
    ContainerInvocation,
    ContainerRemoteError,
    ExecutionContext,
    FailureCode,
    InstalledStepManifest,
    StepBindings,
    StepDescriptor,
)
from syntara_plugin.runtime.adapter import step_registry_from_bindings
from syntara_plugin.runtime.protocol import container_pb2, container_pb2_grpc
from syntara_plugin.runtime.server import (
    AsyncContainerRuntimeService,
    RuntimeServerConfig,
    RuntimeServerError,
    create_runtime_server,
    load_runtime_bindings,
    load_runtime_server_config,
)


class EchoInput(BaseModel):
    message: str


class EchoOutput(BaseModel):
    message: str


class EchoStep(ActionStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
        return EchoOutput(message=inputs.message)


class BarrierStep(ActionStep[EchoInput, EchoOutput]):
    barrier = threading.Barrier(2, timeout=2)

    def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
        self.barrier.wait()
        return EchoOutput(message=inputs.message)


class CancellationStep(ActionStep[EchoInput, EchoOutput]):
    started = threading.Event()
    cancelled = threading.Event()

    def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
        self.started.set()
        if context.cancellation.wait(timeout=5):
            self.cancelled.set()
        return EchoOutput(message=inputs.message)


class CooperativeTimeoutStep(ActionStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
        context.cancellation.wait(timeout=5)
        return EchoOutput(message=inputs.message)


class UncancellableTimeoutStep(ActionStep[EchoInput, EchoOutput]):
    release = threading.Event()
    finished = threading.Event()

    def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
        del context
        self.release.wait(timeout=5)
        self.finished.set()
        return EchoOutput(message=inputs.message)


class FakeServicerContext:
    def add_done_callback(self, callback: object) -> None:
        del callback


def installed_action_bindings(
    step_identity: str, step: BaseStep[EchoInput, EchoOutput]
) -> StepBindings:
    step_class = type(step)
    installed_step = InstalledStepManifest.from_manifest(
        step_identity,
        {
            "apiVersion": "syntara.io/v1alpha1",
            "kind": "Action",
            "metadata": {
                "name": "echo",
                "displayName": "Echo",
                "description": "Echoes one message.",
            },
            "spec": {
                "runtime": {
                    "kind": "custom-runner",
                    "entryPoint": f"{step_class.__module__}:{step_class.__qualname__}",
                },
                "input": {"kind": "inline", "value": EchoInput.model_json_schema()},
                "output": {"kind": "inline", "value": EchoOutput.model_json_schema()},
            },
        },
        {},
    )
    bindings = StepBindings()
    bindings.bind(StepDescriptor.for_installed_step(step, installed_step), step)
    return bindings


def create_bindings() -> StepBindings:
    return installed_action_bindings("smoke.echo.v1", EchoStep())


def create_barrier_bindings() -> StepBindings:
    return installed_action_bindings("smoke.barrier.v1", BarrierStep())


def create_cancellation_bindings() -> StepBindings:
    CancellationStep.started = threading.Event()
    CancellationStep.cancelled = threading.Event()
    return installed_action_bindings("smoke.cancellation.v1", CancellationStep())


def create_cooperative_timeout_bindings() -> StepBindings:
    return installed_action_bindings("smoke.cooperative-timeout.v1", CooperativeTimeoutStep())


def create_uncancellable_timeout_bindings() -> StepBindings:
    return installed_action_bindings("smoke.uncancellable-timeout.v1", UncancellableTimeoutStep())


def write_runtime_config(
    tmp_path: Path,
    *,
    abi: str = "syntara.container/v1alpha1",
    adapter: object | None = None,
) -> Path:
    document = {
        "abi": abi,
        "adapter": adapter if adapter is not None else {"bindingsFactory": "example:create"},
    }
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def not_bindings() -> object:
    return object()


def empty_bindings() -> StepBindings:
    return StepBindings()


def _invocation() -> ContainerInvocation:
    return ContainerInvocation(
        invocation_id="invocation-1",
        workflow_run_id="workflow-run-1",
        step_revision_id="smoke.echo.v1",
        inputs={"message": "hello"},
        credential_file_paths={},
        timeout_seconds=30,
        runtime_context={},
    )


def _barrier_invocation(invocation_id: str) -> ContainerInvocation:
    return ContainerInvocation(
        invocation_id=invocation_id,
        workflow_run_id="workflow-run-1",
        step_revision_id="smoke.barrier.v1",
        inputs={"message": invocation_id},
        credential_file_paths={},
        timeout_seconds=30,
        runtime_context={},
    )


def _cancellation_invocation() -> ContainerInvocation:
    return ContainerInvocation(
        invocation_id="invocation-1",
        workflow_run_id="workflow-run-1",
        step_revision_id="smoke.cancellation.v1",
        inputs={"message": "cancelled"},
        credential_file_paths={},
        timeout_seconds=30,
        runtime_context={},
    )


def _timeout_invocation(step_revision_id: str) -> ContainerInvocation:
    return ContainerInvocation(
        invocation_id="invocation-1",
        workflow_run_id="workflow-run-1",
        step_revision_id=step_revision_id,
        inputs={"message": "timeout"},
        credential_file_paths={},
        timeout_seconds=1,
        runtime_context={},
    )


async def _service_events(
    service: AsyncContainerRuntimeService,
    invocation: ContainerInvocation,
    *,
    release: threading.Event | None = None,
) -> list[container_pb2.ExecutionEvent]:
    request = container_pb2.ExecuteRequest(
        invocation_id=invocation.invocation_id,
        workflow_run_id=invocation.workflow_run_id,
        step_revision_id=invocation.step_revision_id,
        runtime_abi="syntara.container/v1alpha1",
        inputs_json=json.dumps({"message": invocation.inputs["message"]}).encode(),
        timeout_seconds=invocation.timeout_seconds,
    )
    events = [event async for event in service.Execute(request, FakeServicerContext())]
    if release is not None:
        release.set()
    return events


def _run_openssl(*arguments: str) -> None:
    subprocess.run(("openssl", *arguments), check=True, capture_output=True)


def _generate_tls_material(
    directory: Path,
) -> tuple[Path, Path, Path, Path, Path]:
    ca_key = directory / "ca.key"
    ca_certificate = directory / "ca.crt"
    _run_openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(ca_key),
        "-out",
        str(ca_certificate),
        "-days",
        "1",
        "-subj",
        "/CN=runtime-test-ca",
    )

    certificates: dict[str, Path] = {}
    keys: dict[str, Path] = {}
    for role in ("server", "client"):
        key = directory / f"{role}.key"
        request = directory / f"{role}.csr"
        certificate = directory / f"{role}.crt"
        _run_openssl(
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(request),
            "-subj",
            f"/CN=runtime-test-{role}",
        )
        command = [
            "x509",
            "-req",
            "-in",
            str(request),
            "-CA",
            str(ca_certificate),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(certificate),
            "-days",
            "1",
            "-sha256",
        ]
        if role == "server":
            extensions = directory / "server.ext"
            extensions.write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n", encoding="utf-8")
            command.extend(("-extfile", str(extensions)))
        _run_openssl(*command)
        certificates[role] = certificate
        keys[role] = key

    return (
        certificates["server"],
        keys["server"],
        ca_certificate,
        certificates["client"],
        keys["client"],
    )


def test_runtime_server_serves_health_and_executes_a_bound_step() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    async def scenario() -> None:
        server = create_runtime_server(
            RuntimeServerConfig(
                bindings_factory=f"{__name__}:create_bindings",
                address=f"127.0.0.1:{port}",
            )
        )
        await server.start()
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        try:
            client = ContainerClient(channel)
            health = await asyncio.to_thread(client.health)
            result = await asyncio.to_thread(client.execute, _invocation())

            assert health == ("smoke.echo.v1",)
            assert result.output == {"message": "hello"}
        finally:
            channel.close()
            await server.stop(grace=0)

    asyncio.run(scenario())


def test_runtime_server_executes_steps_concurrently() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    async def scenario() -> None:
        server = create_runtime_server(
            RuntimeServerConfig(
                bindings_factory=f"{__name__}:create_barrier_bindings",
                address=f"127.0.0.1:{port}",
            )
        )
        await server.start()
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        try:
            client = ContainerClient(channel)
            results = await asyncio.gather(
                asyncio.to_thread(client.execute, _barrier_invocation("invocation-1")),
                asyncio.to_thread(client.execute, _barrier_invocation("invocation-2")),
            )

            assert [result.output for result in results] == [
                {"message": "invocation-1"},
                {"message": "invocation-2"},
            ]
        finally:
            channel.close()
            await server.stop(grace=0)

    asyncio.run(scenario())


def test_runtime_server_cancels_an_active_step() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    async def scenario() -> None:
        server = create_runtime_server(
            RuntimeServerConfig(
                bindings_factory=f"{__name__}:create_cancellation_bindings",
                address=f"127.0.0.1:{port}",
            )
        )
        await server.start()
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        try:
            client = ContainerClient(channel)
            execution = asyncio.create_task(
                asyncio.to_thread(client.execute, _cancellation_invocation())
            )
            assert await asyncio.to_thread(CancellationStep.started.wait, 5)
            assert await asyncio.to_thread(client.cancel, "invocation-1", timeout_seconds=5)
            with pytest.raises(ContainerRemoteError) as raised:
                await execution
        finally:
            channel.close()
            await server.stop(grace=0)

        assert raised.value.code is FailureCode.CANCELLED
        assert raised.value.outcome == "not_completed"

    asyncio.run(scenario())


def test_runtime_server_cancels_step_when_client_disconnects() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    async def scenario() -> None:
        server = create_runtime_server(
            RuntimeServerConfig(
                bindings_factory=f"{__name__}:create_cancellation_bindings",
                address=f"127.0.0.1:{port}",
            )
        )
        await server.start()
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        try:
            stub = container_pb2_grpc.ContainerServiceStub(channel)
            invocation = _cancellation_invocation()
            request = container_pb2.ExecuteRequest(
                invocation_id=invocation.invocation_id,
                workflow_run_id=invocation.workflow_run_id,
                step_revision_id=invocation.step_revision_id,
                runtime_abi="syntara.container/v1alpha1",
                inputs_json=json.dumps({"message": "disconnect"}).encode(),
                timeout_seconds=30,
            )
            call = stub.Execute(request)
            execution = asyncio.create_task(asyncio.to_thread(list, call))
            assert await asyncio.to_thread(CancellationStep.started.wait, 5)

            call.cancel()
            with pytest.raises(grpc.RpcError) as raised:
                await execution

            assert raised.value.code() is grpc.StatusCode.CANCELLED
            assert await asyncio.to_thread(CancellationStep.cancelled.wait, 5)
        finally:
            channel.close()
            await server.stop(grace=0)

    asyncio.run(scenario())


def test_runtime_server_times_out_a_cooperative_step() -> None:
    service = AsyncContainerRuntimeService(
        step_registry_from_bindings(create_cooperative_timeout_bindings())
    )

    events = asyncio.run(
        _service_events(service, _timeout_invocation("smoke.cooperative-timeout.v1"))
    )

    assert len(events) == 1
    assert events[0].WhichOneof("event") == "failure"
    assert events[0].failure.code == FailureCode.TIMEOUT
    assert events[0].failure.retryable is True
    assert events[0].failure.outcome == "not_completed"
    cancellation_response = asyncio.run(
        service.Cancel(
            container_pb2.CancelRequest(invocation_id="invocation-1"),
            FakeServicerContext(),
        )
    )
    assert cancellation_response.accepted is False


def test_runtime_server_times_out_an_uncancellable_step() -> None:
    service = AsyncContainerRuntimeService(
        step_registry_from_bindings(create_uncancellable_timeout_bindings())
    )

    events = asyncio.run(
        _service_events(
            service,
            _timeout_invocation("smoke.uncancellable-timeout.v1"),
            release=UncancellableTimeoutStep.release,
        )
    )

    assert len(events) == 1
    assert events[0].WhichOneof("event") == "failure"
    assert events[0].failure.code == FailureCode.TIMEOUT
    assert events[0].failure.outcome == "not_completed"

    assert UncancellableTimeoutStep.finished.wait(timeout=5)
    cancellation_response = asyncio.run(
        service.Cancel(
            container_pb2.CancelRequest(invocation_id="invocation-1"),
            FakeServicerContext(),
        )
    )
    assert cancellation_response.accepted is False


def test_runtime_server_serves_health_and_steps_over_mutual_tls(
    tmp_path: Path,
) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    certificate, key, client_ca, client_certificate, client_key = _generate_tls_material(tmp_path)

    async def scenario() -> None:
        server = create_runtime_server(
            RuntimeServerConfig(
                bindings_factory=f"{__name__}:create_bindings",
                address=f"localhost:{port}",
                tls_certificate=certificate,
                tls_key=key,
                tls_client_ca=client_ca,
            )
        )
        await server.start()
        credentials = grpc.ssl_channel_credentials(
            root_certificates=client_ca.read_bytes(),
            private_key=client_key.read_bytes(),
            certificate_chain=client_certificate.read_bytes(),
        )
        channel = grpc.secure_channel(f"localhost:{port}", credentials)
        try:
            client = ContainerClient(channel)
            health = await asyncio.to_thread(client.health, timeout_seconds=5)
            result = await asyncio.to_thread(client.execute, _invocation(), timeout_seconds=5)

            assert health == ("smoke.echo.v1",)
            assert result.output == {"message": "hello"}
        finally:
            channel.close()
            await server.stop(grace=0)

    asyncio.run(scenario())


def test_runtime_server_configuration_accepts_an_uppercase_loopback_host(
    tmp_path: Path,
) -> None:
    config_path = write_runtime_config(tmp_path)
    config = load_runtime_server_config(
        {
            "SYNTARA_RUNTIME_CONFIG": str(config_path),
            "SYNTARA_RUNTIME_GRPC_ADDRESS": "LOCALHOST:50051",
        }
    )

    assert config.address == "LOCALHOST:50051"


@pytest.mark.parametrize("address", ["::1", "grpc://localhost:50051", "localhost:²"])
def test_runtime_server_configuration_rejects_non_host_port_addresses(
    address: str,
    tmp_path: Path,
) -> None:
    config_path = write_runtime_config(tmp_path)
    with pytest.raises(RuntimeServerError, match="host:port"):
        load_runtime_server_config(
            {
                "SYNTARA_RUNTIME_CONFIG": str(config_path),
                "SYNTARA_RUNTIME_GRPC_ADDRESS": address,
            }
        )


def test_runtime_server_configuration_requires_a_config_path() -> None:
    with pytest.raises(RuntimeServerError, match="RUNTIME_CONFIG"):
        load_runtime_server_config({})


def test_runtime_server_configuration_rejects_an_invalid_address(
    tmp_path: Path,
) -> None:
    config_path = write_runtime_config(tmp_path)
    with pytest.raises(RuntimeServerError, match="host:port"):
        load_runtime_server_config(
            {
                "SYNTARA_RUNTIME_CONFIG": str(config_path),
                "SYNTARA_RUNTIME_GRPC_ADDRESS": "localhost:not-a-port",
            }
        )


def test_non_loopback_runtime_listener_requires_mutual_tls(tmp_path: Path) -> None:
    config_path = write_runtime_config(tmp_path)
    with pytest.raises(RuntimeServerError, match="mutual TLS"):
        load_runtime_server_config(
            {
                "SYNTARA_RUNTIME_CONFIG": str(config_path),
                "SYNTARA_RUNTIME_GRPC_ADDRESS": "0.0.0.0:50051",
            }
        )


def test_runtime_tls_material_must_be_complete(tmp_path: Path) -> None:
    config_path = write_runtime_config(tmp_path)
    with pytest.raises(RuntimeServerError, match="certificate, key, and client CA"):
        load_runtime_server_config(
            {
                "SYNTARA_RUNTIME_CONFIG": str(config_path),
                "SYNTARA_RUNTIME_GRPC_TLS_CERT": "/tmp/tls.crt",
            }
        )


def test_runtime_config_rejects_the_wrong_abi(tmp_path: Path) -> None:
    config_path = write_runtime_config(tmp_path, abi="syntara.container/v0")
    with pytest.raises(RuntimeServerError, match="ABI"):
        load_runtime_server_config(config_path=config_path)


def test_runtime_config_rejects_a_language_field(
    tmp_path: Path,
) -> None:
    config_path = write_runtime_config(tmp_path)
    document = json.loads(config_path.read_text(encoding="utf-8"))
    document["language"] = "python"
    config_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(RuntimeServerError, match="exactly abi and adapter"):
        load_runtime_server_config(config_path=config_path)


def test_runtime_config_rejects_an_invalid_python_adapter(tmp_path: Path) -> None:
    config_path = write_runtime_config(tmp_path, adapter={"factory": "example:create"})
    with pytest.raises(RuntimeServerError, match="bindingsFactory"):
        load_runtime_server_config(config_path=config_path)


def test_runtime_bindings_factory_must_use_module_syntax() -> None:
    with pytest.raises(RuntimeServerError, match="module:factory"):
        load_runtime_bindings("example.create")


def test_runtime_bindings_factory_must_return_step_bindings() -> None:
    with pytest.raises(RuntimeServerError, match="return StepBindings"):
        load_runtime_bindings(f"{__name__}:not_bindings")


def test_runtime_bindings_factory_must_register_a_step() -> None:
    with pytest.raises(RuntimeServerError, match="at least one step"):
        load_runtime_bindings(f"{__name__}:empty_bindings")
