"""Tests for the in-container runtime server bootstrap."""

from __future__ import annotations

import asyncio
import re
import socket
import subprocess
import threading
from pathlib import Path

import grpc
import pytest
from pydantic import BaseModel
from syntara_plugin.runtime import (
    ContainerClient,
    ContainerInvocation,
    ContainerRemoteError,
    ExecutionContext,
    FailureCode,
    StepBindings,
    StepDescriptor,
    TaskStep,
)
from syntara_plugin.runtime.server import (
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


class EchoStep(TaskStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
        return EchoOutput(message=inputs.message)


class BarrierStep(TaskStep[EchoInput, EchoOutput]):
    barrier = threading.Barrier(2, timeout=2)

    def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
        self.barrier.wait()
        return EchoOutput(message=inputs.message)


class CancellationStep(TaskStep[EchoInput, EchoOutput]):
    started = threading.Event()

    def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
        self.started.set()
        context.cancellation.wait(timeout=5)
        return EchoOutput(message=inputs.message)


def create_bindings() -> StepBindings:
    step = EchoStep()
    bindings = StepBindings()
    bindings.bind(StepDescriptor.for_step("smoke.echo.v1", step), step)
    return bindings


def create_barrier_bindings() -> StepBindings:
    step = BarrierStep()
    bindings = StepBindings()
    bindings.bind(StepDescriptor.for_step("smoke.barrier.v1", step), step)
    return bindings


def create_cancellation_bindings() -> StepBindings:
    CancellationStep.started = threading.Event()
    step = CancellationStep()
    bindings = StepBindings()
    bindings.bind(StepDescriptor.for_step("smoke.cancellation.v1", step), step)
    return bindings


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
            extensions.write_text(
                "subjectAltName=DNS:localhost,IP:127.0.0.1\n", encoding="utf-8"
            )
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
        server = await create_runtime_server(
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
        server = await create_runtime_server(
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
        server = await create_runtime_server(
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
            assert await asyncio.to_thread(
                client.cancel, "invocation-1", timeout_seconds=5
            )
            with pytest.raises(ContainerRemoteError) as raised:
                await execution
        finally:
            channel.close()
            await server.stop(grace=0)

        assert raised.value.code is FailureCode.CANCELLED
        assert raised.value.outcome == "not_completed"

    asyncio.run(scenario())


def test_runtime_server_serves_health_and_steps_over_mutual_tls(
    tmp_path: Path,
) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    certificate, key, client_ca, client_certificate, client_key = _generate_tls_material(
        tmp_path
    )
    async def scenario() -> None:
        server = await create_runtime_server(
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
            result = await asyncio.to_thread(
                client.execute, _invocation(), timeout_seconds=5
            )

            assert health == ("smoke.echo.v1",)
            assert result.output == {"message": "hello"}
        finally:
            channel.close()
            await server.stop(grace=0)

    asyncio.run(scenario())


def test_runtime_server_configuration_accepts_an_uppercase_loopback_host() -> None:
    config = load_runtime_server_config(
        {
            "SYNTARA_RUNTIME_BINDINGS_FACTORY": "example:create",
            "SYNTARA_RUNTIME_GRPC_ADDRESS": "LOCALHOST:50051",
        }
    )

    assert config.address == "LOCALHOST:50051"


@pytest.mark.parametrize("address", ["::1", "grpc://localhost:50051"])
def test_runtime_server_configuration_rejects_non_host_port_addresses(
    address: str,
) -> None:
    with pytest.raises(RuntimeServerError, match="host:port"):
        load_runtime_server_config(
            {
                "SYNTARA_RUNTIME_BINDINGS_FACTORY": "example:create",
                "SYNTARA_RUNTIME_GRPC_ADDRESS": address,
            }
        )


def test_runtime_server_configuration_requires_a_bindings_factory() -> None:
    with pytest.raises(RuntimeServerError, match="BINDINGS_FACTORY"):
        load_runtime_server_config({})


def test_runtime_server_configuration_rejects_an_invalid_address() -> None:
    with pytest.raises(RuntimeServerError, match="host:port"):
        load_runtime_server_config(
            {
                "SYNTARA_RUNTIME_BINDINGS_FACTORY": "example:create",
                "SYNTARA_RUNTIME_GRPC_ADDRESS": "localhost:not-a-port",
            }
        )


def test_non_loopback_runtime_listener_requires_mutual_tls() -> None:
    with pytest.raises(RuntimeServerError, match="mutual TLS"):
        load_runtime_server_config(
            {
                "SYNTARA_RUNTIME_BINDINGS_FACTORY": "example:create",
                "SYNTARA_RUNTIME_GRPC_ADDRESS": "0.0.0.0:50051",
            }
        )


def test_runtime_tls_material_must_be_complete() -> None:
    with pytest.raises(RuntimeServerError, match="certificate, key, and client CA"):
        load_runtime_server_config(
            {
                "SYNTARA_RUNTIME_BINDINGS_FACTORY": "example:create",
                "SYNTARA_RUNTIME_GRPC_TLS_CERT": "/tmp/tls.crt",
            }
        )


def test_runtime_bindings_factory_must_use_module_syntax() -> None:
    with pytest.raises(RuntimeServerError, match="module:factory"):
        load_runtime_bindings("example.create")


def test_runtime_bindings_factory_must_return_step_bindings() -> None:
    with pytest.raises(RuntimeServerError, match="return StepBindings"):
        load_runtime_bindings(f"{__name__}:not_bindings")


def test_runtime_bindings_factory_must_register_a_step() -> None:
    with pytest.raises(RuntimeServerError, match="at least one step"):
        load_runtime_bindings(f"{__name__}:empty_bindings")


def test_runtime_image_contract_stays_minimal_and_runs_the_server_entrypoint() -> None:
    repository_root = Path(__file__).resolve().parents[4]
    containerfile = (repository_root / "containers/runtime/Containerfile").read_text(
        encoding="utf-8"
    )
    ignored = (repository_root / ".containerignore").read_text(encoding="utf-8")
    makefile = (repository_root / "Makefile").read_text(encoding="utf-8")

    assert "uv sync" in containerfile
    assert "--locked" in containerfile
    assert "--no-dev" in containerfile
    assert "--no-editable" in containerfile
    assert containerfile.count("ARG PIP_FIND_LINKS") == 2
    assert containerfile.count("$NO_BUILD_PACKAGES") == 2
    assert re.search(r"--no-build(?:\s|$)", containerfile) is None
    for package in (
        "annotated-types",
        "attrs",
        "grpcio",
        "jsonschema",
        "jsonschema-specifications",
        "protobuf",
        "pydantic",
        "pydantic-core",
        "referencing",
        "rpds-py",
        "typing-extensions",
        "typing-inspection",
    ):
        assert f"--no-build-package {package}" in containerfile
    assert "--package syntara-plugin-runtime" in containerfile
    assert (
        "command -v uv >/dev/null 2>&1 || "
        "pip install --no-cache-dir --only-binary :all: uv==0.12.3"
    ) in containerfile
    assert "ARG BUILDER_IMAGE=" in containerfile
    assert "ARG RUNTIME_BASE_IMAGE=" in containerfile
    assert "FROM ${BUILDER_IMAGE} AS builder" in containerfile
    assert "FROM ${RUNTIME_BASE_IMAGE}" in containerfile
    assert "COPY sdks/python/packages ./sdks/python/packages" in containerfile
    assert "--offline --no-index --find-links" in containerfile
    assert "/opt/syntara/.venv/bin/pip --version" in containerfile
    assert "USER 1001" in containerfile
    assert "https://github.com/syntara-orchestration/syntara-plugin-sdk" in containerfile
    assert (
        'ENTRYPOINT ["/opt/syntara/.venv/bin/python", "-m", '
        '"syntara_plugin.runtime.server"]' in containerfile
    )
    assert ignored.splitlines()[0] == "*"
    assert "!contracts/**" in ignored
    assert "!sdks/python/packages/**" in ignored
    assert "@mkdir -p dist" in makefile
