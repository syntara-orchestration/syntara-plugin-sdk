"""Launch the in-container gRPC runtime from image-local step bindings."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import signal
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

import grpc

from .abi import CONTAINER_ABI, FailureCode
from .adapter import step_registry_from_bindings
from .bindings import StepBindings
from .container import (
    INCOMPLETE_STEP_MESSAGE,
    ContainerInvocationError,
    StepRegistry,
    failure_event,
    invocation_from_request,
    terminal_execution_event,
)
from .protocol import container_pb2, container_pb2_grpc

_RUNTIME_CONFIG = "SYNTARA_RUNTIME_CONFIG"
_GRPC_ADDRESS = "SYNTARA_RUNTIME_GRPC_ADDRESS"
_TLS_CERTIFICATE = "SYNTARA_RUNTIME_GRPC_TLS_CERT"
_TLS_KEY = "SYNTARA_RUNTIME_GRPC_TLS_KEY"
_TLS_CLIENT_CA = "SYNTARA_RUNTIME_GRPC_TLS_CLIENT_CA"
_DEFAULT_ADDRESS = "127.0.0.1:50051"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class RuntimeServerError(RuntimeError):
    """Raised when the runtime adapter cannot start its gRPC server."""


class AsyncContainerRuntimeService(container_pb2_grpc.ContainerServiceServicer):
    """Async gRPC implementation used by the Python runtime adapter."""

    def __init__(self, steps: StepRegistry) -> None:
        self._steps = steps
        self._active: dict[str, Event] = {}

    async def Execute(
        self,
        request: container_pb2.ExecuteRequest,
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[container_pb2.ExecutionEvent]:
        invocation_id = request.invocation_id
        cancellation: Event | None = None
        try:
            invocation, cancellation = invocation_from_request(request)
            handler = self._steps.resolve(invocation.step_revision_id)
            self._register_invocation(invocation.invocation_id, cancellation)
            context.add_done_callback(cancellation.set)
            result = await asyncio.to_thread(handler, invocation)
            yield terminal_execution_event(invocation, result)
        except ContainerInvocationError as error:
            yield failure_event(invocation_id, error)
        except Exception:
            yield failure_event(
                invocation_id,
                ContainerInvocationError(
                    FailureCode.OUTCOME_UNKNOWN,
                    INCOMPLETE_STEP_MESSAGE,
                    outcome="unknown",
                ),
            )
        finally:
            if cancellation is not None:
                self._unregister_invocation(invocation_id, cancellation)

    async def Cancel(
        self,
        request: container_pb2.CancelRequest,
        context: grpc.aio.ServicerContext,
    ) -> container_pb2.CancelResponse:
        cancellation = self._active.get(request.invocation_id)
        if cancellation is None:
            return container_pb2.CancelResponse(accepted=False)
        cancellation.set()
        return container_pb2.CancelResponse(accepted=True)

    async def Health(
        self,
        request: container_pb2.HealthRequest,
        context: grpc.aio.ServicerContext,
    ) -> container_pb2.HealthResponse:
        return container_pb2.HealthResponse(
            ready=True,
            runtime_abi=CONTAINER_ABI,
            step_revision_ids=self._steps.step_revision_ids,
        )

    def _register_invocation(self, invocation_id: str, cancellation: Event) -> None:
        if invocation_id in self._active:
            raise ContainerInvocationError(
                FailureCode.CONFLICT,
                "The plugin step is already running.",
                outcome="not_started",
            )
        self._active[invocation_id] = cancellation

    def _unregister_invocation(self, invocation_id: str, cancellation: Event) -> None:
        if self._active.get(invocation_id) is cancellation:
            del self._active[invocation_id]


@dataclass(frozen=True)
class RuntimeServerConfig:
    """Validated, non-secret inputs needed to launch one runtime server."""

    bindings_factory: str
    address: str = _DEFAULT_ADDRESS
    tls_certificate: Path | None = None
    tls_key: Path | None = None
    tls_client_ca: Path | None = None


def load_runtime_server_config(
    environ: Mapping[str, str] = os.environ,
    *,
    config_path: Path | None = None,
) -> RuntimeServerConfig:
    """Read the language-neutral runtime config and this adapter's settings."""

    document = _runtime_document(environ, config_path)
    bindings_factory = _bindings_factory(document)
    address, certificate, key, client_ca = _listener_settings(environ)
    return RuntimeServerConfig(bindings_factory, address, certificate, key, client_ca)


def load_runtime_bindings(specification: str) -> StepBindings:
    """Load one image-owned ``StepBindings`` factory."""

    module_name, separator, attribute_name = specification.rpartition(":")
    if not separator or not module_name or not attribute_name:
        message = "runtime bindings factory must use module:factory syntax"
        raise RuntimeServerError(message)
    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, attribute_name)
        bindings = factory()
    except Exception as error:
        message = "the configured runtime bindings factory could not be loaded"
        raise RuntimeServerError(message) from error
    if not isinstance(bindings, StepBindings):
        message = "the runtime bindings factory must return StepBindings"
        raise RuntimeServerError(message)
    if not bindings.step_identities:
        message = "the runtime bindings factory must register at least one step"
        raise RuntimeServerError(message)
    return bindings


def create_runtime_server(config: RuntimeServerConfig) -> grpc.aio.Server:
    """Create and bind, but do not start, the in-container gRPC server."""

    bindings = load_runtime_bindings(config.bindings_factory)
    server = grpc.aio.server()
    container_pb2_grpc.add_ContainerServiceServicer_to_server(
        AsyncContainerRuntimeService(step_registry_from_bindings(bindings)), server
    )
    credentials = _server_credentials(config)
    port = (
        server.add_secure_port(config.address, credentials)
        if credentials is not None
        else server.add_insecure_port(config.address)
    )
    if port == 0:
        message = f"could not bind the runtime gRPC listener: {config.address}"
        raise RuntimeServerError(message)
    return server


async def serve_runtime(config: RuntimeServerConfig) -> None:
    """Run the gRPC server until SIGTERM or SIGINT is received."""

    server = create_runtime_server(config)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    await server.start()
    try:
        await stopping.wait()
    finally:
        await server.stop(grace=30)


def main() -> None:
    """Console entrypoint used by a final Python workload image."""

    asyncio.run(serve_runtime(load_runtime_server_config()))


def _runtime_document(
    environ: Mapping[str, str],
    config_path: Path | None,
) -> Mapping[str, Any]:
    path = config_path
    if path is None:
        raw_path = environ.get(_RUNTIME_CONFIG, "").strip()
        if not raw_path:
            message = f"{_RUNTIME_CONFIG} must name a runtime config JSON file"
            raise RuntimeServerError(message)
        path = Path(raw_path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        message = f"could not read a valid runtime config: {path}"
        raise RuntimeServerError(message) from error
    if not isinstance(document, Mapping):
        message = "runtime config must be a JSON object"
        raise RuntimeServerError(message)
    return document


def _bindings_factory(document: Mapping[str, Any]) -> str:
    if set(document) != {"abi", "adapter"}:
        message = "runtime config requires exactly abi and adapter"
        raise RuntimeServerError(message)
    if document["abi"] != CONTAINER_ABI:
        message = f"runtime config ABI must be {CONTAINER_ABI}"
        raise RuntimeServerError(message)
    adapter = document["adapter"]
    if not isinstance(adapter, Mapping) or set(adapter) != {"bindingsFactory"}:
        message = "the Python runtime adapter requires exactly bindingsFactory"
        raise RuntimeServerError(message)
    bindings_factory = adapter["bindingsFactory"]
    if not isinstance(bindings_factory, str) or not bindings_factory.strip():
        message = "adapter bindingsFactory must name a module:factory function"
        raise RuntimeServerError(message)
    return bindings_factory.strip()


def _listener_settings(
    environ: Mapping[str, str],
) -> tuple[str, Path | None, Path | None, Path | None]:
    address = environ.get(_GRPC_ADDRESS, _DEFAULT_ADDRESS).strip()
    host, port = _split_address(address)
    if not host or not port.isdecimal() or not 1 <= int(port) <= 65_535:
        message = f"{_GRPC_ADDRESS} must be a host:port TCP address"
        raise RuntimeServerError(message)

    certificate = _optional_path(environ.get(_TLS_CERTIFICATE))
    key = _optional_path(environ.get(_TLS_KEY))
    client_ca = _optional_path(environ.get(_TLS_CLIENT_CA))
    supplied_tls = (certificate, key, client_ca)
    if any(value is not None for value in supplied_tls) and not all(
        value is not None for value in supplied_tls
    ):
        message = "runtime gRPC TLS requires certificate, key, and client CA paths"
        raise RuntimeServerError(message)
    normalized_host = host.removeprefix("[").removesuffix("]").casefold()
    if normalized_host not in _LOOPBACK_HOSTS and certificate is None:
        message = "non-loopback runtime gRPC listeners require mutual TLS"
        raise RuntimeServerError(message)
    return address, certificate, key, client_ca


def _split_address(address: str) -> tuple[str, str]:
    if "://" in address:
        return "", ""
    if address.startswith("[") and address.count("]") == 1:
        host, port = address.rsplit(":", 1)
        return host.removeprefix("[").removesuffix("]"), port
    if address.count(":") == 1:
        return address.split(":", 1)
    return "", ""


def _optional_path(value: str | None) -> Path | None:
    if value is None or not value.strip():
        return None
    return Path(value).expanduser()


def _server_credentials(config: RuntimeServerConfig) -> grpc.ServerCredentials | None:
    if config.tls_certificate is None:
        return None
    assert config.tls_key is not None
    assert config.tls_client_ca is not None
    try:
        certificate = config.tls_certificate.read_bytes()
        key = config.tls_key.read_bytes()
        client_ca = config.tls_client_ca.read_bytes()
    except OSError as error:
        message = "could not read the runtime gRPC TLS material"
        raise RuntimeServerError(message) from error
    return grpc.ssl_server_credentials(
        [(key, certificate)],
        root_certificates=client_ca,
        require_client_auth=True,
    )


if __name__ == "__main__":
    main()
