"""Tests for the in-container runtime server bootstrap."""

from __future__ import annotations

import socket
from pathlib import Path

import grpc
import pytest
from pydantic import BaseModel
from syntara_plugin.runtime import (
    ContainerClient,
    ContainerInvocation,
    ExecutionContext,
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


def create_bindings() -> StepBindings:
    step = EchoStep()
    bindings = StepBindings()
    bindings.bind(StepDescriptor.for_step("smoke.echo.v1", step), step)
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


def test_runtime_server_serves_health_and_executes_a_bound_step() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = create_runtime_server(
        RuntimeServerConfig(
            bindings_factory=f"{__name__}:create_bindings",
            address=f"127.0.0.1:{port}",
        )
    )
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        client = ContainerClient(channel)

        assert client.health() == ("smoke.echo.v1",)
        assert client.execute(_invocation()).output == {"message": "hello"}
    finally:
        channel.close()
        server.stop(grace=0).wait()


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

    assert "uv sync" in containerfile
    assert "--locked" in containerfile
    assert "--no-dev" in containerfile
    assert "--no-editable" in containerfile
    assert "--package syntara-plugin-runtime" in containerfile
    assert (
        "command -v uv >/dev/null 2>&1 || "
        "pip install --no-cache-dir --only-binary :all: uv==0.12.3"
    ) in containerfile
    assert "ARG BUILDER_IMAGE=" in containerfile
    assert "ARG RUNTIME_BASE_IMAGE=" in containerfile
    assert "FROM ${BUILDER_IMAGE} AS builder" in containerfile
    assert "FROM ${RUNTIME_BASE_IMAGE}" in containerfile
    assert "--offline --no-index --find-links" in containerfile
    assert "/opt/syntara/.venv/bin/pip --version" in containerfile
    assert "USER 1001" in containerfile
    assert (
        'ENTRYPOINT ["/opt/syntara/.venv/bin/python", "-m", '
        '"syntara_plugin.runtime.server"]' in containerfile
    )
    assert ignored.splitlines()[0] == "*"
    assert "!contracts/**" in ignored
    assert "!sdks/python/packages/runtime/**" in ignored
    assert "!sdks/python/packages/sdk/**" in ignored
