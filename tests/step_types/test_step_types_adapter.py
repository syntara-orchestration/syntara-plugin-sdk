"""Tests for the mechanical migrations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from adapter import NotApplicable, migrate_workload_settings, rewrite_block

SETTINGS = """\
apiVersion: syntara.io/v1alpha1
kind: PluginBuildSettings
manifest: plugin.yaml

## Development defaults.
registry:
  origin: http://localhost:5000

build:
  artifactOutput: dist/http-request.oci.tar
  workload:
    engine: podman
    context: ..
    containerfile: Containerfile
    tag: 0.1.0
    platform: linux/amd64
    imageId: http-request-runner

publish:
  channel: 0.1.0
"""

MIGRATED_SETTINGS = """\
apiVersion: syntara.io/v1alpha1
kind: PluginBuildSettings
manifest: plugin.yaml

## Development defaults.
registry:
  origin: http://localhost:5000

build:
  artifactOutput: dist/http-request.oci.tar
  workloads:
    http-request:
      engine: podman
      context: ..
      containerfile: Containerfile
      tag: 0.1.0
      platform: linux/amd64

publish:
  channel: 0.1.0
"""

MANIFEST = """\
apiVersion: syntara.io/v1alpha1
kind: Plugin
spec:
  documentation:
    path: README.md
  workload:
    image: http-request-runner
  targets:
    - manifest.yaml
"""


def plugin(tmp_path: Path, manifest: str = MANIFEST) -> Path:
    directory = tmp_path / "http-request"
    directory.mkdir()
    (directory / "syntara-plugin.yaml").write_text(SETTINGS)
    (directory / "plugin.yaml").write_text(manifest)
    return directory


def test_migrates_settings_and_manifest(tmp_path: Path) -> None:
    directory = plugin(tmp_path)
    assert migrate_workload_settings(directory) == ["syntara-plugin.yaml", "plugin.yaml"]
    assert (directory / "syntara-plugin.yaml").read_text() == MIGRATED_SETTINGS
    manifest = (directory / "plugin.yaml").read_text()
    assert "  workloads:\n    http-request:\n      image: http-request-runner\n" in manifest
    assert "  targets:\n    - manifest.yaml\n" in manifest


def test_json_manifest_is_migrated(tmp_path: Path) -> None:
    document = {"spec": {"documentation": {"path": "README.md"}, "workload": {"image": "x"}}}
    directory = plugin(tmp_path, json.dumps(document))
    migrate_workload_settings(directory)
    migrated = json.loads((directory / "plugin.yaml").read_text())
    assert list(migrated["spec"]) == ["documentation", "workloads"]
    assert migrated["spec"]["workloads"] == {"http-request": {"image": "x"}}


def test_second_run_is_not_applicable(tmp_path: Path) -> None:
    directory = plugin(tmp_path)
    migrate_workload_settings(directory)
    with pytest.raises(NotApplicable):
        migrate_workload_settings(directory)


def test_missing_block_is_not_applicable() -> None:
    with pytest.raises(NotApplicable, match="no 'workload:' block"):
        rewrite_block("build:\n  other: 1\n", "build", "workload", "workloads", "x", frozenset())


def test_inline_value_is_not_applicable() -> None:
    text = "build:\n  workload: {engine: podman}\n"
    with pytest.raises(NotApplicable):
        rewrite_block(text, "build", "workload", "workloads", "x", frozenset())


def test_nested_children_of_dropped_key_are_removed() -> None:
    text = "build:\n  workload:\n    imageId:\n      nested: 1\n    tag: 1\n"
    assert rewrite_block(text, "build", "workload", "workloads", "x", frozenset({"imageId"})) == (
        "build:\n  workloads:\n    x:\n      tag: 1\n"
    )


def test_invalid_plugin_directory_name(tmp_path: Path) -> None:
    directory = tmp_path / "Bad_Name"
    directory.mkdir()
    with pytest.raises(NotApplicable, match="not a valid workload ID"):
        migrate_workload_settings(directory)
