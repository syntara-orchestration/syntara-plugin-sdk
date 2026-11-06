"""Digest-preserving transfer of a published release and its OCI referrers.

The transfer adapter delegates OCI referrer traversal to the ORAS CLI. It does
not interpret signatures; it carries the subject and its referrers into an OCI
layout archive and verifies every subject digest at both ends.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

_DIGEST_REFERENCE = re.compile(
    r"^(?P<repository>[a-z0-9][a-z0-9._/-]{0,254})@(?P<digest>sha256:[0-9a-f]{64})$"
)
_TAG_REFERENCE = re.compile(
    r"^(?P<repository>[a-z0-9][a-z0-9._/-]{0,254}):(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})$"
)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_RECEIPT_BYTES = 1024 * 1024
_MAX_LAYOUT_ENTRIES = 200_000
_ORAS_TIMEOUT_SECONDS = 30 * 60
_HTTP_SCHEME = "http://"  # NOSONAR - allowed only for explicit loopback development registries.
_OCI_LAYOUT_INDEX = "index.json"


class ReleaseTransferError(ValueError):
    """A release transfer archive could not be safely prepared or imported."""


def export_release_publication(
    receipt_path: Path,
    output_path: Path,
    *,
    registry_origin: str,
    auth_file: Path | None = None,
    allow_insecure_loopback_http: bool = False,
) -> dict[str, Any]:
    """Export a completed release and all recursively attached OCI referrers."""
    receipt = _read_receipt(receipt_path)
    origin = _registry_origin(registry_origin, allow_insecure_loopback_http)
    auth_path = _auth_file(auth_file)
    oras = _find_oras()
    items = _publication_items(receipt)
    output = output_path.expanduser().resolve()
    if output.exists():
        raise ReleaseTransferError(f"refusing to overwrite release transfer archive: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="syntara-release-transfer-", dir=output.parent) as temp:
        layout = Path(temp) / "oci-layout"
        layout.mkdir()
        copied: set[str] = set()
        for item in items:
            immutable_reference = item["immutableReference"]
            digest = item["digest"]
            if immutable_reference in copied:
                continue
            local_tag = _layout_tag(immutable_reference)
            source = f"{origin}/{immutable_reference}"
            _resolve_digest(
                oras,
                source,
                digest,
                auth_file=auth_path,
                plain_http=origin.startswith(_HTTP_SCHEME),
            )
            _run_oras(
                oras,
                [
                    "cp",
                    "--recursive",
                    *(["--from-registry-config", str(auth_path)] if auth_path else []),
                    *(["--from-plain-http"] if origin.startswith(_HTTP_SCHEME) else []),
                    "--to-oci-layout",
                    source,
                    f"{layout}:{local_tag}",
                ],
            )
            _resolve_digest(
                oras,
                f"{layout}:{local_tag}",
                digest,
                auth_file=None,
                plain_http=False,
                oci_layout=True,
            )
            copied.add(immutable_reference)

        _require_layout(layout)
        temporary_handle = tempfile.NamedTemporaryFile(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent, delete=False
        )
        temporary_archive = Path(temporary_handle.name)
        temporary_handle.close()
        try:
            with tarfile.open(temporary_archive, mode="w") as archive:
                for child in sorted(layout.iterdir(), key=lambda path: path.name):
                    archive.add(child, arcname=child.name, recursive=True)
            # Link instead of replace so a concurrent invocation cannot replace
            # a transfer bundle that another process has already created.
            os.link(temporary_archive, output)
        except FileExistsError as error:
            raise ReleaseTransferError(
                f"refusing to overwrite release transfer archive: {output}"
            ) from error
        except (OSError, tarfile.TarError) as error:
            raise ReleaseTransferError(
                "could not write the OCI release transfer archive"
            ) from error
        finally:
            temporary_archive.unlink(missing_ok=True)

    return {
        "archive": str(output),
        "artifactCount": len(items),
        "ok": True,
        "referrers": "included recursively by ORAS",
    }


def import_release_publication(
    archive_path: Path,
    receipt_path: Path,
    *,
    registry_origin: str,
    auth_file: Path | None = None,
    allow_insecure_loopback_http: bool = False,
) -> dict[str, Any]:
    """Import release subjects and referrers, preserving and checking digests."""
    receipt = _read_receipt(receipt_path)
    origin = _registry_origin(registry_origin, allow_insecure_loopback_http)
    auth_path = _auth_file(auth_file)
    oras = _find_oras()
    archive = archive_path.expanduser().resolve()
    if not archive.is_file():
        raise ReleaseTransferError("release transfer archive must be a regular file")
    _validate_layout_archive(archive)
    items = _publication_items(receipt)

    for item in items:
        digest = item["digest"]
        source = f"{archive}:{_layout_tag(item['immutableReference'])}"
        _resolve_digest(oras, source, digest, auth_file=None, plain_http=False, oci_layout=True)
        destination_refs = item["destinationReferences"]
        repository = item["immutableReference"].rsplit("@", 1)[0]
        tags = [destination.rsplit(":", 1)[1] for destination in destination_refs]
        target = f"{origin}/{repository}:{','.join(tags)}"
        _run_oras(
            oras,
            [
                "cp",
                "--recursive",
                "--from-oci-layout",
                *(["--to-registry-config", str(auth_path)] if auth_path else []),
                *(["--to-plain-http"] if origin.startswith(_HTTP_SCHEME) else []),
                source,
                target,
            ],
        )
        for destination in destination_refs:
            _resolve_digest(
                oras,
                f"{origin}/{destination}",
                digest,
                auth_file=auth_path,
                plain_http=origin.startswith(_HTTP_SCHEME),
            )

    return {
        "artifactCount": len(items),
        "ok": True,
        "registryOrigin": origin,
        "referrers": "imported recursively by ORAS",
    }


def _read_receipt(path: Path) -> Mapping[str, Any]:
    receipt_path = path.expanduser()
    try:
        if receipt_path.stat().st_size > _MAX_RECEIPT_BYTES:
            raise ReleaseTransferError("release publication receipt exceeds the 1 MiB limit")
        value = json.loads(receipt_path.read_text(encoding="utf-8"))
    except ReleaseTransferError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseTransferError(
            "release publication receipt is unreadable or invalid JSON"
        ) from error
    if not isinstance(value, Mapping) or value.get("kind") != "PluginReleasePublication":
        raise ReleaseTransferError("input must be a PluginReleasePublication receipt")
    if value.get("ok") is not True:
        raise ReleaseTransferError("release publication receipt is incomplete")
    return value


def _publication_items(receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_items = receipt.get("publications")
    if not isinstance(raw_items, list) or not raw_items:
        raise ReleaseTransferError("release receipt has no published objects")
    items: list[dict[str, Any]] = []
    for position, raw in enumerate(raw_items):
        if not isinstance(raw, Mapping):
            raise ReleaseTransferError(
                f"release receipt publications[{position}] must be an object"
            )
        immutable = raw.get("immutableReference")
        match = _DIGEST_REFERENCE.fullmatch(immutable) if isinstance(immutable, str) else None
        if match is None or any(
            part in {"", ".", ".."} for part in match.group("repository").split("/")
        ):
            raise ReleaseTransferError(
                f"release receipt publications[{position}] has an invalid immutable reference"
            )
        destinations = raw.get("destinationReferences")
        if not isinstance(destinations, list) or not destinations:
            raise ReleaseTransferError(
                f"release receipt publications[{position}] has no transfer destination references"
            )
        checked_destinations: list[str] = []
        for destination in destinations:
            target_match = (
                _TAG_REFERENCE.fullmatch(destination) if isinstance(destination, str) else None
            )
            if (
                target_match is None
                or target_match.group("repository") != match.group("repository")
                or any(
                    part in {"", ".", ".."} for part in target_match.group("repository").split("/")
                )
            ):
                raise ReleaseTransferError(
                    f"release receipt publications[{position}] has an invalid destination reference"
                )
            checked_destinations.append(destination)
        items.append(
            {
                "digest": match.group("digest"),
                "destinationReferences": checked_destinations,
                "immutableReference": immutable,
            }
        )
    return items


def _registry_origin(value: str, allow_insecure_loopback_http: bool) -> str:
    parsed = urlparse(value)
    loopback = parsed.hostname in {"localhost", "127.0.0.1"}
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and not (allow_insecure_loopback_http and loopback))
    ):
        raise ReleaseTransferError(
            "registry origin must be HTTPS; HTTP is allowed only for explicit loopback use"
        )
    return value.rstrip("/")


def _auth_file(value: Path | None) -> Path | None:
    if value is None:
        return None
    path = value.expanduser().resolve()
    if not path.is_file():
        raise ReleaseTransferError("registry auth file must be a regular file")
    return path


def _find_oras() -> str:
    executable = shutil.which("oras")
    if executable is None:
        raise ReleaseTransferError("ORAS CLI is required for release transfer (oras 1.3 or newer)")
    return executable


def _resolve_digest(
    oras: str,
    reference: str,
    expected_digest: str,
    *,
    auth_file: Path | None,
    plain_http: bool,
    oci_layout: bool = False,
) -> None:
    command = ["resolve"]
    if oci_layout:
        command.append("--oci-layout")
    elif auth_file is not None:
        command.extend(("--registry-config", str(auth_file)))
    if plain_http:
        command.append("--plain-http")
    command.append(reference)
    result = _run_oras(oras, command)
    reported = result.strip()
    if _DIGEST.fullmatch(reported) is None or reported != expected_digest:
        raise ReleaseTransferError("release transfer did not preserve an OCI manifest digest")


def _run_oras(oras: str, arguments: list[str]) -> str:
    _validate_oras_arguments(arguments)
    try:
        result = (
            subprocess.run(  # NOSONAR - fixed ORAS argv allowlist; shell=False, validated operands.
                [oras, *arguments],
                capture_output=True,
                check=False,
                shell=False,
                timeout=_ORAS_TIMEOUT_SECONDS,
                text=True,
            )
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReleaseTransferError("ORAS command could not complete") from error
    if result.returncode != 0:
        raise ReleaseTransferError(
            f"ORAS release transfer failed with exit status {result.returncode}"
        )
    return result.stdout


def _validate_oras_arguments(arguments: list[str]) -> None:
    """Allow only the ORAS operations and flags used by this module."""
    if not arguments or arguments[0] not in {"cp", "resolve"}:
        raise ReleaseTransferError("unsupported ORAS operation")
    operation = arguments[0]
    flags = (
        {"--oci-layout", "--plain-http"}
        if operation == "resolve"
        else {
            "--recursive",
            "--from-oci-layout",
            "--to-oci-layout",
            "--from-plain-http",
            "--to-plain-http",
        }
    )
    value_flags = (
        {"--registry-config"}
        if operation == "resolve"
        else {"--from-registry-config", "--to-registry-config"}
    )
    operands = 0
    expecting_path = False
    for argument in arguments[1:]:
        if not isinstance(argument, str) or "\x00" in argument:
            raise ReleaseTransferError("ORAS arguments must be text without null bytes")
        if expecting_path:
            if not os.path.isabs(argument):
                raise ReleaseTransferError("ORAS registry configuration path must be absolute")
            expecting_path = False
        elif argument in value_flags:
            expecting_path = True
        elif argument in flags:
            continue
        elif argument.startswith("-"):
            raise ReleaseTransferError("unsupported ORAS command option")
        else:
            operands += 1
    expected_operands = 1 if operation == "resolve" else 2
    if expecting_path or operands != expected_operands:
        raise ReleaseTransferError("invalid ORAS command arguments")


def _layout_tag(immutable_reference: str) -> str:
    match = _DIGEST_REFERENCE.fullmatch(immutable_reference)
    if match is None:
        raise ReleaseTransferError("release receipt contains an invalid sha256 digest")
    repository_hash = sha256(match.group("repository").encode("utf-8")).hexdigest()[:12]
    return f"sha256-{match.group('digest').removeprefix('sha256:')}-{repository_hash}"


def _require_layout(path: Path) -> None:
    if not (path / "oci-layout").is_file() or not (path / _OCI_LAYOUT_INDEX).is_file():
        raise ReleaseTransferError("ORAS did not produce a complete OCI image layout")


def _validate_layout_archive(path: Path) -> None:
    found: set[str] = set()
    try:
        with tarfile.open(path, mode="r:") as archive:
            for position, member in enumerate(archive):
                if position >= _MAX_LAYOUT_ENTRIES:
                    raise ReleaseTransferError("release transfer archive has too many entries")
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts:
                    raise ReleaseTransferError("release transfer archive contains an unsafe path")
                if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                    raise ReleaseTransferError(
                        "release transfer archive contains an unsupported file type"
                    )
                normalized = "/".join(part for part in name.parts if part not in {"", "."})
                if normalized in {"oci-layout", _OCI_LAYOUT_INDEX}:
                    found.add(normalized)
        if found != {"oci-layout", _OCI_LAYOUT_INDEX}:
            raise ReleaseTransferError("input is not a complete OCI image layout archive")
    except ReleaseTransferError:
        raise
    except (OSError, tarfile.TarError) as error:
        raise ReleaseTransferError("release transfer archive is unreadable or invalid") from error
