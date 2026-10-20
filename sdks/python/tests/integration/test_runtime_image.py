"""Integration test for the UBI-based Python runtime image."""

from __future__ import annotations

import json
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

_BASE_IMAGE = (
    "registry.access.redhat.com/ubi9/python-312@"
    "sha256:e6a10c3150624fbfd33dbd7721ef99ce7a1001b417d64427ad58515ab8044121"
)


def test_ubi_python_image_serves_sample_step_over_grpc(tmp_path: Path) -> None:
    context = tmp_path / "image-context"
    wheels = context / "wheels"
    wheels.mkdir(parents=True)

    _build_workspace_wheel("syntara-plugin-contracts", wheels)
    _write_sample_step(context)
    _build_sample_wheel(context, wheels)
    _write_runtime_config(context)
    _write_containerfile(context)

    image_tag = f"syntara-plugin-runtime-it-{uuid.uuid4().hex}"
    container_name = f"syntara-runtime-it-{uuid.uuid4().hex}"
    _build_image(context, image_tag)

    host_port = _free_port()
    certificate, key, client_ca, client_certificate, client_key = _generate_tls_material(
        tmp_path
    )
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
        result = client.execute(_invocation(), timeout_seconds=10)

        assert health == ("integration.echo.v1",)
        assert result.output == {"message": "hello from UBI"}
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


def _write_sample_step(context: Path) -> None:
    package = context / "sample-step"
    source = package / "src" / "syntara_sample_step"
    source.mkdir(parents=True)
    (package / "pyproject.toml").write_text(
        """[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[project]
name = "syntara-sample-step"
version = "0.1.0"
dependencies = ["syntara-plugin-contracts>=0.1.0a1"]

[tool.hatch.build.targets.wheel]
packages = ["src/syntara_sample_step"]
""",
        encoding="utf-8",
    )
    (source / "__init__.py").write_text("", encoding="utf-8")
    (source / "runtime.py").write_text(
        '''from pydantic import BaseModel
from syntara_plugin.runtime import ExecutionContext, StepBindings, StepDescriptor, TaskStep


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
    bindings.bind(StepDescriptor.for_step("integration.echo.v1", step), step)
    return bindings
''',
        encoding="utf-8",
    )


def _build_sample_wheel(context: Path, output: Path) -> None:
    subprocess.run(
        [
            "uv",
            "build",
            "--directory",
            str(context / "sample-step"),
            "--wheel",
            "--out-dir",
            str(output),
            "--no-create-gitignore",
        ],
        check=True,
    )


def _write_runtime_config(context: Path) -> None:
    document = {
        "abi": "syntara.container/v1alpha1",
        "language": "python",
        "adapter": {
            "bindingsFactory": "syntara_sample_step.runtime:create_bindings"
        },
    }
    (context / "runtime.json").write_text(
        json.dumps(document, indent=2), encoding="utf-8"
    )


def _write_containerfile(context: Path) -> None:
    (context / "Containerfile").write_text(
        f"""FROM {_BASE_IMAGE}

WORKDIR /app
COPY wheels /wheels
RUN python3.12 -m pip install --no-cache-dir --only-binary :all: /wheels/*.whl
USER 0
RUN mkdir -p /etc/syntara
COPY runtime.json /etc/syntara/runtime.json
RUN chmod 0644 /etc/syntara/runtime.json

ENV SYNTARA_RUNTIME_CONFIG=/etc/syntara/runtime.json \\
    SYNTARA_RUNTIME_GRPC_ADDRESS=0.0.0.0:50051 \\
    SYNTARA_RUNTIME_GRPC_TLS_CERT=/run/secrets/server.crt \\
    SYNTARA_RUNTIME_GRPC_TLS_KEY=/run/secrets/server.key \\
    SYNTARA_RUNTIME_GRPC_TLS_CLIENT_CA=/run/secrets/client-ca.crt \\
    PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1

USER 1001
EXPOSE 50051
ENTRYPOINT ["python3.12", "-m", "syntara_plugin.runtime.server"]
""",
        encoding="utf-8",
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


def _invocation() -> ContainerInvocation:
    return ContainerInvocation(
        invocation_id="integration-invocation-1",
        workflow_run_id="integration-workflow-run-1",
        step_revision_id="integration.echo.v1",
        inputs={"message": "hello from UBI"},
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


def _make_tls_material_readable(
    certificate: Path, key: Path, client_ca: Path
) -> None:
    for path in (certificate, key, client_ca):
        path.chmod(0o644)


def _run_openssl(*arguments: str) -> None:
    subprocess.run(("openssl", *arguments), check=True, capture_output=True)
