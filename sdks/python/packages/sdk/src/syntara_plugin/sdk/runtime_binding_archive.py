"""Portable, verified OCI-layout archives for RuntimeBinding artifacts."""

from __future__ import annotations

import json
import tarfile
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from syntara_plugin.contracts import load_bundle, validate_runtime_binding

from .artifact_archive import (
    OCI_LAYOUT_VERSION,
    ArtifactArchiveError,
    _add_bytes,
    _blob,
    _blob_path,
    _descriptor,
    _json_mapping,
    _read_archive_entries,
)
from .oci import OCI_IMAGE_MANIFEST_MEDIA_TYPE, OciBlob, OciDescriptor, canonical_json_bytes
from .runtime_binding import (
    OCI_RUNTIME_BINDING_ARTIFACT_TYPE,
    OCI_RUNTIME_BINDING_CONFIG_MEDIA_TYPE,
    OCI_RUNTIME_BINDING_DOCUMENT_MEDIA_TYPE,
    RuntimeBindingArtifact,
)

_MAX_RUNTIME_BINDING_ARCHIVE_BYTES = 16 * 1024 * 1024
_OCI_INDEX_FILENAME = "index.json"


def write_runtime_binding_archive(
    artifact: RuntimeBindingArtifact,
    destination: Path,
) -> Path:
    """Write a new OCI-layout tar archive without replacing prior release bytes."""

    destination = destination.expanduser()
    if destination.exists():
        raise ArtifactArchiveError(
            f"refusing to overwrite existing artifact archive: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(destination, mode="x", format=tarfile.PAX_FORMAT) as archive:
            _add_bytes(
                archive,
                "oci-layout",
                canonical_json_bytes({"imageLayoutVersion": OCI_LAYOUT_VERSION}),
            )
            _add_bytes(
                archive,
                _OCI_INDEX_FILENAME,
                canonical_json_bytes(
                    {
                        "schemaVersion": 2,
                        "manifests": [
                            {
                                **artifact.manifest.descriptor.as_dict(),
                                "annotations": json.loads(artifact.manifest.content)["annotations"],
                            }
                        ],
                    }
                ),
            )
            for blob in sorted(
                (artifact.config, artifact.binding_document, artifact.manifest),
                key=lambda item: item.descriptor.digest,
            ):
                _add_bytes(archive, _blob_path(blob.descriptor.digest), blob.content)
    except OSError as error:
        destination.unlink(missing_ok=True)
        raise ArtifactArchiveError("could not write RuntimeBinding archive") from error
    return destination


def read_runtime_binding_archive(
    source: Path,
    *,
    plugin_descriptor: Mapping[str, Any] | None = None,
    plugin_manifest_descriptor: OciDescriptor | None = None,
) -> RuntimeBindingArtifact:
    """Read and verify a RuntimeBinding OCI archive and optional plugin linkage."""

    source = source.expanduser()
    if not source.is_file():
        raise ArtifactArchiveError(f"artifact archive does not exist: {source}")
    if source.stat().st_size > _MAX_RUNTIME_BINDING_ARCHIVE_BYTES:
        raise ArtifactArchiveError("RuntimeBinding archive exceeds the 16 MiB safety limit")
    entries = _read_archive_entries(source)
    layout = _json_mapping(entries.get("oci-layout"), "OCI layout")
    if layout != {"imageLayoutVersion": OCI_LAYOUT_VERSION}:
        raise ArtifactArchiveError("RuntimeBinding archive has an unsupported OCI layout version")
    index = _json_mapping(entries.get(_OCI_INDEX_FILENAME), "OCI index")
    manifests = index.get("manifests")
    if not isinstance(manifests, list) or len(manifests) != 1:
        raise ArtifactArchiveError("RuntimeBinding archive index must name one manifest")
    indexed = _descriptor(manifests[0], "index manifest")
    if indexed.media_type != OCI_IMAGE_MANIFEST_MEDIA_TYPE:
        raise ArtifactArchiveError("RuntimeBinding archive index references a non-OCI manifest")
    manifest = _blob(entries, indexed)
    manifest_document = _json_mapping(manifest.content, "OCI manifest")
    config_descriptor = _descriptor(manifest_document.get("config"), "config")
    layers = manifest_document.get("layers")
    if not isinstance(layers, list) or len(layers) != 1:
        raise ArtifactArchiveError("RuntimeBinding OCI manifest must contain one document layer")
    binding_descriptor = _descriptor(layers[0], "RuntimeBinding document")
    config = _blob(entries, config_descriptor)
    binding_document = _blob(entries, binding_descriptor)
    expected_paths = {
        "oci-layout",
        _OCI_INDEX_FILENAME,
        _blob_path(indexed.digest),
        _blob_path(config_descriptor.digest),
        _blob_path(binding_descriptor.digest),
    }
    if set(entries) != expected_paths:
        raise ArtifactArchiveError(
            "RuntimeBinding archive contains unexpected or missing OCI members"
        )
    _validate_runtime_binding_contents(
        manifest_document,
        config,
        binding_document,
        plugin_descriptor=plugin_descriptor,
        plugin_manifest_descriptor=plugin_manifest_descriptor,
    )
    return RuntimeBindingArtifact(config, binding_document, manifest)


def _validate_runtime_binding_contents(
    manifest: Mapping[str, Any],
    config: OciBlob,
    binding_document: OciBlob,
    *,
    plugin_descriptor: Mapping[str, Any] | None,
    plugin_manifest_descriptor: OciDescriptor | None,
) -> None:
    """Validate OCI links and the language-neutral RuntimeBinding schema."""

    if (
        manifest.get("schemaVersion") != 2
        or manifest.get("mediaType") != OCI_IMAGE_MANIFEST_MEDIA_TYPE
        or manifest.get("artifactType") != OCI_RUNTIME_BINDING_ARTIFACT_TYPE
        or manifest.get("config") != config.descriptor.as_dict()
        or manifest.get("layers") != [binding_document.descriptor.as_dict()]
    ):
        raise ArtifactArchiveError("RuntimeBinding OCI manifest has an invalid artifact envelope")
    try:
        config_document = json.loads(config.content.decode("utf-8"))
        binding = json.loads(binding_document.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArtifactArchiveError(
            "RuntimeBinding OCI content is not valid UTF-8 JSON/YAML"
        ) from error
    if not isinstance(config_document, Mapping) or not isinstance(binding, Mapping):
        raise ArtifactArchiveError("RuntimeBinding OCI config and document must be objects")
    spec = binding.get("spec")
    plugin_link = spec.get("plugin") if isinstance(spec, Mapping) else None
    plugin_digest = plugin_link.get("digest") if isinstance(plugin_link, Mapping) else None
    subject = manifest.get("subject")
    if (
        config.descriptor.media_type != OCI_RUNTIME_BINDING_CONFIG_MEDIA_TYPE
        or binding_document.descriptor.media_type != OCI_RUNTIME_BINDING_DOCUMENT_MEDIA_TYPE
        or config_document.get("apiVersion") != "syntara.runtime-binding.config/v1alpha1"
        or config_document.get("kind") != "RuntimeBindingArtifactConfig"
        or config_document.get("bindingDocumentDigest") != binding_document.descriptor.digest
        or config_document.get("contractBundleDigest") != load_bundle().digest
        or config_document.get("pluginArtifactDigest") != plugin_digest
        or not isinstance(subject, Mapping)
        or subject.get("digest") != plugin_digest
        or subject.get("mediaType") != OCI_IMAGE_MANIFEST_MEDIA_TYPE
        or not isinstance(subject.get("size"), int)
        or isinstance(subject.get("size"), bool)
        or subject.get("size", 0) <= 0
    ):
        raise ArtifactArchiveError("RuntimeBinding OCI config does not bind the exact document")
    if plugin_manifest_descriptor is not None and subject != plugin_manifest_descriptor.as_dict():
        raise ArtifactArchiveError("RuntimeBinding OCI subject does not match the plugin manifest")
    diagnostics = validate_runtime_binding(
        load_bundle(), binding, resolved=True, plugin_descriptor=plugin_descriptor
    )
    if diagnostics:
        raise ArtifactArchiveError("RuntimeBinding document violates the supported contract")
    if (
        f"sha256:{sha256(binding_document.content).hexdigest()}"
        != binding_document.descriptor.digest
    ):
        raise ArtifactArchiveError("RuntimeBinding document digest does not match its bytes")
