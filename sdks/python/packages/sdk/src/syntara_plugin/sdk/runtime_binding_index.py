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

    candidate: dict[str, Any] = {
        "bindingRepository": binding_repository,
        "bindingDigest": binding_digest,
        "status": status,
    }
    if release is not None:
        if not release:
            raise RuntimeBindingIndexError("release label must be non-empty when supplied")
        candidate["release"] = release

    if current is None:
        if current_manifest_digest is not None:
            raise RuntimeBindingIndexError("a missing index cannot have a predecessor digest")
        generation = 1
        previous_digest = None
        entries = [candidate]
    else:
        if current_manifest_digest is None:
            raise RuntimeBindingIndexError("an existing index requires its manifest digest")
        metadata = _mapping(current.get("metadata"), "current index metadata")
        spec = _mapping(current.get("spec"), "current index spec")
        if metadata.get("pluginDigest") != plugin_digest:
            raise RuntimeBindingIndexError("current index belongs to a different plugin digest")
        old_generation = metadata.get("generation")
        if not isinstance(old_generation, int) or isinstance(old_generation, bool) or old_generation < 1:
            raise RuntimeBindingIndexError("current index generation must be positive")
        raw_entries = spec.get("entries")
        if not isinstance(raw_entries, list):
            raise RuntimeBindingIndexError("current index entries must be a list")
        entries = [dict(_mapping(item, "current index entry")) for item in raw_entries]
        matches = [index for index, item in enumerate(entries) if item.get("bindingDigest") == binding_digest]
        if len(matches) > 1:
            raise RuntimeBindingIndexError("current index repeats a binding digest")
        if matches:
            if entries[matches[0]] == candidate:
                return dict(current), True
            entries[matches[0]] = candidate
        else:
            entries.append(candidate)
        generation = old_generation + 1
        previous_digest = current_manifest_digest

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
    return document, False


def build_runtime_binding_index_artifact(
    document: Mapping[str, Any],
) -> RuntimeBindingIndexArtifact:
    """Build OCI bytes after checking the binding-index contract and freshness."""

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
        load_bundle(), document, plugin_digest=plugin_digest, now=now, checkpoint=checkpoint
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
