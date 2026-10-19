from __future__ import annotations

import gzip
import json
import tarfile
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest
from syntara_plugin.sdk import (
    OCI_ARTIFACT_TYPE,
    OCI_CONFIG_MEDIA_TYPE,
    OCI_CONTENT_BUNDLE_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    OCI_PLUGIN_MANIFEST_MEDIA_TYPE,
    ArtifactBuildError,
    BuildRequest,
    CompilationResult,
    build_plugin_artifact,
    compile_workspace,
)
from syntara_plugin.sdk.artifact import _reference_media_type

DIGEST = "quay.io/acme/issue-tools@sha256:" + "a" * 64


def _workspace(tmp_path: Path) -> tuple[Path, bytes]:
    root = tmp_path / "plugin.yaml"
    action = tmp_path / "steps/create-issue/manifest.yaml"
    schema = tmp_path / "schemas/input.json"
    action.parent.mkdir(parents=True)
    schema.parent.mkdir()
    root.write_text(
        """apiVersion: syntara.io/v1alpha1
kind: Plugin
metadata:
  namespace: acme
  name: issue-tools
  version: 0.1.0
  displayName: Issue tools
  description: Creates GitHub issues.
spec:
  workload:
    image: plugin-workload
  documentation:
    path: docs/README.md
  targets:
    - steps/create-issue/manifest.yaml
"""
    )
    action.write_text(
        """apiVersion: syntara.io/v1alpha1
kind: Action
metadata:
  name: create-issue
  displayName: Create issue
  description: Creates one issue.
spec:
  runtime:
    kind: custom-runner
    entryPoint: issue_tools.actions:CreateIssue
  input:
    kind: asset
    path: schemas/input.json
  output:
    kind: inline
    value:
      type: object
  error:
    kind: inline
    value:
      type: object
"""
    )
    raw_schema = b'{\n  "type": "object",\n  "properties": {"title": {"type": "string"}}\n}\n'
    schema.write_bytes(raw_schema)
    documentation = tmp_path / "docs/README.md"
    documentation.parent.mkdir()
    documentation.write_bytes(b"# Issue tools\n\nCreate and label GitHub issues.\n")
    return root, raw_schema


def _compile(root: Path):
    return compile_workspace(BuildRequest(root, image_bindings={"plugin-workload": DIGEST}))


def test_build_plugin_artifact_is_deterministic_and_preserves_asset_bytes(tmp_path: Path) -> None:
    root, raw_schema = _workspace(tmp_path)
    compilation = _compile(root)

    first = build_plugin_artifact(compilation)
    second = build_plugin_artifact(_compile(root))

    assert first.digest == second.digest
    assert first.manifest.content == second.manifest.content
    assert first.content_bundle.content == second.content_bundle.content
    manifest = json.loads(first.manifest.content)
    assert manifest["schemaVersion"] == 2
    assert manifest["mediaType"] == OCI_IMAGE_MANIFEST_MEDIA_TYPE
    assert manifest["artifactType"] == OCI_ARTIFACT_TYPE
    assert [layer["mediaType"] for layer in manifest["layers"]] == [
        OCI_PLUGIN_MANIFEST_MEDIA_TYPE,
        OCI_CONTENT_BUNDLE_MEDIA_TYPE,
    ]
    assert json.loads(first.plugin_manifest.content) == compilation.descriptor

    config = json.loads(first.config.content)
    assert first.config.descriptor.media_type == OCI_CONFIG_MEDIA_TYPE
    assert config["pluginDescriptorDigest"] == compilation.digest
    assert config["assetIndex"]["schemas/input.json"] == {
        "canonicalDigest": compilation.asset_index["schemas/input.json"]["canonicalDigest"],
        "digest": f"sha256:{sha256(raw_schema).hexdigest()}",
        "mediaType": "application/schema+json",
        "references": ["steps/create-issue/manifest.yaml#/spec/input"],
        "size": len(raw_schema),
    }
    assert config["assetIndex"]["docs/README.md"]["mediaType"] == "text/markdown"
    assert config["assetIndex"]["docs/README.md"]["references"] == [
        "plugin.yaml#/spec/documentation"
    ]

    with tarfile.open(
        fileobj=BytesIO(gzip.decompress(first.content_bundle.content)), mode="r:"
    ) as archive:
        members = archive.getmembers()
        assert [(member.name, member.mode, member.mtime) for member in members] == [
            ("docs/README.md", 0o644, 0),
            ("schemas/input.json", 0o644, 0),
        ]
        extracted = archive.extractfile(members[1])
        assert extracted is not None
        assert extracted.read() == raw_schema


def test_artifact_builder_rejects_inconsistent_in_memory_asset_index(tmp_path: Path) -> None:
    root, _ = _workspace(tmp_path)
    compilation = _compile(root)
    asset = compilation.assets["schemas/input.json"]
    invalid = CompilationResult(
        descriptor=compilation.descriptor,
        assets={"schemas/other.json": asset},
    )

    with pytest.raises(ArtifactBuildError, match="does not match asset path"):
        build_plugin_artifact(invalid)


def test_artifact_builder_defends_against_hidden_in_memory_asset_path(tmp_path: Path) -> None:
    root, _ = _workspace(tmp_path)
    compilation = _compile(root)
    asset = compilation.assets["schemas/input.json"]
    hidden = type(asset)(
        path="schemas/.input.json",
        media_type=asset.media_type,
        content=asset.content,
        source_digest=asset.source_digest,
        canonical_digest=asset.canonical_digest,
        references=asset.references,
    )
    invalid = CompilationResult(descriptor=compilation.descriptor, assets={hidden.path: hidden})

    with pytest.raises(ArtifactBuildError, match="not a normalized relative POSIX path"):
        build_plugin_artifact(invalid)


def test_artifact_builder_rejects_markdown_as_a_schema_asset(tmp_path: Path) -> None:
    root, _ = _workspace(tmp_path)
    compilation = _compile(root)
    schema_asset = compilation.assets["schemas/input.json"]
    invalid_asset = type(schema_asset)(
        path="docs/input.md",
        media_type="text/markdown",
        content=schema_asset.content,
        source_digest=schema_asset.source_digest,
        canonical_digest=schema_asset.canonical_digest,
        references=schema_asset.references,
    )
    invalid = CompilationResult(
        descriptor=compilation.descriptor, assets={invalid_asset.path: invalid_asset}
    )

    with pytest.raises(ArtifactBuildError, match="non-schema media type"):
        build_plugin_artifact(invalid)


def test_artifact_builder_accepts_credential_schema_asset_references() -> None:
    """Credential schemas are valid typed schema assets in an OCI artifact."""
    assert (
        _reference_media_type(
            "schemas/github-service-token.credential.yaml",
            "credentials/github-service-token.yaml#/spec/credentialSchema",
        )
        == "application/schema+yaml"
    )
