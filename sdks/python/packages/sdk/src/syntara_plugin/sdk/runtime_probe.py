"""Probe runtime metadata from the exact image selected by a RuntimeBinding."""

from __future__ import annotations

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
from syntara_plugin.runtime.container import ContainerClient, ContainerHealth, ContainerRemoteError

from .container_engine import load_oci_archive, managed_runtime
from .runner import RunExecutionError, TlsOptions, _channel
from .workload import WorkloadBuildError

_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_RUNTIME_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+()_-]{0,255}$")
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
) -> list[dict[str, str]]:
    """Read a custom-runner Health report and match it to source declarations."""

    with _image_available(
        image,
        archive=archive,
        auth_file=auth_file,
        allow_insecure_loopback_http=allow_insecure_loopback_http,
    ) as local_image:
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

    reports = {item.name: item for item in report.runtime_components}
    if len(reports) != len(report.runtime_components):
        raise RuntimeProbeError("workload Health reported duplicate runtime component names")
    observations: list[dict[str, str]] = []
    for declaration in declarations:
        name = declaration["name"]
        executable = declaration["executable"]
        observed = reports.get(name)
        if observed is None:
            observations.append(
                {"name": name, "executable": executable, "verification": "unverified"}
            )
            continue
        if observed.executable != executable:
            raise RuntimeProbeError(
                f"runtime component {name!r} reports a different executable than its RuntimeBinding"
            )
        if observed.verification not in {"verified", "unverified"}:
            raise RuntimeProbeError(
                f"runtime component {name!r} reports an invalid verification status"
            )
        if observed.verification == "verified" and observed.version:
            if not _RUNTIME_VERSION.fullmatch(observed.version):
                raise RuntimeProbeError(
                    f"runtime component {name!r} reports an invalid version"
                )
            observations.append(
                {
                    "name": name,
                    "executable": executable,
                    "version": observed.version,
                    "verification": "verified",
                }
            )
        else:
            if observed.version:
                raise RuntimeProbeError(
                    f"runtime component {name!r} reports a version without verification"
                )
            observations.append(
                {"name": name, "executable": executable, "verification": "unverified"}
            )
    return observations


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
    raise RuntimeProbeError("workload image did not provide a runtime Health response") from last_error


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
            raise RuntimeProbeError("workload archive does not match its immutable image reference") from error
        return

    if shutil.which("podman") is None:
        raise RuntimeProbeError("Podman is required to probe a prebuilt workload image")
    existed = _image_id(image) is not None
    if not existed:
        _pull_image(image, auth_file=auth_file, allow_insecure_loopback_http=allow_insecure_loopback_http)
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
        raise RuntimeProbeError("Podman could not prepare the workload image for probing") from error
    if completed.returncode != 0:
        raise RuntimeProbeError("Podman could not prepare the workload image for probing")
    return completed.stdout.strip()
