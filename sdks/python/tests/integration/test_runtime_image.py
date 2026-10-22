"""Integration test for the UBI-based Python runtime image."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import grpc
import pytest
from syntara_plugin.runtime import ContainerClient, ContainerInvocation

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SYNTARA_RUNTIME_IMAGE_TESTS") != "1",
        reason="set SYNTARA_RUNTIME_IMAGE_TESTS=1 to run the Podman integration test",
    ),
]

_EXAMPLE = Path(__file__).resolve().parents[4] / "examples" / "existing-step"
_ACTION_IDENTITY = "example.existing-step.create-ticket"
_CONTROL_IDENTITY = "example.existing-step.normalize-records"
_TRIGGER_IDENTITY = "example.existing-step.record-opened"


def test_ubi_python_image_serves_existing_step_plugin_over_grpc(tmp_path: Path) -> None:
    context = tmp_path / "image-context"
    wheels = context / "wheels"
    wheels.mkdir(parents=True)

    _build_workspace_wheel("syntara-plugin-contracts", wheels)
    _build_example_wheel(wheels)
    shutil.copyfile(_EXAMPLE / "runtime.json", context / "runtime.json")
    shutil.copyfile(_EXAMPLE / "Containerfile", context / "Containerfile")

    image_tag = f"syntara-plugin-runtime-it-{uuid.uuid4().hex}"
    container_name = f"syntara-runtime-it-{uuid.uuid4().hex}"
    _build_image(context, image_tag)

    host_port = _free_port()
    certificate, key, client_ca, client_certificate, client_key = _generate_tls_material(tmp_path)
    _make_tls_material_readable(certificate, key, client_ca)

    subprocess.run(
        [
            "podman",
            "run",
            "--rm",
            "--detach",
            "--name",
            container_name,
            "--publish",
            f"127.0.0.1:{host_port}:50051",
            "--volume",
            f"{certificate}:/run/secrets/server.crt:ro,Z",
            "--volume",
            f"{key}:/run/secrets/server.key:ro,Z",
            "--volume",
            f"{client_ca}:/run/secrets/client-ca.crt:ro,Z",
            image_tag,
        ],
        check=True,
    )

    credentials = grpc.ssl_channel_credentials(
        root_certificates=client_ca.read_bytes(),
        private_key=client_key.read_bytes(),
        certificate_chain=client_certificate.read_bytes(),
    )
    channel = grpc.secure_channel(f"127.0.0.1:{host_port}", credentials)
    try:
        client = ContainerClient(channel)
        health = _wait_for_health(client)
        action = client.execute(
            _invocation(
                _ACTION_IDENTITY,
                {"title": "hello from UBI"},
                invocation_id="existing-action",
            ),
            timeout_seconds=10,
        )
        control = client.execute(
            _invocation(
                _CONTROL_IDENTITY,
                {"records": ["Alpha", "Beta"]},
                invocation_id="existing-control",
            ),
            timeout_seconds=10,
        )
        trigger = client.execute(
            _invocation(
                _TRIGGER_IDENTITY,
                {"repository": "syntara-plugin-sdk"},
                invocation_id="existing-trigger",
            ),
            timeout_seconds=10,
        )
        invalid = client.execute(
            _invocation(_ACTION_IDENTITY, {}, invocation_id="existing-invalid"),
            timeout_seconds=10,
        )

        assert health == (_ACTION_IDENTITY, _CONTROL_IDENTITY, _TRIGGER_IDENTITY)
        assert action.output == {"ticket_id": "existing-action:hello from UBI"}
        assert control.output == {"count": 2, "normalized": ["alpha", "beta"]}
        assert trigger.output == {
            "event_id": "existing-run:syntara-plugin-sdk",
            "repository": "syntara-plugin-sdk",
        }
        assert invalid.status_code == 1
        assert invalid.error_code == "INVALID_INPUT"
        assert invalid.output is None
        assert invalid.output_schema_digest == action.output_schema_digest
    finally:
        channel.close()
        subprocess.run(
            ["podman", "rm", "--force", container_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["podman", "rmi", "--force", image_tag],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _build_workspace_wheel(package: str, output: Path) -> None:
    subprocess.run(
        [
            "uv",
            "build",
            "--package",
            package,
            "--wheel",
            "--out-dir",
            str(output),
            "--no-create-gitignore",
        ],
        check=True,
    )


def _build_example_wheel(output: Path) -> None:
    subprocess.run(
        [
            "uv",
            "build",
            "--directory",
            str(_EXAMPLE),
            "--wheel",
            "--out-dir",
            str(output),
            "--no-create-gitignore",
        ],
        check=True,
    )


def _build_image(context: Path, image_tag: str) -> None:
    subprocess.run(
        [
            "podman",
            "build",
            "--format",
            "oci",
            "--file",
            str(context / "Containerfile"),
            "--tag",
            image_tag,
            str(context),
        ],
        check=True,
    )


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _invocation(
    step_revision_id: str,
    inputs: dict[str, object],
    *,
    invocation_id: str,
) -> ContainerInvocation:
    return ContainerInvocation(
        invocation_id=invocation_id,
        workflow_run_id="existing-run",
        step_revision_id=step_revision_id,
        inputs=inputs,
        credential_file_paths={},
        timeout_seconds=30,
        runtime_context={},
    )


def _wait_for_health(client: ContainerClient) -> tuple[str, ...]:
    deadline = time.monotonic() + 30
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return client.health(timeout_seconds=2)
        except Exception as error:
            last_error = error
            time.sleep(0.25)
    message = "runtime container did not become healthy"
    raise AssertionError(message) from last_error


def _generate_tls_material(
    directory: Path,
) -> tuple[Path, Path, Path, Path, Path]:
    if shutil.which("openssl") is None:
        pytest.skip("OpenSSL is required for the runtime-image integration test")

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
        "/CN=syntara-runtime-integration-ca",
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
            f"/CN=syntara-runtime-integration-{role}",
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
                "subjectAltName=DNS:localhost,IP:127.0.0.1\n",
                encoding="utf-8",
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


def _make_tls_material_readable(certificate: Path, key: Path, client_ca: Path) -> None:
    for path in (certificate, key, client_ca):
        path.chmod(0o644)


def _run_openssl(*arguments: str) -> None:
    subprocess.run(("openssl", *arguments), check=True, capture_output=True)
