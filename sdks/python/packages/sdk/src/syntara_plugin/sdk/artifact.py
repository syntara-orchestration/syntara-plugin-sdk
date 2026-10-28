"""Deterministic, offline OCI artifact assembly for compiled Syntara plugins.

This module deliberately stops before transport and trust operations.  It turns
an already-validated :class:`CompilationResult` into content-addressed OCI
blobs; registry upload, signing, and policy verification remain separate
adapters owned by later increments.
"""

from __future__ import annotations

import gzip
import tarfile
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import PurePosixPath

from syntara_plugin.contracts import (
    load_bundle,
    validate_document,
    validate_plugin_descriptor_semantics,
)

from .authoring import CompilationResult, CompiledAsset
from .oci import OCI_IMAGE_MANIFEST_MEDIA_TYPE, OciBlob, canonical_json_bytes

OCI_ARTIFACT_TYPE = "application/vnd.syntara.plugin.v1"
OCI_CONFIG_MEDIA_TYPE = "application/vnd.syntara.plugin.config.v1+json"
OCI_PLUGIN_MANIFEST_MEDIA_TYPE = "application/vnd.syntara.plugin.manifest.v1+yaml"
OCI_CONTENT_BUNDLE_MEDIA_TYPE = "application/vnd.syntara.plugin.content.v1.tar+gzip"

_SCHEMA_SOURCE_FIELDS = frozenset({"configuration", "credentialSchema", "error", "input", "output"})
_HTTP_MAPPING_SOURCE_FIELDS = frozenset(
    {"runtime/operation/requestMap", "runtime/operation/responseMap"}
)
_OCI_IMAGE_TITLE_ANNOTATION = "org.opencontainers.image.title"
_DOCUMENTATION_REFERENCE = "plugin.yaml#/spec/documentation"


class ArtifactBuildError(ValueError):
    """Raised when an in-process compilation result cannot form a safe artifact."""


@dataclass(frozen=True)
class PluginArtifact:
    """Offline OCI artifact ready for a later signing or registry adapter."""

    config: OciBlob
    plugin_manifest: OciBlob
    content_bundle: OciBlob
    manifest: OciBlob

    @property
    def digest(self) -> str:
        """Return the immutable OCI manifest digest used by catalogs/installers."""

        return self.manifest.descriptor.digest

    @property
    def blobs(self) -> Mapping[str, OciBlob]:
        """Return every uploadable blob indexed by its verified digest."""

        return {
            blob.descriptor.digest: blob
            for blob in (self.config, self.plugin_manifest, self.content_bundle, self.manifest)
        }


def build_plugin_artifact(compilation: CompilationResult) -> PluginArtifact:
    """Build a deterministic metadata-only OCI artifact from compiler output.

    The plugin metadata is canonical JSON, which is valid YAML 1.2 and can
    therefore safely use the versioned YAML media type.  The content bundle
    preserves exact source bytes for every externalized asset so consumers can
    verify descriptor, config, and archive inventories independently.
    """

    descriptor = compilation.descriptor
    errors = validate_document(load_bundle(), "plugin_descriptor", descriptor)
    errors.extend(validate_plugin_descriptor_semantics(descriptor))
    if errors:
        raise ArtifactBuildError("Invalid compiled descriptor: " + "; ".join(errors))
    metadata = descriptor.get("metadata") if isinstance(descriptor, Mapping) else None
    if descriptor.get("kind") != "PluginDescriptor" or not isinstance(metadata, Mapping):
        raise ArtifactBuildError(
            "Compilation result must contain a PluginDescriptor metadata mapping."
        )
    namespace = metadata.get("namespace")
    name = metadata.get("name")
    version = metadata.get("version")
    if not all(isinstance(value, str) and value for value in (namespace, name, version)):
        raise ArtifactBuildError(
            "PluginDescriptor metadata must include namespace, name, and version."
        )

    _validate_assets(compilation.assets)
    plugin_manifest = OciBlob.create(
        compilation.canonical_json(),
        OCI_PLUGIN_MANIFEST_MEDIA_TYPE,
        annotations={_OCI_IMAGE_TITLE_ANNOTATION: "plugin.manifest.yaml"},
    )
    content_bundle = OciBlob.create(
        _build_content_bundle(compilation.assets),
        OCI_CONTENT_BUNDLE_MEDIA_TYPE,
        annotations={_OCI_IMAGE_TITLE_ANNOTATION: "content.tar.gz"},
    )
    config = OciBlob.create(
        canonical_json_bytes(
            {
                "apiVersion": "syntara.plugin.config/v1alpha1",
                "kind": "PluginArtifactConfig",
                "pluginDescriptorDigest": compilation.digest,
                "assetIndex": compilation.asset_index,
            }
        ),
        OCI_CONFIG_MEDIA_TYPE,
    )
    manifest = OciBlob.create(
        canonical_json_bytes(
            {
                "schemaVersion": 2,
                "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
                "artifactType": OCI_ARTIFACT_TYPE,
                "config": config.descriptor.as_dict(),
                "layers": [
                    plugin_manifest.descriptor.as_dict(),
                    content_bundle.descriptor.as_dict(),
                ],
                "annotations": {
                    _OCI_IMAGE_TITLE_ANNOTATION: f"{namespace}/{name}",
                    "org.opencontainers.image.version": version,
                },
            }
        ),
        OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    )
    return PluginArtifact(config, plugin_manifest, content_bundle, manifest)


def _validate_assets(assets: Mapping[str, CompiledAsset]) -> None:
    seen_casefold: dict[str, str] = {}
    for path, asset in assets.items():
        _validate_asset(path, asset)
        folded = path.casefold()
        existing = seen_casefold.setdefault(folded, path)
        if existing != path:
            raise ArtifactBuildError(
                f"Asset path {path!r} collides with {existing!r} on case-insensitive filesystems."
            )


def _validate_asset(path: str, asset: CompiledAsset) -> None:
    if path != asset.path:
        raise ArtifactBuildError(
            f"Asset index key {path!r} does not match asset path {asset.path!r}."
        )
    _validate_asset_path(path)
    _validate_asset_content(path, asset)
    _validate_asset_references(path, asset)


def _validate_asset_path(path: str) -> None:
    parts = PurePosixPath(path).parts
    invalid_path = any(
        (
            not path,
            "\\" in path,
            path.startswith("/"),
            path != unicodedata.normalize("NFC", path),
            len(path) > 1024,
            _has_control_character(path),
            _has_unsafe_path_part(parts),
        )
    )
    if invalid_path:
        raise ArtifactBuildError(f"Asset path {path!r} is not a normalized relative POSIX path.")


def _has_control_character(path: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in path)


def _has_unsafe_path_part(parts: tuple[str, ...]) -> bool:
    return any(part in {"", ".", ".."} or part.startswith(".") for part in parts)


def _validate_asset_content(path: str, asset: CompiledAsset) -> None:
    if f"sha256:{sha256(asset.content).hexdigest()}" != asset.source_digest:
        raise ArtifactBuildError(f"Asset {path!r} source digest does not match its content.")


def _validate_asset_references(path: str, asset: CompiledAsset) -> None:
    if not asset.references or asset.references != tuple(sorted(set(asset.references))):
        raise ArtifactBuildError(f"Asset {path!r} must have sorted, unique source references.")
    for reference in asset.references:
        _validate_asset_reference(path, asset.media_type, reference)


def _validate_asset_reference(path: str, media_type: str, reference: str) -> None:
    if reference == _DOCUMENTATION_REFERENCE:
        if media_type != "text/markdown" or not path.endswith(".md"):
            raise ArtifactBuildError(
                f"Asset {path!r} has an invalid documentation reference {reference!r}."
            )
        return
    expected_media_type = _reference_media_type(path, reference)
    if media_type != expected_media_type:
        raise ArtifactBuildError(f"Asset {path!r} has an invalid media type for {reference!r}.")


def _reference_media_type(path: str, reference: str) -> str:
    """Return the one allowed media type for a typed asset reference."""

    source_path, separator, field = reference.partition("#/spec/")
    if not separator or not source_path:
        raise ArtifactBuildError(f"Asset {path!r} has an invalid source reference {reference!r}.")
    if field in _SCHEMA_SOURCE_FIELDS:
        prefix = "application/schema+"
    elif field in _HTTP_MAPPING_SOURCE_FIELDS:
        prefix = "application/"
    else:
        raise ArtifactBuildError(f"Asset {path!r} has an invalid source reference {reference!r}.")
    if path.endswith(".json"):
        return f"{prefix}json"
    if path.endswith((".yaml", ".yml")):
        return f"{prefix}yaml"
    if path.endswith(".md"):
        kind = "schema" if field in _SCHEMA_SOURCE_FIELDS else "HTTP mapping"
        raise ArtifactBuildError(f"Asset {path!r} has a non-{kind} media type for {reference!r}.")
    raise ArtifactBuildError(f"Asset {path!r} must use a JSON, YAML, or Markdown filename.")


def _build_content_bundle(assets: Mapping[str, CompiledAsset]) -> bytes:
    """Create a reproducible gzip-compressed tar without filesystem extraction."""

    output = BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0, filename="") as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path in sorted(assets):
                asset = assets[path]
                entry = tarfile.TarInfo(name=path)
                entry.size = len(asset.content)
                entry.mode = 0o644
                entry.uid = 0
                entry.gid = 0
                entry.uname = ""
                entry.gname = ""
                entry.mtime = 0
                entry.pax_headers = {}
                archive.addfile(entry, BytesIO(asset.content))
    return output.getvalue()
