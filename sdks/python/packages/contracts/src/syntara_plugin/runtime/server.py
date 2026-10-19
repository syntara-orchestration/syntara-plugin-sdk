"""Launch the in-container gRPC runtime from image-local step bindings."""

from __future__ import annotations

import importlib
import os
import signal
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

import grpc

from .adapter import step_registry_from_bindings
from .bindings import StepBindings
from .container import add_container_runtime_service

_BINDINGS_FACTORY = "SYNTARA_RUNTIME_BINDINGS_FACTORY"
_GRPC_ADDRESS = "SYNTARA_RUNTIME_GRPC_ADDRESS"
_TLS_CERTIFICATE = "SYNTARA_RUNTIME_GRPC_TLS_CERT"
_TLS_KEY = "SYNTARA_RUNTIME_GRPC_TLS_KEY"
_TLS_CLIENT_CA = "SYNTARA_RUNTIME_GRPC_TLS_CLIENT_CA"
_DEFAULT_ADDRESS = "127.0.0.1:50051"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class RuntimeServerError(RuntimeError):
    """Raised when the runtime image cannot start its gRPC server."""


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
) -> RuntimeServerConfig:
    """Read and validate the runtime image's environment contract."""

    bindings_factory = environ.get(_BINDINGS_FACTORY, "").strip()
    if not bindings_factory:
        message = f"{_BINDINGS_FACTORY} must name a module:factory function"
        raise RuntimeServerError(message)
    address = environ.get(_GRPC_ADDRESS, _DEFAULT_ADDRESS).strip()
    host, port = _split_address(address)
    if not host or not port.isdigit() or not 1 <= int(port) <= 65_535:
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
    if host.removeprefix("[").removesuffix("]") not in _LOOPBACK_HOSTS and certificate is None:
        message = "non-loopback runtime gRPC listeners require mutual TLS"
        raise RuntimeServerError(message)
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


def create_runtime_server(config: RuntimeServerConfig) -> grpc.Server:
    """Create and bind, but do not start, the in-container gRPC server."""

    bindings = load_runtime_bindings(config.bindings_factory)
    server = grpc.server(ThreadPoolExecutor(max_workers=4))
    add_container_runtime_service(server, step_registry_from_bindings(bindings))
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


def serve_runtime(config: RuntimeServerConfig) -> None:
    """Run the gRPC server until SIGTERM or SIGINT is received."""

    server = create_runtime_server(config)
    stopping = Event()

    def terminate(_signum: int, _frame: Any) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    server.start()
    try:
        while not stopping.wait(0.1):
            pass
    finally:
        server.stop(grace=30).wait(timeout=35)


def main() -> None:
    """Console entrypoint used by the SDK runtime base image."""

    serve_runtime(load_runtime_server_config())


def _split_address(address: str) -> tuple[str, str]:
    if address.count(":") == 1:
        return address.split(":", 1)
    if address.startswith("[") and address.count("]") == 1:
        host, port = address.rsplit(":", 1)
        return host.removeprefix("[").removesuffix("]"), port
    if address.count(":") == 2 and address.rsplit(":", 1)[0].count(":") == 1:
        return address.rsplit(":", 1)
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
