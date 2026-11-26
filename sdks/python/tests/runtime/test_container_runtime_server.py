"""Synchronous runtime server construction (AAP-103446)."""

from __future__ import annotations

import shutil
import socket
from pathlib import Path

import grpc
import pytest
from syntara_plugin.runtime import (
    ContainerClient,
    ContainerInvocation,
    ContainerRemoteError,
    ContainerResult,
    ContainerRuntimeServer,
    ContainerRuntimeService,
    StepRegistry,
    create_container_runtime_server,
)
from syntara_plugin.runtime import server as server_module
from syntara_plugin.runtime.server import RuntimeServerError
from syntara_plugin.sdk.container_engine import _generate_tls_material

OUTPUT_SCHEMA_DIGEST = "sha256:" + "a" * 64


def _echo(invocation: ContainerInvocation) -> ContainerResult:
    return ContainerResult(OUTPUT_SCHEMA_DIGEST, output={"echo": invocation.inputs.get("value")})


def _registry() -> StepRegistry:
    return StepRegistry({"step.v1": _echo})


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _tls_material(tmp_path: Path):
    material = _generate_tls_material(tmp_path)
    if material is None:
        pytest.skip("openssl is not installed")
    return material


def _invocation() -> ContainerInvocation:
    return ContainerInvocation(
        invocation_id="sync-1",
        step_revision_id="step.v1",
        inputs={"value": "hello"},
        timeout_seconds=10,
    )


def _record_service_shutdown(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Wrap the registration call so the test observes service.shutdown()."""

    shutdown_calls: list[bool] = []
    original = server_module.add_container_runtime_service

    def recording(server, steps, **kwargs):
        service = original(server, steps, **kwargs)
        real_shutdown = service.shutdown

        def shutdown():
            real_shutdown()
            shutdown_calls.append(True)

        service.shutdown = shutdown
        return service

    monkeypatch.setattr(server_module, "add_container_runtime_service", recording)
    return shutdown_calls


def test_tcp_listener_requires_mutual_tls() -> None:
    registry = _registry()
    address = f"127.0.0.1:{_free_port()}"
    with pytest.raises(RuntimeServerError, match="mutual TLS"):
        create_container_runtime_server(registry, address=address)


def test_tcp_listener_requires_complete_tls_material(tmp_path: Path) -> None:
    material = _tls_material(tmp_path)
    registry = _registry()
    address = f"127.0.0.1:{_free_port()}"
    with pytest.raises(RuntimeServerError, match="certificate, key, and client CA"):
        create_container_runtime_server(
            registry, address=address, tls_certificate=material.server_certificate
        )


def test_unix_listener_rejects_tls_material(tmp_path: Path) -> None:
    material = _tls_material(tmp_path)
    registry = _registry()
    address = f"unix:{tmp_path}/runtime.sock"
    with pytest.raises(RuntimeServerError, match="does not use TLS"):
        create_container_runtime_server(
            registry,
            address=address,
            tls_certificate=material.server_certificate,
            tls_key=material.server_key,
            tls_client_ca=material.client_ca,
        )


@pytest.mark.parametrize("address", ["unix:relative/runtime.sock", "unix:"])
def test_unix_listener_requires_an_absolute_socket_path(address: str) -> None:
    registry = _registry()
    with pytest.raises(RuntimeServerError, match="absolute socket path"):
        create_container_runtime_server(registry, address=address)


@pytest.mark.parametrize(
    "address",
    [
        "grpc://localhost:50051",
        "localhost:not-a-port",
        "localhost:70000",
        "::1",
        "",
        "unix-abstract:123",
        "vsock:42",
        "dns:50051",
    ],
)
def test_listener_rejects_invalid_addresses(address: str) -> None:
    registry = _registry()
    with pytest.raises(RuntimeServerError, match="host:port TCP address or a unix"):
        create_container_runtime_server(registry, address=address)


@pytest.mark.parametrize(
    "address",
    [
        " 127.0.0.1:50051",
        "127.0.0.1:50051\n",
        "unix:/tmp/air gap.sock",
        "unix:/tmp/nul\x00byte.sock",
    ],
)
def test_listener_rejects_invisible_characters(address: str) -> None:
    # Whitespace or control characters the caller cannot see must be an
    # invalid address, not a misleading bind failure or a truncated path.
    registry = _registry()
    with pytest.raises(RuntimeServerError, match="host:port TCP address or a unix"):
        create_container_runtime_server(registry, address=address)


def test_max_workers_must_be_a_positive_integer() -> None:
    registry = _registry()
    with pytest.raises(TypeError):
        create_container_runtime_server(registry, address="unix:/tmp/one.sock", max_workers=True)
    with pytest.raises(TypeError):
        create_container_runtime_server(registry, address="unix:/tmp/two.sock", max_workers="2")
    with pytest.raises(ValueError):
        create_container_runtime_server(registry, address="unix:/tmp/three.sock", max_workers=0)


def test_tcp_listener_serves_over_mutual_tls(tmp_path: Path) -> None:
    material = _tls_material(tmp_path)
    registry = _registry()
    runtime = create_container_runtime_server(
        registry,
        address=f"127.0.0.1:{_free_port()}",
        tls_certificate=material.server_certificate,
        tls_key=material.server_key,
        tls_client_ca=material.client_ca,
    )
    assert isinstance(runtime, ContainerRuntimeServer)
    assert isinstance(runtime.service, ContainerRuntimeService)
    runtime.server.start()
    credentials = grpc.ssl_channel_credentials(
        root_certificates=material.client_ca.read_bytes(),
        private_key=material.client_key.read_bytes(),
        certificate_chain=material.client_certificate.read_bytes(),
    )
    channel = grpc.secure_channel(runtime.address, credentials)
    # A TLS client without a client certificate: mutual TLS is enforced by
    # the handshake, so this must fail even though the channel itself is TLS.
    certless = grpc.secure_channel(
        runtime.address,
        grpc.ssl_channel_credentials(root_certificates=material.client_ca.read_bytes()),
    )
    plain = grpc.insecure_channel(runtime.address)
    plain_client = ContainerClient(plain)
    certless_client = ContainerClient(certless)
    try:
        client = ContainerClient(channel)
        assert client.health(timeout_seconds=5) == ("step.v1",)
        result = client.execute(_invocation(), timeout_seconds=15)
        assert result.output == {"echo": "hello"}
        with pytest.raises(ContainerRemoteError):
            plain_client.health(timeout_seconds=5)
        with pytest.raises(ContainerRemoteError):
            certless_client.health(timeout_seconds=5)
    finally:
        channel.close()
        certless.close()
        plain.close()
        runtime.server.stop(None)
        runtime.service.shutdown()


def test_ephemeral_tcp_port_returns_the_bound_address(tmp_path: Path) -> None:
    material = _tls_material(tmp_path)
    registry = _registry()
    runtime = create_container_runtime_server(
        registry,
        address="127.0.0.1:0",
        tls_certificate=material.server_certificate,
        tls_key=material.server_key,
        tls_client_ca=material.client_ca,
    )
    assert runtime.address != "127.0.0.1:0"
    runtime.server.start()
    credentials = grpc.ssl_channel_credentials(
        root_certificates=material.client_ca.read_bytes(),
        private_key=material.client_key.read_bytes(),
        certificate_chain=material.client_certificate.read_bytes(),
    )
    channel = grpc.secure_channel(runtime.address, credentials)
    try:
        assert ContainerClient(channel).health(timeout_seconds=5) == ("step.v1",)
    finally:
        channel.close()
        runtime.server.stop(None)
        runtime.service.shutdown()


def test_unix_listener_serves_a_step(tmp_path: Path) -> None:
    registry = _registry()
    runtime = create_container_runtime_server(registry, address=f"unix:{tmp_path}/runtime.sock")
    runtime.server.start()
    channel = grpc.insecure_channel(runtime.address)
    try:
        client = ContainerClient(channel)
        assert client.health(timeout_seconds=5) == ("step.v1",)
        result = client.execute(_invocation(), timeout_seconds=15)
        assert result.output == {"echo": "hello"}
    finally:
        channel.close()
        runtime.server.stop(None)
        runtime.service.shutdown()


def test_unix_socket_path_must_not_already_exist(tmp_path: Path) -> None:
    # gRPC would unlink and replace an existing socket file, silently taking
    # the address over from a server that still listens on it.
    occupied = tmp_path / "runtime.sock"
    occupied.write_bytes(b"")
    registry = _registry()
    with pytest.raises(RuntimeServerError, match="unix socket path already exists"):
        create_container_runtime_server(registry, address=f"unix:{occupied}")
    runtime = create_container_runtime_server(registry, address=f"unix:{tmp_path}/other.sock")
    runtime.server.start()
    try:
        channel = grpc.insecure_channel(runtime.address)
        try:
            assert ContainerClient(channel).health(timeout_seconds=5) == ("step.v1",)
        finally:
            channel.close()
    finally:
        runtime.server.stop(None)
        runtime.service.shutdown()


def test_unix_bind_failure_shuts_the_constructed_service_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutdown_calls = _record_service_shutdown(monkeypatch)
    registry = _registry()
    # A socket path whose parent directory does not exist fails the bind
    # after the service was constructed, exercising the cleanup path.
    with pytest.raises(RuntimeServerError, match="could not bind the runtime listener"):
        create_container_runtime_server(
            registry, address="unix:/nonexistent-aap-103446-parent/runtime.sock"
        )
    assert shutdown_calls, "the failed construction must shut the service down"


def test_tcp_bind_failure_shuts_the_constructed_service_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    material = _tls_material(tmp_path)
    shutdown_calls = _record_service_shutdown(monkeypatch)
    registry = _registry()
    port = _free_port()
    with socket.socket() as blocker:
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        with pytest.raises(RuntimeServerError, match="could not bind the runtime listener"):
            create_container_runtime_server(
                registry,
                address=f"127.0.0.1:{port}",
                tls_certificate=material.server_certificate,
                tls_key=material.server_key,
                tls_client_ca=material.client_ca,
            )
    assert shutdown_calls, "the failed construction must shut the service down"


def test_unreadable_tls_material_fails_before_construction(tmp_path: Path) -> None:
    # The error must be the bounded configuration message, never a mislabeled
    # bind failure (RuntimeServerError subclasses RuntimeError, so the bind
    # guard must not swallow it). A missing path also works when tests run
    # as root, where a chmod would not.
    material = _tls_material(tmp_path)
    registry = _registry()
    address = f"127.0.0.1:{_free_port()}"
    missing_key = tmp_path / "missing.key"
    with pytest.raises(RuntimeServerError, match="could not read the runtime gRPC TLS material"):
        create_container_runtime_server(
            registry,
            address=address,
            tls_certificate=material.server_certificate,
            tls_key=missing_key,
            tls_client_ca=material.client_ca,
        )


def test_invalid_tls_material_is_rejected_before_construction(tmp_path: Path) -> None:
    # Unreadable or mismatched material must fail as a configuration error,
    # never as a misleading bind failure.
    material = _tls_material(tmp_path)
    registry = _registry()
    garbage = tmp_path / "garbage.crt"
    garbage.write_bytes(b"not a certificate")
    mismatched_key = tmp_path / "mismatched.key"
    shutil.copyfile(material.client_key, mismatched_key)
    empty_ca = tmp_path / "empty-ca.crt"
    empty_ca.write_bytes(b"")
    cases = (
        (garbage, material.server_key, material.client_ca),
        (material.server_certificate, mismatched_key, material.client_ca),
        (material.server_certificate, material.server_key, empty_ca),
    )
    for bad_certificate, bad_key, bad_client_ca in cases:
        address = f"127.0.0.1:{_free_port()}"
        with pytest.raises(RuntimeServerError, match="TLS material is invalid"):
            create_container_runtime_server(
                registry,
                address=address,
                tls_certificate=bad_certificate,
                tls_key=bad_key,
                tls_client_ca=bad_client_ca,
            )


def test_a_second_tcp_server_cannot_silently_share_the_port(tmp_path: Path) -> None:
    # grpc enables SO_REUSEPORT by default, which would let a second server
    # share the address while both claim to serve; the helper disables it so
    # the second construction fails loudly and the first keeps serving.
    material = _tls_material(tmp_path)
    registry = _registry()
    first = create_container_runtime_server(
        registry,
        address=f"127.0.0.1:{_free_port()}",
        tls_certificate=material.server_certificate,
        tls_key=material.server_key,
        tls_client_ca=material.client_ca,
    )
    try:
        with pytest.raises(RuntimeServerError, match="could not bind the runtime listener"):
            create_container_runtime_server(
                registry,
                address=first.address,
                tls_certificate=material.server_certificate,
                tls_key=material.server_key,
                tls_client_ca=material.client_ca,
            )
        first.server.start()
        credentials = grpc.ssl_channel_credentials(
            root_certificates=material.client_ca.read_bytes(),
            private_key=material.client_key.read_bytes(),
            certificate_chain=material.client_certificate.read_bytes(),
        )
        channel = grpc.secure_channel(first.address, credentials)
        try:
            assert ContainerClient(channel).health(timeout_seconds=5) == ("step.v1",)
        finally:
            channel.close()
    finally:
        first.server.stop(None)
        first.service.shutdown()


def test_parameter_types_are_guarded() -> None:
    registry = _registry()
    address = f"127.0.0.1:{_free_port()}"
    key = Path("/tmp/key")
    client_ca = Path("/tmp/ca")
    with pytest.raises(TypeError):
        create_container_runtime_server(registry, address=123)
    with pytest.raises(TypeError):
        create_container_runtime_server(
            registry,
            address=address,
            tls_certificate="not-a-path",
            tls_key=key,
            tls_client_ca=client_ca,
        )
