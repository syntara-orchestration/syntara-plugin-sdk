"""Build resolved RuntimeBinding artifacts independently of plugin releases."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from syntara_plugin.contracts import (
    load_bundle,
    validate_runtime_binding,
)

from .oci import OCI_IMAGE_MANIFEST_MEDIA_TYPE, OciBlob, OciDescriptor, canonical_json_bytes

OCI_RUNTIME_BINDING_ARTIFACT_TYPE = "application/vnd.syntara.runtime-binding.v1"
OCI_RUNTIME_BINDING_CONFIG_MEDIA_TYPE = "application/vnd.syntara.runtime-binding.config.v1+json"
OCI_RUNTIME_BINDING_DOCUMENT_MEDIA_TYPE = "application/vnd.syntara.runtime-binding.document.v1+yaml"
_API_VERSION = "syntara.io/v1alpha1"


class RuntimeBindingArtifactBuildError(ValueError):
    """Raised when a resolved RuntimeBinding is not internally consistent."""


@dataclass(frozen=True)
class RuntimeBindingArtifact:
    """Offline OCI artifact containing one resolved runtime binding document."""

    config: OciBlob
    binding_document: OciBlob
    manifest: OciBlob

    @property
    def digest(self) -> str:
        """Return the immutable OCI manifest digest."""

        return self.manifest.descriptor.digest

    @property
    def blobs(self) -> Mapping[str, OciBlob]:
        """Return every uploadable blob, indexed by its verified digest."""

        return {
            blob.descriptor.digest: blob
            for blob in (self.config, self.binding_document, self.manifest)
        }


def build_runtime_binding_artifact(
    document: Mapping[str, Any],
    plugin_descriptor: Mapping[str, Any],
    plugin_manifest_descriptor: OciDescriptor,
) -> RuntimeBindingArtifact:
    """Build a deterministic OCI artifact after validating its exact plugin link."""

    diagnostics = validate_runtime_binding(
        load_bundle(), document, resolved=True, plugin_descriptor=plugin_descriptor
    )
    if diagnostics:
        raise RuntimeBindingArtifactBuildError(
            "; ".join(f"{item.code}: {item.message}" for item in diagnostics)
        )
    metadata = document["metadata"]
    name = metadata.get("name")
    namespace = metadata.get("namespace")
    if not isinstance(name, str) or not isinstance(namespace, str):
        raise RuntimeBindingArtifactBuildError("binding metadata must identify namespace and name")
    if (
        plugin_manifest_descriptor.media_type != OCI_IMAGE_MANIFEST_MEDIA_TYPE
        or plugin_manifest_descriptor.digest != document["spec"]["plugin"]["digest"]
        or plugin_manifest_descriptor.size <= 0
    ):
        raise RuntimeBindingArtifactBuildError(
            "plugin manifest descriptor must match the linked plugin artifact"
        )

    content = canonical_json_bytes(document)
    binding_document = OciBlob.create(
        content,
        OCI_RUNTIME_BINDING_DOCUMENT_MEDIA_TYPE,
        annotations={"org.opencontainers.image.title": "runtime-bindings.yaml"},
    )
    config = OciBlob.create(
        canonical_json_bytes(
            {
                "apiVersion": "syntara.runtime-binding.config/v1alpha1",
                "kind": "RuntimeBindingArtifactConfig",
                "bindingDocumentDigest": binding_document.descriptor.digest,
                "contractBundleDigest": load_bundle().digest,
                "pluginArtifactDigest": document["spec"]["plugin"]["digest"],
            }
        ),
        OCI_RUNTIME_BINDING_CONFIG_MEDIA_TYPE,
    )
    manifest = OciBlob.create(
        canonical_json_bytes(
            {
                "schemaVersion": 2,
                "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
                "artifactType": OCI_RUNTIME_BINDING_ARTIFACT_TYPE,
                "config": config.descriptor.as_dict(),
                "layers": [binding_document.descriptor.as_dict()],
                "subject": plugin_manifest_descriptor.as_dict(),
                "annotations": {
                    "org.opencontainers.image.title": f"{namespace}/{name}-runtime",
                    "org.opencontainers.image.version": str(metadata.get("release", "unversioned")),
                },
            }
        ),
        OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    )
    return RuntimeBindingArtifact(config, binding_document, manifest)


def resolve_runtime_binding(
    source: Mapping[str, Any],
    plugin_descriptor: Mapping[str, Any],
    *,
    plugin_repository: str,
    plugin_digest: str,
    image_bindings: Mapping[str, str],
) -> dict[str, Any]:
    """Resolve source build targets to immutable image references for one plugin."""

    bundle = load_bundle()
    diagnostics = validate_runtime_binding(bundle, source, resolved=False)
    if diagnostics:
        raise RuntimeBindingArtifactBuildError(
            "; ".join(f"{item.code}: {item.message}" for item in diagnostics)
        )
    if not plugin_repository or not plugin_digest.startswith("sha256:"):
        raise RuntimeBindingArtifactBuildError("plugin repository and digest are required")
    plugin_metadata = plugin_descriptor.get("metadata")
    plugin_spec = plugin_descriptor.get("spec")
    if not isinstance(plugin_metadata, Mapping) or not isinstance(plugin_spec, Mapping):
        raise RuntimeBindingArtifactBuildError("plugin descriptor metadata and spec are required")
    binding_metadata = source.get("metadata")
    if not isinstance(binding_metadata, Mapping):
        raise RuntimeBindingArtifactBuildError("RuntimeBinding metadata is required")

    workloads = _resolve_workload_options(source["spec"]["workloads"], image_bindings)
    step_revisions = _custom_runner_step_revisions(plugin_spec["targets"])
    resolved = {
        "apiVersion": _API_VERSION,
        "kind": "RuntimeBinding",
        "metadata": dict(binding_metadata),
        "spec": {
            "plugin": {"repository": plugin_repository, "digest": plugin_digest},
            "contract": {
                "pluginRevisionId": plugin_spec["plugin_revision_id"],
                "contractBundleDigest": plugin_spec["contractBundleDigest"],
                "stepRevisionIds": step_revisions,
            },
            "workloads": workloads,
        },
    }
    diagnostics = validate_runtime_binding(
        bundle, resolved, resolved=True, plugin_descriptor=plugin_descriptor
    )
    if diagnostics:
        raise RuntimeBindingArtifactBuildError(
            "; ".join(f"{item.code}: {item.message}" for item in diagnostics)
        )
    return resolved


def _resolve_workload_options(
    source_workloads: Mapping[str, Any], image_bindings: Mapping[str, str]
) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    for workload_id, workload in source_workloads.items():
        resolved[workload_id] = {
            "default": workload["default"],
            "options": [
                _resolve_workload_option(option, image_bindings) for option in workload["options"]
            ],
        }
    return resolved


def _resolve_workload_option(
    option: Mapping[str, Any], image_bindings: Mapping[str, str]
) -> dict[str, Any]:
    image_source = option["image"]
    if "buildTarget" in image_source:
        image = image_bindings.get(image_source["buildTarget"])
        if image is None:
            raise RuntimeBindingArtifactBuildError(
                f"no image binding was supplied for build target {image_source['buildTarget']!r}"
            )
    else:
        image = image_source["reference"]
    metadata = {key: option[key] for key in ("name", "displayName", "description") if key in option}
    return metadata | {"image": image}


def _custom_runner_step_revisions(targets: list[Mapping[str, Any]]) -> list[str]:
    return sorted(
        target["step_revision_id"]
        for target in targets
        if target.get("kind") in {"Action", "Control", "Trigger"}
        and isinstance(target.get("runtime"), Mapping)
        and target["runtime"].get("kind") == "custom-runner"
    )
