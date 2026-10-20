from __future__ import annotations

import json
import tarfile
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from syntara_plugin.sdk import (
    ArtifactArchiveError,
    BuildRequest,
    PluginArtifact,
    build_plugin_artifact,
    compile_workspace,
    read_plugin_artifact_archive,
    write_plugin_artifact_archive,
)
from syntara_plugin.sdk.cli import main
from syntara_plugin.sdk.oci import OCI_IMAGE_MANIFEST_MEDIA_TYPE, OciBlob, canonical_json_bytes


def _artifact(tmp_path: Path):
    """Create a minimal public authoring workspace and assemble its artifact."""
    workspace = tmp_path / "status-tools"
    assert main(["init", str(workspace)]) == 0
    return build_plugin_artifact(compile_workspace(BuildRequest(workspace / "plugin.yaml")))


def _archive_with_descriptor_mutation(
    artifact: PluginArtifact,
    tmp_path: Path,
    archive_name: str,
    mutate: Callable[[dict[str, Any]], None],
) -> Path:
    """Rebuild the OCI graph after mutating the compiled descriptor document."""

    descriptor_document = json.loads(artifact.plugin_manifest.content)
    mutate(descriptor_document)
    descriptor = OciBlob.create(
        canonical_json_bytes(descriptor_document), artifact.plugin_manifest.descriptor.media_type
    )
    config_document = json.loads(artifact.config.content)
    config_document["pluginDescriptorDigest"] = descriptor.descriptor.digest
    config = OciBlob.create(
        canonical_json_bytes(config_document), artifact.config.descriptor.media_type
    )
    manifest_document = json.loads(artifact.manifest.content)
    manifest_document["config"] = config.descriptor.as_dict()
    manifest_document["layers"][0] = descriptor.descriptor.as_dict()
    manifest = OciBlob.create(
        canonical_json_bytes(manifest_document), OCI_IMAGE_MANIFEST_MEDIA_TYPE
    )
    tampered = PluginArtifact(
        config=config,
        plugin_manifest=descriptor,
        content_bundle=artifact.content_bundle,
        manifest=manifest,
    )
    archive = tmp_path / archive_name
    write_plugin_artifact_archive(tampered, archive)
    return archive


def test_archive_round_trip_is_deterministic_and_preserves_the_artifact(tmp_path: Path) -> None:
    """A build archive can be transported without changing any OCI bytes."""
    artifact = _artifact(tmp_path)
    first = tmp_path / "first.oci.tar"
    second = tmp_path / "second.oci.tar"

    write_plugin_artifact_archive(artifact, first)
    write_plugin_artifact_archive(artifact, second)

    assert first.read_bytes() == second.read_bytes()
    assert read_plugin_artifact_archive(first) == artifact


def test_archive_reader_rejects_a_blob_whose_content_does_not_match_its_digest(
    tmp_path: Path,
) -> None:
    """Publication never uploads an archive whose declared OCI content was altered."""
    artifact = _artifact(tmp_path)
    original = tmp_path / "original.oci.tar"
    altered = tmp_path / "altered.oci.tar"
    write_plugin_artifact_archive(artifact, original)

    with tarfile.open(original, mode="r:") as source, tarfile.open(altered, mode="x") as target:
        for member in source.getmembers():
            payload_handle = source.extractfile(member)
            assert payload_handle is not None
            payload = payload_handle.read()
            if (
                member.name
                == f"blobs/sha256/{artifact.content_bundle.descriptor.digest.split(':', 1)[1]}"
            ):
                payload = b"!" + payload[1:]
            copied = tarfile.TarInfo(member.name)
            copied.size = len(payload)
            copied.mode = member.mode
            copied.mtime = member.mtime
            target.addfile(copied, BytesIO(payload))

    with pytest.raises(ArtifactArchiveError, match="does not match its descriptor"):
        read_plugin_artifact_archive(altered)


def test_archive_writer_refuses_to_replace_a_previous_build(tmp_path: Path) -> None:
    """An accidental second build cannot silently replace a reviewable artifact."""
    artifact = _artifact(tmp_path)
    output = tmp_path / "plugin.oci.tar"
    write_plugin_artifact_archive(artifact, output)

    with pytest.raises(ArtifactArchiveError, match="refusing to overwrite"):
        write_plugin_artifact_archive(artifact, output)


def test_archive_reader_rejects_a_self_consistent_archive_with_unbound_config(
    tmp_path: Path,
) -> None:
    """OCI hashes alone cannot prove config-to-descriptor semantic bindings."""
    artifact = _artifact(tmp_path)
    config = OciBlob.create(
        canonical_json_bytes(
            {
                "apiVersion": "syntara.plugin.config/v1alpha1",
                "kind": "PluginArtifactConfig",
                "pluginDescriptorDigest": "sha256:" + "0" * 64,
                "assetIndex": json.loads(artifact.config.content)["assetIndex"],
            }
        ),
        artifact.config.descriptor.media_type,
    )
    manifest_document = json.loads(artifact.manifest.content)
    manifest_document["config"] = config.descriptor.as_dict()
    manifest = OciBlob.create(
        canonical_json_bytes(manifest_document), OCI_IMAGE_MANIFEST_MEDIA_TYPE
    )
    malformed = type(artifact)(
        config=config,
        plugin_manifest=artifact.plugin_manifest,
        content_bundle=artifact.content_bundle,
        manifest=manifest,
    )
    archive = tmp_path / "malformed.oci.tar"
    write_plugin_artifact_archive(malformed, archive)

    with pytest.raises(ArtifactArchiveError, match="does not bind its plugin descriptor"):
        read_plugin_artifact_archive(archive)


def test_archive_reader_rejects_a_descriptor_schema_digest_that_does_not_bind_its_schema(
    tmp_path: Path,
) -> None:
    """A rebuilt OCI graph still cannot replace a schema without its canonical digest."""
    artifact = _artifact(tmp_path)
    def replace_input_schema(document: dict[str, Any]) -> None:
        document["spec"]["targets"][0]["input"]["type"] = "array"

    archive = _archive_with_descriptor_mutation(
        artifact, tmp_path, "schema-digest-tampered.oci.tar", replace_input_schema
    )

    with pytest.raises(ArtifactArchiveError, match="schema digest does not match"):
        read_plugin_artifact_archive(archive)


def test_archive_reader_rejects_duplicate_compiled_target_identities(tmp_path: Path) -> None:
    """An OCI-consistent descriptor cannot reuse an identity across Step types."""

    artifact = _artifact(tmp_path)
    def duplicate_target(document: dict[str, Any]) -> None:
        targets = document["spec"]["targets"]
        targets.append(dict(targets[0]))

    archive = _archive_with_descriptor_mutation(
        artifact, tmp_path, "duplicate-identity.oci.tar", duplicate_target
    )

    with pytest.raises(ArtifactArchiveError, match="duplicate target identities"):
        read_plugin_artifact_archive(archive)


@pytest.mark.parametrize("image", ["quay.io/acme/runner:latest", None])
def test_archive_reader_rejects_unpinned_workload_images(
    tmp_path: Path, image: str | None
) -> None:
    """Published workload descriptors cannot use tags or null image references."""

    artifact = _artifact(tmp_path)

    def add_invalid_workload(document: dict[str, Any]) -> None:
        document["spec"]["workloads"] = {
            "runner": {"abi": "syntara.container/v1alpha1", "image": image}
        }

    archive = _archive_with_descriptor_mutation(
        artifact, tmp_path, "unpinned-workload.oci.tar", add_invalid_workload
    )

    with pytest.raises(ArtifactArchiveError, match="pinned to a sha256 digest"):
        read_plugin_artifact_archive(archive)


def test_archive_reader_rejects_a_custom_runner_without_a_workload_entry(
    tmp_path: Path,
) -> None:
    """Every custom-runner target must resolve its published workload metadata."""

    artifact = _artifact(tmp_path)

    def remove_referenced_workload(document: dict[str, Any]) -> None:
        target = document["spec"]["targets"][0]
        target["runtime"] = {
            "abi": "syntara.container/v1alpha1",
            "entryPoint": "example.x:Request",
            "kind": "custom-runner",
            "workload": "runner",
        }
        document["spec"]["workloads"] = {}

    archive = _archive_with_descriptor_mutation(
        artifact, tmp_path, "missing-workload.oci.tar", remove_referenced_workload
    )

    with pytest.raises(ArtifactArchiveError, match="references a missing workload"):
        read_plugin_artifact_archive(archive)
