"""Small, explicit Podman adapter for custom-runner authoring previews."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from syntara_plugin.contracts import (
    canonical_json_bytes,
    registration_manifest,
    validate_registration_manifest,
)

_SHA256_DIGEST_PREFIX = "sha256:"
REGISTRATION_MANIFEST_PATH = "/opt/syntara/registration-manifest.json"


class WorkloadBuildError(RuntimeError):
    """Raised when the selected workload-image build cannot produce a digest."""


@dataclass(frozen=True)
class WorkloadBuildRequest:
    """All validated inputs needed to build and push one author-owned image."""

    engine: str
    context: Path
    containerfile: Path
    repository: str
    tag: str
    platform: str
    allow_insecure_loopback_http: bool = False
    auth_file: Path | None = None
    registration_descriptor: Mapping[str, Any] | None = None
    workload_id: str | None = None

    @property
    def tagged_reference(self) -> str:
        return f"{self.repository}:{self.tag}"


def build_and_push_workload(request: WorkloadBuildRequest, *, verbose: bool = False) -> str:
    """Build and push through Podman, returning one immutable repository digest.

    The registry digest is read only from Podman's ``--digestfile`` result;
    this function never accepts a mutable tag as an SDK image binding.
    """

    _validate_request(request)
    manifest_bytes = _registration_manifest_bytes(request)
    build_id = uuid4().hex
    staging_reference = f"{request.repository}:syntara-stage-{build_id}"
    registered_reference = f"{request.repository}:syntara-registered-{build_id}"
    _build_staging_image(request, staging_reference, verbose=verbose)
    _build_registration_layer(
        request, staging_reference, registered_reference, manifest_bytes, verbose=verbose
    )
    digest = _push_image(request, registered_reference, verbose=verbose)
    if not _is_sha256_digest(digest):
        raise WorkloadBuildError("Podman did not report a valid sha256 image digest")
    return f"{request.repository}@{digest}"


def _validate_request(request: WorkloadBuildRequest) -> None:
    if request.engine != "podman":
        raise WorkloadBuildError("only the podman workload engine is supported")
    if not request.context.is_dir():
        raise WorkloadBuildError(f"workload context is not a directory: {request.context}")
    if not request.containerfile.is_file():
        raise WorkloadBuildError(
            f"workload containerfile is not a regular file: {request.containerfile}"
        )
    if not request.repository or "@" in request.repository:
        raise WorkloadBuildError("workload repository must be a non-empty taggable repository")
    if not request.tag or any(character.isspace() for character in request.tag):
        raise WorkloadBuildError("workload tag must be a non-empty tag without whitespace")
    if request.registration_descriptor is None or not request.workload_id:
        raise WorkloadBuildError("a compiled descriptor and workload ID are required")
    if not re.fullmatch(r"[A-Za-z0-9._:/-]+", request.tagged_reference):
        raise WorkloadBuildError("invalid workload image reference for registration layer")
    if not request.platform or "/" not in request.platform:
        raise WorkloadBuildError("workload platform must be explicit, for example linux/amd64")
    if request.allow_insecure_loopback_http and not _is_loopback_repository(request.repository):
        raise WorkloadBuildError(
            "insecure workload publication is restricted to localhost or 127.0.0.1"
        )


def _registration_manifest_bytes(request: WorkloadBuildRequest) -> bytes:
    assert request.registration_descriptor is not None
    assert request.workload_id is not None
    try:
        manifest = registration_manifest(request.registration_descriptor, request.workload_id)
        validate_registration_manifest(request.registration_descriptor, request.workload_id, manifest)
        return canonical_json_bytes(manifest)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise WorkloadBuildError(f"invalid workload registration: {error}") from error


def _build_staging_image(
    request: WorkloadBuildRequest, staging_reference: str, *, verbose: bool
) -> None:
    _run(
        [
            request.engine, "build", "--format", "oci", "--platform", request.platform,
            "--file", str(request.containerfile), "--tag", staging_reference,
            str(request.context),
        ],
        verbose=verbose,
    )


def _build_registration_layer(
    request: WorkloadBuildRequest,
    staging_reference: str,
    registered_reference: str,
    manifest_bytes: bytes,
    *,
    verbose: bool,
) -> None:
    with tempfile.TemporaryDirectory(prefix="syntara-registration-") as directory:
        context = Path(directory)
        (context / "manifest.json").write_bytes(manifest_bytes)
        containerfile = context / "Containerfile"
        containerfile.write_text(
            f"FROM {staging_reference}\nCOPY manifest.json {REGISTRATION_MANIFEST_PATH}\n",
            encoding="utf-8",
        )
        _run(
            [
                request.engine,
                "build",
                "--format",
                "oci",
                "--platform",
                request.platform,
                "--file",
                str(containerfile),
                "--tag",
                registered_reference,
                str(context),
            ],
            verbose=verbose,
        )


def _push_image(
    request: WorkloadBuildRequest, source_reference: str, *, verbose: bool
) -> str:
    digest_file = tempfile.NamedTemporaryFile(prefix="syntara-plugin-digest-", delete=False)
    digest_path = Path(digest_file.name)
    digest_file.close()
    try:
        command = [request.engine, "push", "--digestfile", str(digest_path)]
        if request.auth_file is not None:
            command.extend(("--authfile", str(request.auth_file)))
        if request.allow_insecure_loopback_http:
            command.append("--tls-verify=false")
        command.extend((source_reference, request.tagged_reference))
        _run(command, verbose=verbose)
        return digest_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise WorkloadBuildError(f"could not read Podman image digest: {error}") from error
    finally:
        digest_path.unlink(missing_ok=True)


def _run(command: list[str], *, verbose: bool) -> None:
    if verbose:
        print(f"+ {' '.join(command)}", file=sys.stderr)
    try:
        if verbose:
            subprocess.run(command, check=True, stdin=subprocess.DEVNULL, stdout=sys.stderr)
        else:
            completed = subprocess.run(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            if completed.returncode:
                detail = completed.stderr.strip()
                suffix = f": {detail}" if detail else ""
                raise WorkloadBuildError(
                    f"container engine command failed ({completed.returncode}){suffix}"
                )
    except FileNotFoundError as error:
        raise WorkloadBuildError("podman is not installed or is not available on PATH") from error
    except subprocess.CalledProcessError as error:
        raise WorkloadBuildError(f"container engine command failed ({error.returncode})") from error


def _is_sha256_digest(value: str) -> bool:
    return (
        len(value) == len(_SHA256_DIGEST_PREFIX) + 64
        and value.startswith(_SHA256_DIGEST_PREFIX)
        and all(
            character in "0123456789abcdef"
            for character in value.removeprefix(_SHA256_DIGEST_PREFIX)
        )
    )


def _is_loopback_repository(repository: str) -> bool:
    """Return whether an OCI repository starts with an approved loopback host."""

    host = repository.split("/", 1)[0].split(":", 1)[0]
    return host in {"localhost", "127.0.0.1"}
