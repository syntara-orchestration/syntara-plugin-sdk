"""Immutable OCI representation and update rules for per-plugin binding indexes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from syntara_plugin.contracts import (
    RuntimeBindingCheckpoint,
    load_bundle,
    validate_runtime_binding_index,
)

from .oci import OCI_IMAGE_MANIFEST_MEDIA_TYPE, OciBlob, canonical_json_bytes

OCI_RUNTIME_BINDING_INDEX_ARTIFACT_TYPE = "application/vnd.syntara.runtime-binding-index.v1"
OCI_RUNTIME_BINDING_INDEX_CONFIG_MEDIA_TYPE = (
    "application/vnd.syntara.runtime-binding-index.config.v1+json"
)
OCI_RUNTIME_BINDING_INDEX_DOCUMENT_MEDIA_TYPE = (
    "application/vnd.syntara.runtime-binding-index.document.v1+json"
)


class RuntimeBindingIndexError(ValueError):
    """Raised when an immutable binding-index revision cannot be constructed."""


@dataclass(frozen=True)
class RuntimeBindingIndexArtifact:
    """A deterministic OCI artifact containing one validated binding index."""

    config: OciBlob
    document: OciBlob
    manifest: OciBlob

    @property
    def digest(self) -> str:
        """Return the immutable manifest digest."""
        return self.manifest.descriptor.digest


@dataclass(frozen=True)
class RuntimeBindingIndexSnapshot:
    """The currently published index and its registry manifest digest."""

    manifest_digest: str
    document: Mapping[str, Any]
    etag: str | None = None


def next_runtime_binding_index(
    *,
    current: Mapping[str, Any] | None,
    current_manifest_digest: str | None,
    plugin_digest: str,
    binding_repository: str,
    binding_digest: str,
    expires_in_hours: int,
    release: str | None = None,
    status: str = "available",
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """Append or update one binding status in the next immutable index generation."""

    issued_at = _validate_index_update_inputs(
        plugin_digest, binding_repository, binding_digest, expires_in_hours, status, now
    )
    candidate = _binding_index_entry(binding_repository, binding_digest, release, status)
    generation, previous_digest, entries, idempotent = _next_index_entries(
        current,
        current_manifest_digest,
        plugin_digest,
        binding_digest,
        candidate,
        issued_at,
    )
    if idempotent:
        assert current is not None
        return dict(current), True

    document = _build_binding_index_document(
        plugin_digest,
        generation,
        previous_digest,
        entries,
        issued_at,
        expires_in_hours,
    )
    return document, False


def _validate_index_update_inputs(
    plugin_digest: str,
    binding_repository: str,
    binding_digest: str,
    expires_in_hours: int,
    status: str,
    now: datetime | None,
) -> datetime:
    if not binding_repository or not binding_digest.startswith("sha256:"):
        raise RuntimeBindingIndexError("binding repository and sha256 digest are required")
    if not plugin_digest.startswith("sha256:"):
        raise RuntimeBindingIndexError("plugin digest must use sha256")
    if expires_in_hours <= 0:
        raise RuntimeBindingIndexError("index expiry must be positive")
    if status not in {"available", "withdrawn"}:
        raise RuntimeBindingIndexError("binding status must be available or withdrawn")
    issued_at = now or datetime.now(UTC)
    if issued_at.tzinfo is None or issued_at.utcoffset() is None:
        raise RuntimeBindingIndexError("index time must be timezone-aware")
    return issued_at


def _binding_index_entry(
    binding_repository: str, binding_digest: str, release: str | None, status: str
) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "bindingRepository": binding_repository,
        "bindingDigest": binding_digest,
        "status": status,
    }
    if release is not None:
        if not release:
            raise RuntimeBindingIndexError("release label must be non-empty when supplied")
        candidate["release"] = release
    return candidate


def _next_index_entries(
    current: Mapping[str, Any] | None,
    current_manifest_digest: str | None,
    plugin_digest: str,
    binding_digest: str,
    candidate: dict[str, Any],
    issued_at: datetime,
) -> tuple[int, str | None, list[dict[str, Any]], bool]:
    if current is None:
        if current_manifest_digest is not None:
            raise RuntimeBindingIndexError("a missing index cannot have a predecessor digest")
        return 1, None, [candidate], False
    generation, current_expiry, entries = _read_current_index_state(
        current, current_manifest_digest, plugin_digest
    )
    entries, idempotent = _replace_or_append_binding_entry(
        entries, candidate, binding_digest, current_expiry, issued_at
    )
    return generation + 1, current_manifest_digest, entries, idempotent


def _read_current_index_state(
    current: Mapping[str, Any], current_manifest_digest: str | None, plugin_digest: str
) -> tuple[int, datetime, list[dict[str, Any]]]:
    if current_manifest_digest is None:
        raise RuntimeBindingIndexError("an existing index requires its manifest digest")
    metadata = _mapping(current.get("metadata"), "current index metadata")
    spec = _mapping(current.get("spec"), "current index spec")
    if metadata.get("pluginDigest") != plugin_digest:
        raise RuntimeBindingIndexError("current index belongs to a different plugin digest")
    generation = metadata.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise RuntimeBindingIndexError("current index generation must be positive")
    raw_entries = spec.get("entries")
    if not isinstance(raw_entries, list):
        raise RuntimeBindingIndexError("current index entries must be a list")
    expires_at = metadata.get("expiresAt")
    if not isinstance(expires_at, str):
        raise RuntimeBindingIndexError("current index expiresAt must be a timestamp")
    try:
        current_expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeBindingIndexError("current index expiresAt must be a timestamp") from error
    if current_expiry.tzinfo is None or current_expiry.utcoffset() is None:
        raise RuntimeBindingIndexError("current index expiresAt must include a timezone")
    entries = [dict(_mapping(item, "current index entry")) for item in raw_entries]
    return generation, current_expiry, entries


def _replace_or_append_binding_entry(
    entries: list[dict[str, Any]],
    candidate: dict[str, Any],
    binding_digest: str,
    current_expiry: datetime,
    issued_at: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    matches = [
        index for index, item in enumerate(entries) if item.get("bindingDigest") == binding_digest
    ]
    if len(matches) > 1:
        raise RuntimeBindingIndexError("current index repeats a binding digest")
    if not matches:
        return [*entries, candidate], False
    match = matches[0]
    if entries[match] == candidate and current_expiry > issued_at:
        return entries, True
    entries[match] = candidate
    return entries, False


def _build_binding_index_document(
    plugin_digest: str,
    generation: int,
    previous_digest: str | None,
    entries: list[dict[str, Any]],
    issued_at: datetime,
    expires_in_hours: int,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "RuntimeBindingIndex",
        "metadata": {
            "pluginDigest": plugin_digest,
            "generation": generation,
            "issuedAt": _timestamp(issued_at),
            "expiresAt": _timestamp(issued_at + timedelta(hours=expires_in_hours)),
        },
        "spec": {
            "contractVersion": "syntara.io/v1alpha1",
            # Preserve release order. Consumers can select the newest eligible
            # binding by walking backward and skipping withdrawn entries.
            "entries": entries,
        },
    }
    if previous_digest is not None:
        document["metadata"]["previousIndexDigest"] = previous_digest
    checkpoint = RuntimeBindingCheckpoint(
        generation=generation - 1,
        index_digest=previous_digest,
    )
    diagnostics = validate_runtime_binding_index(
        load_bundle(), document, plugin_digest=plugin_digest, now=issued_at, checkpoint=checkpoint
    )
    if diagnostics:
        raise RuntimeBindingIndexError("; ".join(item.code for item in diagnostics))
    return document


def build_runtime_binding_index_artifact(
    document: Mapping[str, Any],
    *,
    allow_expired_predecessor: bool = False,
) -> RuntimeBindingIndexArtifact:
    """Build OCI bytes after checking the binding-index contract and freshness.

    ``allow_expired_predecessor`` is reserved for reconstructing an already
    published index while creating its successor. The publisher still applies
    strict freshness validation before it accepts an artifact for publication.
    """

    metadata = _mapping(document.get("metadata"), "index metadata")
    plugin_digest = metadata.get("pluginDigest")
    generation = metadata.get("generation")
    if not isinstance(plugin_digest, str) or not isinstance(generation, int):
        raise RuntimeBindingIndexError("index plugin digest and generation are required")
    now = datetime.now(UTC)
    checkpoint = RuntimeBindingCheckpoint(
        generation=generation - 1,
        index_digest=metadata.get("previousIndexDigest"),
    )
    diagnostics = validate_runtime_binding_index(
        load_bundle(),
        document,
        plugin_digest=plugin_digest,
        now=now,
        checkpoint=checkpoint,
        allow_expired_predecessor=allow_expired_predecessor,
    )
    if diagnostics:
        raise RuntimeBindingIndexError("; ".join(item.code for item in diagnostics))

    index_blob = OciBlob.create(
        canonical_json_bytes(document),
        OCI_RUNTIME_BINDING_INDEX_DOCUMENT_MEDIA_TYPE,
        annotations={"org.opencontainers.image.title": "runtime-binding-index.json"},
    )
    config = OciBlob.create(
        canonical_json_bytes(
            {
                "apiVersion": "syntara.runtime-binding-index.config/v1alpha1",
                "kind": "RuntimeBindingIndexArtifactConfig",
                "contractBundleDigest": load_bundle().digest,
                "documentDigest": index_blob.descriptor.digest,
                "generation": generation,
                "pluginDigest": plugin_digest,
            }
        ),
        OCI_RUNTIME_BINDING_INDEX_CONFIG_MEDIA_TYPE,
    )
    manifest = OciBlob.create(
        canonical_json_bytes(
            {
                "schemaVersion": 2,
                "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
                "artifactType": OCI_RUNTIME_BINDING_INDEX_ARTIFACT_TYPE,
                "config": config.descriptor.as_dict(),
                "layers": [index_blob.descriptor.as_dict()],
                "annotations": {
                    "org.opencontainers.image.title": f"runtime-binding-index/{plugin_digest}",
                    "org.opencontainers.image.version": str(generation),
                },
            }
        ),
        OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    )
    return RuntimeBindingIndexArtifact(config=config, document=index_blob, manifest=manifest)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeBindingIndexError(f"{label} must be an object")
    return value


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
