"""Small, explicit Podman adapter for custom-runner authoring previews."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from syntara_plugin.contracts import (
    canonical_json_bytes,
    registration_manifest,
    validate_registration_manifest,
)

from .oci import OCI_IMAGE_MANIFEST_MEDIA_TYPE

_SHA256_DIGEST_PREFIX = "sha256:"
REGISTRATION_MANIFEST_PATH = "/opt/syntara/registration-manifest.json"
_MAX_OCI_ARCHIVE_MEMBERS = 100_000
_MAX_OCI_INDEX_BYTES = 1024 * 1024
_MAX_OCI_MANIFEST_BYTES = 16 * 1024 * 1024
_OCI_IMAGE_MANIFEST_LABEL = "OCI image manifest"


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
    try:
        _build_staging_image(request, staging_reference, verbose=verbose)
        _build_registration_layer(
            request, staging_reference, registered_reference, manifest_bytes, verbose=verbose
        )
        digest = _push_image(request, registered_reference, verbose=verbose)
        if not _is_sha256_digest(digest):
            raise WorkloadBuildError("Podman did not report a valid sha256 image digest")
        return f"{request.repository}@{digest}"
    finally:
        _remove_local_image(request.engine, registered_reference, verbose=verbose)
        _remove_local_image(request.engine, staging_reference, verbose=verbose)


def build_workload_archive(
    request: WorkloadBuildRequest,
    destination: Path,
    *,
    verbose: bool = False,
) -> str:
    """Build a registered workload image into a local OCI archive.

    No registry write occurs here. The returned immutable reference is derived
    from the OCI manifest bytes in the archive, so a later publish operation
    can require the registry to preserve that exact digest.
    """

    _validate_request(request)
    destination = destination.expanduser()
    if destination.exists():
        raise WorkloadBuildError(f"refusing to overwrite workload archive: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest_bytes = _registration_manifest_bytes(request)
    build_id = uuid4().hex
    staging_reference = f"{request.repository}:syntara-stage-{build_id}"
    registered_reference = f"{request.repository}:syntara-registered-{build_id}"
    try:
        _build_staging_image(request, staging_reference, verbose=verbose)
        _build_registration_layer(
            request, staging_reference, registered_reference, manifest_bytes, verbose=verbose
        )
        _run(
            [
                request.engine,
                "save",
                "--format",
                "oci-archive",
                "--output",
                str(destination),
                registered_reference,
            ],
            verbose=verbose,
        )
        digest = _oci_archive_manifest_digest(destination)
        return f"{request.repository}@{digest}"
    except (OSError, tarfile.TarError, json.JSONDecodeError, KeyError, TypeError) as error:
        destination.unlink(missing_ok=True)
        raise WorkloadBuildError(
            f"could not inspect the local OCI workload archive: {error}"
        ) from error
    except WorkloadBuildError:
        destination.unlink(missing_ok=True)
        raise
    finally:
        _remove_local_image(request.engine, registered_reference, verbose=verbose)
        _remove_local_image(request.engine, staging_reference, verbose=verbose)


def publish_workload_archive(
    archive: Path,
    destination: str,
    *,
    auth_file: Path | None = None,
    allow_insecure_loopback_http: bool = False,
    verbose: bool = False,
) -> str:
    """Copy an exact OCI archive to a registry and require digest preservation."""

    archive = archive.expanduser()
    if not archive.is_file():
        raise WorkloadBuildError("workload OCI archive is not a regular file")
    if "@" in destination or ":" not in destination.rsplit("/", 1)[-1]:
        raise WorkloadBuildError("workload destination must be a repository and explicit tag")
    if allow_insecure_loopback_http and not _is_loopback_repository(destination.rsplit(":", 1)[0]):
        raise WorkloadBuildError(
            "insecure workload publication is restricted to localhost or 127.0.0.1"
        )
    expected_digest = _oci_archive_manifest_digest(archive)
    digest_handle = tempfile.NamedTemporaryFile(prefix="syntara-workload-digest-", delete=False)
    digest_path = Path(digest_handle.name)
    digest_handle.close()
    try:
        command = ["skopeo", "copy", "--preserve-digests", "--digestfile", str(digest_path)]
        if auth_file is not None:
            command.extend(("--dest-authfile", str(auth_file)))
        if allow_insecure_loopback_http:
            command.append("--dest-tls-verify=false")
        command.extend((f"oci-archive:{archive}", f"docker://{destination}"))
        _run(command, verbose=verbose)
        reported = digest_path.read_text(encoding="utf-8").strip()
        if reported != expected_digest:
            raise WorkloadBuildError("registry copy did not preserve the local OCI manifest digest")
        return f"{destination.rsplit(':', 1)[0]}@{reported}"
    except OSError as error:
        raise WorkloadBuildError(f"could not read registry workload digest: {error}") from error
    finally:
        digest_path.unlink(missing_ok=True)


def _oci_archive_manifest_digest(archive_path: Path) -> str:
    """Read and hash the single image manifest referenced by an OCI archive."""

    with tarfile.open(archive_path, mode="r:") as archive:
        members = _oci_archive_members(archive)
        index_document = _oci_archive_json(archive, members, "index.json", _MAX_OCI_INDEX_BYTES)
        descriptor = _single_image_manifest_descriptor(index_document)
        digest = _image_manifest_digest(descriptor)
        content = _image_manifest_content(archive, members, descriptor, digest)
        _validate_image_manifest_content(content)
        return digest


def _oci_archive_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    for count, member in enumerate(archive, start=1):
        if count > _MAX_OCI_ARCHIVE_MEMBERS:
            raise WorkloadBuildError("OCI image archive has too many entries")
        if member.name in members:
            raise WorkloadBuildError("OCI image archive contains duplicate paths")
        members[member.name] = member
    return members


def _oci_archive_json(
    archive: tarfile.TarFile,
    members: Mapping[str, tarfile.TarInfo],
    path: str,
    maximum_size: int,
) -> Mapping[str, Any]:
    member = members.get(path)
    if member is None or not member.isfile() or member.size > maximum_size:
        raise WorkloadBuildError(f"OCI image archive has no bounded {path}")
    handle = archive.extractfile(member)
    if handle is None:
        raise WorkloadBuildError(f"OCI image archive {path} cannot be read")
    return _json_mapping(handle.read(), f"OCI image archive {path}")


def _single_image_manifest_descriptor(index: Mapping[str, Any]) -> Mapping[str, Any]:
    if index.get("schemaVersion") != 2:
        raise WorkloadBuildError("OCI image archive index has an unsupported schema version")
    manifests = index.get("manifests")
    if not isinstance(manifests, list) or len(manifests) != 1:
        raise WorkloadBuildError("workload OCI archive must contain exactly one image manifest")
    descriptor = _mapping(manifests[0], "OCI image manifest descriptor")
    if descriptor.get("mediaType") != OCI_IMAGE_MANIFEST_MEDIA_TYPE:
        raise WorkloadBuildError("OCI image archive references an unsupported manifest type")
    return descriptor


def _image_manifest_digest(descriptor: Mapping[str, Any]) -> str:
    digest = descriptor.get("digest")
    if not isinstance(digest, str) or not _is_sha256_digest(digest):
        raise WorkloadBuildError("OCI image archive manifest digest is invalid")
    size = descriptor.get("size")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
        or size > _MAX_OCI_MANIFEST_BYTES
    ):
        raise WorkloadBuildError("OCI image archive manifest descriptor size is invalid")
    return digest


def _image_manifest_content(
    archive: tarfile.TarFile,
    members: Mapping[str, tarfile.TarInfo],
    descriptor: Mapping[str, Any],
    digest: str,
) -> bytes:
    size = descriptor["size"]
    member = members.get(f"blobs/sha256/{digest.removeprefix('sha256:')}")
    if member is None or not member.isfile() or member.size != size:
        raise WorkloadBuildError("OCI image archive manifest blob is missing or oversized")
    handle = archive.extractfile(member)
    if handle is None:
        raise WorkloadBuildError("OCI image archive manifest blob cannot be read")
    content = handle.read()
    if f"sha256:{sha256(content).hexdigest()}" != digest:
        raise WorkloadBuildError("OCI image archive manifest digest does not match its bytes")
    return content


def _validate_image_manifest_content(content: bytes) -> None:
    manifest = _json_mapping(content, _OCI_IMAGE_MANIFEST_LABEL)
    if (
        manifest.get("schemaVersion") != 2
        or manifest.get("mediaType") != OCI_IMAGE_MANIFEST_MEDIA_TYPE
    ):
        raise WorkloadBuildError("OCI image archive manifest has an unsupported schema")


def workload_archive_digest(archive_path: Path) -> str:
    """Return the verified image-manifest digest of a local OCI archive."""
    try:
        return _oci_archive_manifest_digest(archive_path.expanduser())
    except (OSError, tarfile.TarError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise WorkloadBuildError("could not verify workload OCI archive") from error


def inspect_workload_archive(archive_path: Path) -> dict[str, Any]:
    """Read bounded OCI manifest/config metadata without unpacking image layers.

    The registration manifest is stored inside a layer, so this deliberately
    reports that it was not inspected instead of implying the image's runtime
    registration has been verified.
    """
    archive_path = archive_path.expanduser()
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            members: dict[str, tarfile.TarInfo] = {}
            for count, member in enumerate(archive, start=1):
                if count > 100_000:
                    raise WorkloadBuildError("OCI workload archive has too many entries")
                if member.name in members:
                    raise WorkloadBuildError("OCI workload archive contains duplicate paths")
                members[member.name] = member

            index = _read_archive_json(archive, members, "index.json", 1024 * 1024)
            manifests = index.get("manifests")
            if not isinstance(manifests, list) or len(manifests) != 1:
                raise WorkloadBuildError(
                    "workload OCI archive must contain exactly one image manifest"
                )
            manifest_descriptor = _mapping(manifests[0], "OCI manifest descriptor")
            manifest_blob = _read_archive_blob(
                archive,
                members,
                manifest_descriptor,
                expected_media_types={OCI_IMAGE_MANIFEST_MEDIA_TYPE},
                label=_OCI_IMAGE_MANIFEST_LABEL,
                maximum_bytes=16 * 1024 * 1024,
            )
            manifest = _json_mapping(manifest_blob, _OCI_IMAGE_MANIFEST_LABEL)
            if manifest.get("schemaVersion") != 2:
                raise WorkloadBuildError("OCI image manifest has an unsupported schema version")

            config_descriptor = _mapping(manifest.get("config"), "OCI image config descriptor")
            config_blob = _read_archive_blob(
                archive,
                members,
                config_descriptor,
                expected_media_types={
                    "application/vnd.oci.image.config.v1+json",
                    "application/vnd.docker.container.image.v1+json",
                },
                label="OCI image config",
                maximum_bytes=16 * 1024 * 1024,
            )
            config = _json_mapping(config_blob, "OCI image config")
            layers = manifest.get("layers")
            if not isinstance(layers, list):
                raise WorkloadBuildError("OCI image manifest layers must be a list")
            layer_summaries = []
            for position, raw_layer in enumerate(layers):
                layer = _mapping(raw_layer, f"OCI image layer {position}")
                digest = _archive_digest(layer.get("digest"), f"OCI image layer {position} digest")
                size = layer.get("size")
                if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                    raise WorkloadBuildError(f"OCI image layer {position} size is invalid")
                layer_summaries.append(
                    {"digest": digest, "mediaType": layer.get("mediaType"), "size": size}
                )

            platform = {
                key: config[key]
                for key in ("os", "architecture", "variant")
                if isinstance(config.get(key), str)
            }
            return {
                "configDigest": _archive_digest(
                    config_descriptor.get("digest"), "OCI image config digest"
                ),
                "kind": "WorkloadImage",
                "layerCount": len(layer_summaries),
                "layers": layer_summaries,
                "manifestDigest": _archive_digest(
                    manifest_descriptor.get("digest"), "OCI image manifest digest"
                ),
                "manifestMediaType": manifest_descriptor.get("mediaType"),
                "platform": platform,
                "registrationManifest": {
                    "inspected": False,
                    "reason": "registration data is stored in image layers",
                },
            }
    except WorkloadBuildError:
        raise
    except (OSError, tarfile.TarError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise WorkloadBuildError("could not inspect workload OCI archive") from error


def _read_archive_json(
    archive: tarfile.TarFile,
    members: Mapping[str, tarfile.TarInfo],
    path: str,
    maximum_bytes: int,
) -> dict[str, Any]:
    member = members.get(path)
    if member is None or not member.isfile() or member.size > maximum_bytes:
        raise WorkloadBuildError(f"OCI workload archive has no bounded {path}")
    handle = archive.extractfile(member)
    if handle is None:
        raise WorkloadBuildError(f"OCI workload archive {path} cannot be read")
    return _json_mapping(handle.read(maximum_bytes + 1), path)


def _read_archive_blob(
    archive: tarfile.TarFile,
    members: Mapping[str, tarfile.TarInfo],
    descriptor: Mapping[str, Any],
    *,
    expected_media_types: set[str],
    label: str,
    maximum_bytes: int,
) -> bytes:
    digest = _archive_digest(descriptor.get("digest"), f"{label} digest")
    media_type = descriptor.get("mediaType")
    if media_type not in expected_media_types:
        raise WorkloadBuildError(f"{label} has an unsupported media type")
    size = descriptor.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > maximum_bytes:
        raise WorkloadBuildError(f"{label} size is invalid or exceeds the inspection limit")
    member = members.get(f"blobs/sha256/{digest.removeprefix('sha256:')}")
    if member is None or not member.isfile() or member.size != size:
        raise WorkloadBuildError(f"{label} blob is missing or its size does not match")
    handle = archive.extractfile(member)
    if handle is None:
        raise WorkloadBuildError(f"{label} blob cannot be read")
    content = handle.read(maximum_bytes + 1)
    if len(content) != size or f"sha256:{sha256(content).hexdigest()}" != digest:
        raise WorkloadBuildError(f"{label} digest does not match its bytes")
    return content


def _json_mapping(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkloadBuildError(f"{label} is not valid JSON") from error
    return dict(_mapping(value, label))


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkloadBuildError(f"{label} must be an object")
    return value


def _archive_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not _is_sha256_digest(value):
        raise WorkloadBuildError(f"{label} is invalid")
    return value


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
        validate_registration_manifest(
            request.registration_descriptor, request.workload_id, manifest
        )
        return canonical_json_bytes(manifest)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise WorkloadBuildError(f"invalid workload registration: {error}") from error


def _build_staging_image(
    request: WorkloadBuildRequest, staging_reference: str, *, verbose: bool
) -> None:
    _run(
        [
            request.engine,
            "build",
            "--format",
            "oci",
            "--platform",
            request.platform,
            "--file",
            str(request.containerfile),
            "--tag",
            staging_reference,
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


def _push_image(request: WorkloadBuildRequest, source_reference: str, *, verbose: bool) -> str:
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


def _remove_local_image(engine: str, reference: str, *, verbose: bool) -> None:
    """Best-effort removal of one invocation's temporary Podman image tag."""

    try:
        _run([engine, "rmi", reference], verbose=verbose)
    except (OSError, WorkloadBuildError):
        # Cleanup must not hide the build or publication failure that triggered it.
        pass


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
