from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from syntara_plugin.sdk import (
    BuildRequest,
    build_plugin_artifact,
    build_runtime_binding_artifact,
    compile_workspace,
    read_runtime_binding_archive,
    resolve_runtime_binding,
    write_plugin_artifact_archive,
    write_runtime_binding_archive,
)
from syntara_plugin.sdk.cli import _read_linked_runtime_binding_archive


def _plugin(tmp_path: Path) -> tuple[dict[str, object], Path]:
    """Compile a plugin with separate Python and Bash runtime contracts."""
    source = Path(__file__).parents[4] / "examples" / "python-steps"
    workspace = tmp_path / "plugin"
    shutil.copytree(source, workspace)
    plugin_path = workspace / "plugin.yaml"
    plugin = yaml.safe_load(plugin_path.read_text(encoding="utf-8"))
    plugin["spec"].pop("workload")
    plugin["spec"]["workloads"] = {
        "python": {"image": "python"},
        "bash": {"image": "bash"},
    }
    plugin_path.write_text(yaml.safe_dump(plugin, sort_keys=False), encoding="utf-8")
    action_path = workspace / "steps" / "create-issue" / "manifest.yaml"
    action = yaml.safe_load(action_path.read_text(encoding="utf-8"))
    action["spec"]["runtime"]["workload"] = "python"
    action_path.write_text(yaml.safe_dump(action, sort_keys=False), encoding="utf-8")
    trigger_path = workspace / "steps" / "record-opened" / "manifest.yaml"
    trigger = yaml.safe_load(trigger_path.read_text(encoding="utf-8"))
    trigger["spec"]["runtime"]["workload"] = "bash"
    trigger_path.write_text(yaml.safe_dump(trigger, sort_keys=False), encoding="utf-8")
    compiled = compile_workspace(
        BuildRequest(
            plugin_path,
            require_image_bindings=False,
            bind_workload_images=False,
        )
    )
    return dict(compiled.descriptor), workspace


def test_runtime_binding_archive_keeps_plugin_and_image_options_independent(
    tmp_path: Path,
) -> None:
    descriptor, workspace = _plugin(tmp_path)
    plugin_artifact = build_plugin_artifact(
        compile_workspace(
            BuildRequest(
                workspace / "plugin.yaml",
                require_image_bindings=False,
                bind_workload_images=False,
            )
        )
    )
    source = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "RuntimeBinding",
        "metadata": {
            "namespace": "example",
            "name": "python-steps-runtime-custom",
            "release": "security-refresh-1",
        },
        "spec": {
            "plugin": {"source": "plugin.yaml"},
            "workloads": {
                "python": {
                    "default": "python-base",
                    "options": [
                        {
                            "name": "python-base",
                            "displayName": "Python base",
                            "image": {"buildTarget": "python-base"},
                        },
                        {
                            "name": "python-aws",
                            "displayName": "Python with AWS SDK",
                            "image": {"buildTarget": "python-aws"},
                        },
                    ],
                },
                "bash": {
                    "default": "bash-base",
                    "options": [
                        {
                            "name": "bash-base",
                            "displayName": "Bash base",
                            "image": {"buildTarget": "bash-base"},
                        }
                    ],
                },
            },
        },
    }
    resolved = resolve_runtime_binding(
        source,
        descriptor,
        plugin_repository="acme/python-steps",
        plugin_digest=plugin_artifact.digest,
        image_bindings={
            "python-base": "quay.io/acme/python-base@sha256:" + "c" * 64,
            "python-aws": "quay.io/acme/python-aws@sha256:" + "d" * 64,
            "bash-base": "quay.io/acme/bash-base@sha256:" + "e" * 64,
        },
    )
    binding_artifact = build_runtime_binding_artifact(
        resolved, descriptor, plugin_artifact.manifest.descriptor
    )
    archive_path = tmp_path / "runtime-binding.oci.tar"
    plugin_archive_path = tmp_path / "plugin.oci.tar"

    write_runtime_binding_archive(binding_artifact, archive_path)
    write_plugin_artifact_archive(plugin_artifact, plugin_archive_path)
    reopened = read_runtime_binding_archive(archive_path, plugin_descriptor=descriptor)
    linked, linked_plugin, linked_document = _read_linked_runtime_binding_archive(
        archive_path,
        plugin_archive_path,
        expected_plugin_repository="acme/python-steps",
    )

    assert reopened.config.content == binding_artifact.config.content
    assert reopened.binding_document.content == binding_artifact.binding_document.content
    assert reopened.manifest.content == binding_artifact.manifest.content
    assert reopened.digest == binding_artifact.digest
    assert linked.digest == binding_artifact.digest
    assert linked_plugin.digest == plugin_artifact.digest
    assert linked_document["metadata"] == source["metadata"]
    assert json.loads(reopened.binding_document.content)["spec"]["plugin"]["digest"] == plugin_artifact.digest
    assert reopened.digest != plugin_artifact.digest
    assert resolved["spec"]["workloads"]["python"]["options"][1]["image"].endswith("d" * 64)
    changed_image = resolve_runtime_binding(
        source,
        descriptor,
        plugin_repository="acme/python-steps",
        plugin_digest=plugin_artifact.digest,
        image_bindings={
            "python-base": "quay.io/acme/python-base@sha256:" + "f" * 64,
            "python-aws": "quay.io/acme/python-aws@sha256:" + "1" * 64,
            "bash-base": "quay.io/acme/bash-base@sha256:" + "2" * 64,
        },
    )
    changed_binding = build_runtime_binding_artifact(
        changed_image, descriptor, plugin_artifact.manifest.descriptor
    )
    assert changed_binding.digest != binding_artifact.digest
    assert changed_binding.config.content != binding_artifact.config.content
    assert plugin_artifact.digest == build_plugin_artifact(
        compile_workspace(
            BuildRequest(
                workspace / "plugin.yaml",
                require_image_bindings=False,
                bind_workload_images=False,
            )
        )
    ).digest

    with pytest.raises(ValueError, match="registry.artifactRepository"):
        _read_linked_runtime_binding_archive(
            archive_path,
            plugin_archive_path,
            expected_plugin_repository="acme/another-plugin",
        )

    other_workspace = tmp_path / "other-plugin"
    shutil.copytree(workspace, other_workspace)
    other_manifest_path = other_workspace / "plugin.yaml"
    other_manifest = yaml.safe_load(other_manifest_path.read_text(encoding="utf-8"))
    other_manifest["metadata"]["version"] = "9.9.9"
    other_manifest_path.write_text(
        yaml.safe_dump(other_manifest, sort_keys=False), encoding="utf-8"
    )
    other_artifact = build_plugin_artifact(
        compile_workspace(
            BuildRequest(
                other_manifest_path,
                require_image_bindings=False,
                bind_workload_images=False,
            )
        )
    )
    other_archive_path = tmp_path / "other-plugin.oci.tar"
    write_plugin_artifact_archive(other_artifact, other_archive_path)
    with pytest.raises(ValueError):
        _read_linked_runtime_binding_archive(
            archive_path,
            other_archive_path,
            expected_plugin_repository="acme/python-steps",
        )
