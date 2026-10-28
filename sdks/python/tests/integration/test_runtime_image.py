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

_EXAMPLE = Path(__file__).resolve().parents[4] / "examples" / "existing-step"
_ACTION_IDENTITY = "example.existing-step.create-ticket"
_CONTROL_IDENTITY = "example.existing-step.normalize-records"
_TRIGGER_IDENTITY = "example.existing-step.record-opened"
_BASE_IMAGE = (
    "registry.access.redhat.com/ubi9/python-312@"
    "sha256:e6a10c3150624fbfd33dbd7721ef99ce7a1001b417d64427ad58515ab8044121"
)


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
        """from pydantic import BaseModel
from syntara_plugin.runtime import (
    ActionStep,
    ExecutionContext,
    InstalledStepManifest,
    StepBindings,
    StepDescriptor,
)


class EchoInput(BaseModel):
    message: str


class EchoOutput(BaseModel):
    message: str


class EchoStep(ActionStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
        return EchoOutput(message=inputs.message)


def create_bindings() -> StepBindings:
    step = EchoStep()
    installed_step = InstalledStepManifest.from_manifest(
        "integration.echo.v1",
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
                    "entryPoint": "syntara_sample_step.runtime:EchoStep",
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
""",
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
        "adapter": {"bindingsFactory": "syntara_sample_step.runtime:create_bindings"},
    }
    (context / "runtime.json").write_text(json.dumps(document, indent=2), encoding="utf-8")


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


def _write_managed_containerfile(context: Path) -> None:
    """A TLS-free containerfile: loopback listener, host network friendly."""

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
    PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1

USER 1001
EXPOSE 50051
ENTRYPOINT ["python3.12", "-m", "syntara_plugin.runtime.server"]
""",
        encoding="utf-8",
    )


def test_managed_image_mode_runs_and_cleans_up_the_workload_container(
    tmp_path: Path, capsys
) -> None:
    """`run --mode image` without --address manages the container itself."""

    from syntara_plugin.sdk.cli import main

    context = tmp_path / "image-context"
    wheels = context / "wheels"
    wheels.mkdir(parents=True)
    _build_workspace_wheel("syntara-plugin-contracts", wheels)
    _write_sample_step(context)
    _build_sample_wheel(context, wheels)
    _write_runtime_config(context)
    _write_managed_containerfile(context)
    image_tag = f"syntara-plugin-managed-it-{uuid.uuid4().hex}"
    _build_image(context, image_tag)

    workspace = tmp_path / "ws"
    (workspace / "steps/v1").mkdir(parents=True)
    (workspace / "docs").mkdir()
    (workspace / "plugin.yaml").write_text(
        f"""apiVersion: syntara.io/v1alpha1
kind: Plugin
metadata:
  namespace: integration
  name: echo
  version: 0.1.0
  displayName: Echo
  description: Managed image-mode fixture.
spec:
  documentation:
    path: docs/README.md
  workload:
    image: {image_tag}
  targets:
    - steps/v1/manifest.yaml
""",
        encoding="utf-8",
    )
    (workspace / "steps/v1/manifest.yaml").write_text(
        """apiVersion: syntara.io/v1alpha1
kind: Action
metadata:
  name: v1
  displayName: Echo
  description: Echoes one message.
spec:
  runtime:
    kind: custom-runner
    entryPoint: syntara_sample_step.runtime:EchoStep
  input:
    kind: inline
    value:
      type: object
      properties:
        message:
          type: string
      required: [message]
  output:
    kind: inline
    value:
      type: object
      properties:
        message:
          type: string
      required: [message]
  error:
    kind: inline
    value:
      type: object
""",
        encoding="utf-8",
    )
    (workspace / "docs/README.md").write_text("# Echo\n", encoding="utf-8")

    try:
        status = main(
            [
                "run",
                "--manifest",
                str(workspace / "plugin.yaml"),
                "--mode",
                "image",
                "--input",
                '{"message": "hello managed"}',
            ]
        )
    finally:
        subprocess.run(
            ["podman", "rmi", "--force", image_tag],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    assert status == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert any(line.startswith("Mode:") and line.strip().endswith("image") for line in lines)
    assert any(line.startswith("Image:") and image_tag in line for line in lines)
    assert '"message": "hello managed"' in captured.out
    leftovers = subprocess.run(
        ["podman", "ps", "-a", "--filter", "name=syntara-run-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert not leftovers.stdout.strip()
