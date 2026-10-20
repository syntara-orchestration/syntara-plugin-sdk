"""Portable, verified OCI-layout archives for Syntara plugin artifacts."""

from __future__ import annotations

import gzip
import json
import tarfile
from collections.abc import Mapping
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, cast

from syntara_plugin.contracts import (
    load_bundle,
    validate_document,
    validate_plugin_descriptor_semantics,
)

from .artifact import (
    OCI_ARTIFACT_TYPE,
    OCI_CONFIG_MEDIA_TYPE,
    OCI_CONTENT_BUNDLE_MEDIA_TYPE,
    OCI_PLUGIN_MANIFEST_MEDIA_TYPE,
    PluginArtifact,
)
from .oci import OCI_IMAGE_MANIFEST_MEDIA_TYPE, OciBlob, OciDescriptor, canonical_json_bytes

OCI_LAYOUT_VERSION = "1.0.0"
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_OCI_LAYOUT_FILENAME = "oci-layout"
_OCI_INDEX_FILENAME = "index.json"


class ArtifactArchiveError(ValueError):
    """Raised when a portable plugin archive is malformed or unsafe."""


def write_plugin_artifact_archive(artifact: PluginArtifact, destination: Path) -> Path:
    """Write one deterministic OCI-layout archive without overwriting a prior build."""
    destination = destination.expanduser()
    if destination.exists():
        raise ArtifactArchiveError(
            f"refusing to overwrite existing artifact archive: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)

    manifest = _manifest(artifact.manifest.content)
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                **artifact.manifest.descriptor.as_dict(),
                "annotations": dict(manifest["annotations"]),
            }
        ],
    }
    blobs = (artifact.config, artifact.plugin_manifest, artifact.content_bundle, artifact.manifest)
    with tarfile.open(destination, mode="x", format=tarfile.PAX_FORMAT) as archive:
        _add_bytes(
            archive,
            _OCI_LAYOUT_FILENAME,
            canonical_json_bytes({"imageLayoutVersion": OCI_LAYOUT_VERSION}),
        )
        _add_bytes(archive, _OCI_INDEX_FILENAME, canonical_json_bytes(index))
        for blob in sorted(blobs, key=lambda item: item.descriptor.digest):
            _add_bytes(archive, _blob_path(blob.descriptor.digest), blob.content)
    return destination


def read_plugin_artifact_archive(source: Path) -> PluginArtifact:
    """Read and fully verify one self-contained OCI-layout plugin archive."""
    source = source.expanduser()
    if not source.is_file():
        raise ArtifactArchiveError(f"artifact archive does not exist: {source}")
    if source.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise ArtifactArchiveError("artifact archive exceeds the 64 MiB safety limit")
    entries = _read_archive_entries(source)
    config, descriptor, bundle, indexed_manifest = _read_artifact_blobs(entries)
    expected_paths = {
        _OCI_LAYOUT_FILENAME,
        _OCI_INDEX_FILENAME,
        *(
            _blob_path(blob.descriptor.digest)
            for blob in (indexed_manifest, config, descriptor, bundle)
        ),
    }
    if set(entries) != expected_paths:
        raise ArtifactArchiveError("artifact archive contains unexpected or missing OCI members")
    _verify_plugin_contents(config, descriptor, bundle)
    # OCI index annotations describe how the archive is presented; they are not
    # part of the artifact manifest blob itself. Recreate the artifact's own
    # descriptor so reading and writing is a lossless PluginArtifact round trip.
    manifest = OciBlob.create(indexed_manifest.content, OCI_IMAGE_MANIFEST_MEDIA_TYPE)
    return PluginArtifact(
        config=config, plugin_manifest=descriptor, content_bundle=bundle, manifest=manifest
    )


def _verify_plugin_contents(config: OciBlob, descriptor: OciBlob, bundle: OciBlob) -> None:
    """Verify the semantic links that OCI content hashing cannot express."""

    config_document = _json_mapping(config.content, "plugin config")
    if config_document.get("apiVersion") != "syntara.plugin.config/v1alpha1":
        raise ArtifactArchiveError("plugin artifact config has an unsupported apiVersion")
    if config_document.get("kind") != "PluginArtifactConfig":
        raise ArtifactArchiveError("plugin artifact config has an unsupported kind")
    if config_document.get("pluginDescriptorDigest") != descriptor.descriptor.digest:
        raise ArtifactArchiveError("plugin artifact config does not bind its plugin descriptor")

    descriptor_document = _json_mapping(descriptor.content, "plugin descriptor")
    contract_bundle = load_bundle()
    semantic_errors = validate_plugin_descriptor_semantics(descriptor_document)
    if semantic_errors:
        raise ArtifactArchiveError(
            "plugin descriptor semantic validation failed: " + "; ".join(semantic_errors)
        )
    contract_errors = validate_document(contract_bundle, "plugin_descriptor", descriptor_document)
    if contract_errors:
        raise ArtifactArchiveError("plugin descriptor does not satisfy the supported contract")
    metadata = descriptor_document.get("metadata")
    specification = descriptor_document.get("spec")
    if not isinstance(metadata, Mapping) or not isinstance(specification, Mapping):
        raise ArtifactArchiveError("plugin descriptor must contain metadata and spec objects")
    if not all(isinstance(metadata.get(key), str) and metadata[key] for key in ("namespace", "name", "version")):
        raise ArtifactArchiveError("plugin descriptor metadata is incomplete")
    if specification.get("contractBundleDigest") != contract_bundle.digest:
        raise ArtifactArchiveError("plugin descriptor was compiled against an unsupported contract bundle")
    asset_index = specification.get("assets")
    if not isinstance(asset_index, Mapping):
        raise ArtifactArchiveError("plugin descriptor asset inventory is invalid")
    targets = specification.get("targets")
    workloads = specification.get("workloads")
    if not isinstance(targets, list) or not targets or not isinstance(workloads, Mapping):
        raise ArtifactArchiveError("plugin descriptor does not satisfy the supported contract")
    if config_document.get("assetIndex") != asset_index:
        raise ArtifactArchiveError("plugin artifact config asset index does not match its descriptor")
    documentation = specification.get("documentation")
    if not isinstance(documentation, Mapping) or not isinstance(documentation.get("path"), str):
        raise ArtifactArchiveError("plugin descriptor requires a documentation asset")
    documentation_asset = asset_index.get(documentation["path"])
    if (
        not isinstance(documentation_asset, Mapping)
        or documentation_asset.get("digest") != documentation.get("digest")
    ):
        raise ArtifactArchiveError("plugin descriptor documentation does not match its asset inventory")
    _verify_content_bundle(bundle.content, asset_index)


def _verify_content_bundle(content: bytes, asset_index: Mapping[str, Any]) -> None:
    """Verify source-byte inventory records against the compressed asset bundle."""

    expected_paths = set(asset_index)
    members: dict[str, bytes] = {}
    total_size = 0
    try:
        with gzip.GzipFile(fileobj=BytesIO(content), mode="rb") as compressed:
            with tarfile.open(fileobj=compressed, mode="r:") as archive:
                for member in archive.getmembers():
                    if not member.isfile() or not _safe_bundle_path(member.name):
                        raise ArtifactArchiveError("plugin content bundle contains an unsafe member")
                    total_size += member.size
                    if total_size > _MAX_ARCHIVE_BYTES:
                        raise ArtifactArchiveError("plugin content bundle exceeds the 64 MiB safety limit")
                    payload_handle = archive.extractfile(member)
                    if payload_handle is None:
                        raise ArtifactArchiveError("plugin content bundle member cannot be read")
                    if member.name in members:
                        raise ArtifactArchiveError("plugin content bundle contains duplicate member names")
                    members[member.name] = payload_handle.read()
    except (OSError, tarfile.TarError, EOFError) as error:
        raise ArtifactArchiveError("plugin content bundle is not a readable gzip tar file") from error
    if set(members) != expected_paths:
        raise ArtifactArchiveError("plugin content bundle does not match its asset inventory")
    for path, payload in members.items():
        entry = asset_index[path]
        if not isinstance(entry, Mapping):
            raise ArtifactArchiveError(f"plugin asset inventory entry {path!r} is invalid")
        digest = f"sha256:{sha256(payload).hexdigest()}"
        if entry.get("digest") != digest or entry.get("size") != len(payload):
            raise ArtifactArchiveError(f"plugin content bundle asset {path!r} does not match its inventory")
        if not isinstance(entry.get("mediaType"), str):
            raise ArtifactArchiveError(f"plugin content bundle asset {path!r} has no media type")
        if not isinstance(entry.get("canonicalDigest"), str) or not isinstance(
            entry.get("references"), list
        ):
            raise ArtifactArchiveError(f"plugin content bundle asset {path!r} has an invalid inventory")


def _safe_bundle_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(path) and not path.startswith("/") and all(part not in {"", ".", ".."} for part in parts)


def _read_archive_entries(source: Path) -> dict[str, bytes]:
    """Read only regular, relative members from a bounded OCI layout tarball."""
    try:
        with tarfile.open(source, mode="r:") as archive:
            members = archive.getmembers()
            if any(not _safe_member(member) for member in members):
                raise ArtifactArchiveError("artifact archive contains an unsafe member")
            entries = {member.name: _read_member(archive, member) for member in members}
    except (tarfile.TarError, OSError) as error:
        raise ArtifactArchiveError(
            "artifact archive is not a readable uncompressed tar file"
        ) from error
    if len(entries) != len(members):
        raise ArtifactArchiveError("artifact archive contains duplicate member names")
    return entries


def _safe_member(member: tarfile.TarInfo) -> bool:
    return (
        member.isfile() and not member.name.startswith("/") and ".." not in Path(member.name).parts
    )


def _read_artifact_blobs(
    entries: Mapping[str, bytes],
) -> tuple[OciBlob, OciBlob, OciBlob, OciBlob]:
    """Verify the fixed OCI artifact structure and return its four blobs."""
    layout = _json_mapping(entries.get(_OCI_LAYOUT_FILENAME), _OCI_LAYOUT_FILENAME)
    if layout != {"imageLayoutVersion": OCI_LAYOUT_VERSION}:
        raise ArtifactArchiveError("artifact archive has an unsupported OCI layout version")
    index = _json_mapping(entries.get(_OCI_INDEX_FILENAME), _OCI_INDEX_FILENAME)
    manifests = index.get("manifests")
    if not isinstance(manifests, list) or len(manifests) != 1:
        raise ArtifactArchiveError("artifact archive index must contain exactly one manifest")
    index_manifest_descriptor = _descriptor(manifests[0], "index manifest")
    if index_manifest_descriptor.media_type != OCI_IMAGE_MANIFEST_MEDIA_TYPE:
        raise ArtifactArchiveError("artifact archive index manifest has an invalid media type")
    indexed_manifest = _blob(entries, index_manifest_descriptor)
    manifest_document = _manifest(indexed_manifest.content)
    if manifest_document.get("artifactType") != OCI_ARTIFACT_TYPE:
        raise ArtifactArchiveError("artifact archive manifest has an unexpected artifact type")

    config = _blob(entries, _descriptor(manifest_document.get("config"), "manifest config"))
    layers = manifest_document.get("layers")
    if not isinstance(layers, list) or len(layers) != 2:
        raise ArtifactArchiveError("artifact archive manifest must contain exactly two layers")
    descriptor = _blob(entries, _descriptor(layers[0], "plugin descriptor layer"))
    bundle = _blob(entries, _descriptor(layers[1], "content bundle layer"))
    if config.descriptor.media_type != OCI_CONFIG_MEDIA_TYPE:
        raise ArtifactArchiveError("artifact archive config has an invalid media type")
    if descriptor.descriptor.media_type != OCI_PLUGIN_MANIFEST_MEDIA_TYPE:
        raise ArtifactArchiveError("artifact archive plugin descriptor has an invalid media type")
    if bundle.descriptor.media_type != OCI_CONTENT_BUNDLE_MEDIA_TYPE:
        raise ArtifactArchiveError("artifact archive content bundle has an invalid media type")
    return config, descriptor, bundle, indexed_manifest


def _add_bytes(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    member = tarfile.TarInfo(name=name)
    member.size = len(content)
    member.mode = 0o644
    member.mtime = 0
    member.uid = 0
    member.gid = 0
    member.uname = ""
    member.gname = ""
    archive.addfile(member, BytesIO(content))


def _blob_path(digest: str) -> str:
    algorithm, separator, value = digest.partition(":")
    if (
        algorithm != "sha256"
        or not separator
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ArtifactArchiveError("OCI descriptor digest must be a lowercase SHA-256 digest")
    return f"blobs/sha256/{value}"


def _read_member(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    extracted = archive.extractfile(member)
    if extracted is None:
        raise ArtifactArchiveError("artifact archive member cannot be read")
    return extracted.read()


def _json_mapping(content: bytes | None, label: str) -> Mapping[str, Any]:
    if content is None:
        raise ArtifactArchiveError(f"artifact archive is missing {label}")
    try:
        value = json.loads(content)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArtifactArchiveError(f"artifact archive {label} is not valid JSON") from error
    if not isinstance(value, Mapping):
        raise ArtifactArchiveError(f"artifact archive {label} must be an object")
    return cast(Mapping[str, Any], value)


def _manifest(content: bytes) -> Mapping[str, Any]:
    manifest = _json_mapping(content, "manifest")
    if (
        manifest.get("schemaVersion") != 2
        or manifest.get("mediaType") != OCI_IMAGE_MANIFEST_MEDIA_TYPE
    ):
        raise ArtifactArchiveError("artifact archive manifest is not an OCI image manifest")
    annotations = manifest.get("annotations")
    if not isinstance(annotations, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in annotations.items()
    ):
        raise ArtifactArchiveError("artifact archive manifest annotations are invalid")
    return manifest


def _descriptor(value: object, label: str) -> OciDescriptor:
    if not isinstance(value, Mapping):
        raise ArtifactArchiveError(f"artifact archive {label} descriptor must be an object")
    media_type = value.get("mediaType")
    digest = value.get("digest")
    size = value.get("size")
    annotations = value.get("annotations", {})
    if (
        not isinstance(media_type, str)
        or not isinstance(digest, str)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
    ):
        raise ArtifactArchiveError(f"artifact archive {label} descriptor is invalid")
    if not isinstance(annotations, Mapping) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in annotations.items()
    ):
        raise ArtifactArchiveError(f"artifact archive {label} annotations are invalid")
    return OciDescriptor(
        media_type=media_type,
        digest=digest,
        size=size,
        annotations=cast(Mapping[str, str], annotations),
    )


def _blob(entries: Mapping[str, bytes], descriptor: OciDescriptor) -> OciBlob:
    content = entries.get(_blob_path(descriptor.digest))
    if content is None:
        raise ArtifactArchiveError(f"artifact archive is missing blob {descriptor.digest}")
    if (
        len(content) != descriptor.size
        or f"sha256:{sha256(content).hexdigest()}" != descriptor.digest
    ):
        raise ArtifactArchiveError(
            f"artifact archive blob {descriptor.digest} does not match its descriptor"
        )
    return OciBlob(descriptor=descriptor, content=content)
