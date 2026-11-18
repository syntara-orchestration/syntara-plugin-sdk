"""Probe runtime metadata from the exact image selected by a RuntimeBinding."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from syntara_plugin.contracts import registration_manifest
from syntara_plugin.runtime import supports_runtime_probe
from syntara_plugin.runtime.container import ContainerClient, ContainerHealth, ContainerRemoteError

from .container_engine import load_oci_archive, managed_runtime
from .runner import RunExecutionError, TlsOptions, _channel
from .runtime_metadata import (
    RuntimeMetadataError,
    is_valid_runtime_version,
    runtime_metadata_from_annotations,
)
from .workload import WorkloadBuildError

_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROBE_STARTUP_SECONDS = 15.0


class RuntimeProbeError(WorkloadBuildError):
    """Raised when the selected image cannot provide truthful runtime metadata."""


def observe_runtime_image(
    image: str,
    declarations: Sequence[Mapping[str, str]],
    *,
    descriptor: Mapping[str, Any],
    workload_id: str,
    archive: Path | None = None,
    auth_file: Path | None = None,
    allow_insecure_loopback_http: bool = False,
    expected_metadata: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Read image annotations and compare them with supported Health probes."""

    with _image_available(
        image,
        archive=archive,
        auth_file=auth_file,
        allow_insecure_loopback_http=allow_insecure_loopback_http,
    ) as local_image:
        metadata = _read_runtime_annotations(local_image)
        if expected_metadata is not None and dict(expected_metadata) != metadata:
            raise RuntimeProbeError(
                "runtime annotations on the digest-pinned image differ from the build settings"
            )
        try:
            resolved_execution = _probe_execution_document(descriptor, workload_id)
            with managed_runtime(
                local_image,
                probe_only=True,
                resolved_execution=resolved_execution,
            ) as runtime:
                report = _read_health_report(
                    runtime.address,
                    runtime.tls,
                    liveness=runtime.liveness,
                )
        except RunExecutionError as error:
            detail = "; ".join(error.detail)
            message = error.message
            if detail:
                message = f"{message}: {detail}"
            raise RuntimeProbeError(message) from error

    components = _runtime_observations(declarations, report.runtime_components)
    matching = next(
        (item for item in components if item["name"].casefold() == metadata["language"]),
        None,
    )
    if matching is None:
        raise RuntimeProbeError(
            f"runtime language {metadata['language']!r} has no matching component declaration"
        )
    verification = "unverified"
    if supports_runtime_probe(metadata["language"]) and matching.get("verification") != "verified":
        raise RuntimeProbeError(
            f"supported runtime {metadata['language']!r} was not verified by its declared "
            "executable; check runtime.components and the image"
        )
    if matching.get("verification") == "verified" and supports_runtime_probe(metadata["language"]):
        if matching.get("version") != metadata["version"]:
            raise RuntimeProbeError(
                "runtime version annotation differs from the version reported by its executable"
            )
        verification = "verified"
    return metadata | {"verification": verification, "components": components}


def _read_runtime_annotations(image: str) -> dict[str, str]:
    """Read and validate runtime annotations from the exact local image ID."""

    try:
        completed = subprocess.run(
            ["podman", "image", "inspect", "--format", "{{json .Annotations}}", image],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeProbeError("could not inspect the digest-pinned workload image") from error
    if completed.returncode != 0:
        raise RuntimeProbeError("could not inspect the digest-pinned workload image")
    try:
        annotations = json.loads(completed.stdout.strip() or "null")
    except json.JSONDecodeError as error:
        raise RuntimeProbeError("workload image annotations are not valid JSON") from error
    if annotations is None:
        annotations = {}
    if not isinstance(annotations, Mapping):
        raise RuntimeProbeError("workload image annotations must be an object")
    try:
        metadata = runtime_metadata_from_annotations(
            annotations,
            source_name="workload image",
        )
    except RuntimeMetadataError as error:
        raise RuntimeProbeError(str(error)) from error
    if metadata is None:
        raise RuntimeProbeError(
            "workload image is missing Syntara runtime annotations; provide both "
            "io.syntara.runtime.language and io.syntara.runtime.version"
        )
    return metadata


def _runtime_observations(
    declarations: Sequence[Mapping[str, str]], reports: Sequence[Any]
) -> list[dict[str, str]]:
    """Match image Health results to declared probe targets."""

    reports_by_name = {item.name: item for item in reports}
    if len(reports_by_name) != len(reports):
        raise RuntimeProbeError("workload Health reported duplicate runtime component names")
    return [
        _runtime_component_observation(declaration, reports_by_name.get(declaration["name"]))
        for declaration in declarations
    ]


def _runtime_component_observation(
    declaration: Mapping[str, str], observed: Any | None
) -> dict[str, str]:
    """Validate one reported component and return the resolved binding record."""

    name = declaration["name"]
    executable = declaration["executable"]
    if not supports_runtime_probe(name):
        return {"name": name, "executable": executable, "verification": "unverified"}
    if observed is None:
        raise RuntimeProbeError(
            f"supported runtime component {name!r} was not reported by Health; check its "
            "runtime.components executable declaration"
        )
    if observed.executable != executable:
        raise RuntimeProbeError(
            f"runtime component {name!r} reports a different executable than its RuntimeBinding"
        )
    if observed.verification not in {"verified", "unverified"}:
        raise RuntimeProbeError(
            f"runtime component {name!r} reports an invalid verification status"
        )
    if observed.verification != "verified" or not observed.version:
        raise RuntimeProbeError(
            f"supported runtime component {name!r} was not verified by its executable probe"
        )
    if not is_valid_runtime_version(observed.version):
        raise RuntimeProbeError(f"runtime component {name!r} reports an invalid version")
    return {
        "name": name,
        "executable": executable,
        "version": observed.version,
        "verification": "verified",
    }


def _probe_execution_document(
    descriptor: Mapping[str, Any], workload_id: str
) -> dict[str, list[dict[str, Any]]]:
    """Prepare the normal resolved-execution input needed to start this image."""

    registered = registration_manifest(descriptor, workload_id)
    steps = registered["steps"]
    assert isinstance(steps, Mapping)
    return {
        "bindings": [
            {
                "stepRevisionId": revision,
                "stepIdentity": step["stepIdentity"],
                "runtimeKind": "custom-runner",
                "entryPoint": step["entryPoint"],
                "abi": step["abi"],
                "schemaDigests": step["schemaDigests"],
            }
            for revision, step in steps.items()
        ]
    }


def _read_health_report(
    address: str,
    tls: TlsOptions | None,
    *,
    liveness: Callable[[], None] | None = None,
) -> ContainerHealth:
    if tls is not None and not isinstance(tls, TlsOptions):
        raise RuntimeProbeError("workload runtime TLS settings are invalid")
    channel = _channel(address, tls)
    client = ContainerClient(channel)
    deadline = time.monotonic() + _PROBE_STARTUP_SECONDS
    last_error: ContainerRemoteError | None = None
    try:
        while True:
            if liveness is not None:
                liveness()
            try:
                return client.health_report(timeout_seconds=2.0)
            except ContainerRemoteError as error:
                if error.source == "protocol":
                    raise RuntimeProbeError(
                        "workload image does not implement the supported Syntara runtime ABI"
                    ) from error
                last_error = error
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.25)
    finally:
        channel.close()
    raise RuntimeProbeError(
        "workload image did not provide a runtime Health response"
    ) from last_error


@contextmanager
def _image_available(
    image: str,
    *,
    archive: Path | None,
    auth_file: Path | None,
    allow_insecure_loopback_http: bool,
) -> Iterator[str]:
    if archive is not None:
        try:
            expected_digest = image.rsplit("@", 1)[1]
            with load_oci_archive(archive, expected_digest=expected_digest) as local_image:
                yield local_image
        except (IndexError, ValueError, RunExecutionError) as error:
            raise RuntimeProbeError(
                "workload archive does not match its immutable image reference"
            ) from error
        return

    if shutil.which("podman") is None:
        raise RuntimeProbeError("Podman is required to probe a prebuilt workload image")
    existed = _image_id(image) is not None
    if not existed:
        _pull_image(
            image, auth_file=auth_file, allow_insecure_loopback_http=allow_insecure_loopback_http
        )
    image_id = _image_id(image)
    if image_id is None:
        raise RuntimeProbeError("Podman could not resolve the immutable workload image")
    try:
        yield image_id
    finally:
        if not existed:
            _run_podman(["image", "rm", image_id])


def _pull_image(
    image: str,
    *,
    auth_file: Path | None,
    allow_insecure_loopback_http: bool,
) -> None:
    command = ["pull", "--quiet"]
    if auth_file is not None:
        command.extend(("--authfile", str(auth_file)))
    registry = image.split("/", 1)[0] if "/" in image else ""
    if allow_insecure_loopback_http:
        if not _is_loopback_registry(registry):
            raise RuntimeProbeError("insecure image probing is restricted to loopback registries")
        command.append("--tls-verify=false")
    command.append(image)
    _run_podman(command)


def _is_loopback_registry(registry: str) -> bool:
    host = urlparse(f"//{registry}").hostname
    return host in {"localhost", "127.0.0.1", "::1"}


def _image_id(image: str) -> str | None:
    try:
        completed = subprocess.run(
            ["podman", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and _IMAGE_ID.fullmatch(value) else None


def _run_podman(arguments: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            ["podman", *arguments],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeProbeError(
            "Podman could not prepare the workload image for probing"
        ) from error
    if completed.returncode != 0:
        raise RuntimeProbeError("Podman could not prepare the workload image for probing")
    return completed.stdout.strip()
