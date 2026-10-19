"""Tests for manifest validation and the compile helper."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from syntara_plugin.sdk import CompilationError, Plugin, Target
from syntara_plugin.testing import (
    HarnessUsageError,
    ManifestValidationError,
    ManifestValidator,
    compile_manifest,
)

DIGEST = "quay.io/testing/greet-workload@sha256:" + "a" * 64

ACTION_DOCUMENT: dict[str, Any] = {
    "apiVersion": "syntara.io/v1alpha1",
    "kind": "Action",
    "metadata": {
        "name": "greet",
        "displayName": "Greet",
        "description": "Greets one person.",
    },
    "spec": {
        "runtime": {"kind": "custom-workload", "entryPoint": "greet.steps:Greet"},
        "input": {"kind": "inline", "value": {"type": "object"}},
        "output": {"kind": "inline", "value": {"type": "object"}},
        "error": {"kind": "inline", "value": {"type": "object"}},
    },
}


def test_validator_accepts_a_valid_action_document() -> None:
    ManifestValidator()(ACTION_DOCUMENT, kind="action")


def test_validator_infers_the_kind_from_the_document() -> None:
    ManifestValidator()(ACTION_DOCUMENT)


def test_validator_reports_diagnostics_for_invalid_documents() -> None:
    invalid = {key: value for key, value in ACTION_DOCUMENT.items() if key != "metadata"}
    validator = ManifestValidator()
    diagnostics = validator.check(invalid)
    assert diagnostics
    with pytest.raises(ManifestValidationError) as raised:
        validator(invalid)
    assert raised.value.diagnostics


def test_validator_rejects_documents_without_an_inferable_kind() -> None:
    validator = ManifestValidator()
    with pytest.raises(ValueError, match="kind could not be inferred"):
        validator({"spec": {}})


def test_validator_names_the_bundle_escape_hatch_for_uncovered_kinds() -> None:
    validator = ManifestValidator()
    with pytest.raises(HarnessUsageError, match="bundle="):
        validator({"kind": "Task", "spec": {}})


def test_validator_maps_camel_case_kinds_to_contract_kinds() -> None:
    validator = ManifestValidator()
    # Kind inference must resolve to the kebab-case contract kind rather than
    # raising a raw KeyError for CamelCase manifest kinds.
    assert isinstance(validator.check({"kind": "CredentialRecipe"}), list)
    assert isinstance(validator.check({"kind": "IntegrationType"}), list)
    assert isinstance(validator.check({"kind": "CredentialRecipe"}, kind="CredentialRecipe"), list)


def test_validator_accepts_an_owner_supplied_bundle() -> None:
    from syntara_plugin.contracts import load_bundle

    ManifestValidator()(ACTION_DOCUMENT, bundle=load_bundle())


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "compile-plugin"
    (workspace / "steps/greet").mkdir(parents=True)
    (workspace / "docs").mkdir()
    (workspace / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "syntara.io/v1alpha1",
                "kind": "Plugin",
                "metadata": {
                    "namespace": "testing",
                    "name": "compile-plugin",
                    "version": "0.1.0",
                    "displayName": "Compile",
                    "description": "Compiles.",
                },
                "spec": {
                    "documentation": {"path": "docs/README.md"},
                    "workload": {"image": "compile-workload"},
                    "targets": ["steps/greet/manifest.yaml"],
                },
            }
        ),
        encoding="utf-8",
    )
    (workspace / "steps/greet/manifest.yaml").write_text(
        yaml.safe_dump(ACTION_DOCUMENT), encoding="utf-8"
    )
    (workspace / "docs/README.md").write_text("# Compile\n", encoding="utf-8")
    return workspace


def test_compile_manifest_accepts_a_workspace_directory(tmp_path: Path) -> None:
    result = compile_manifest(_workspace(tmp_path))
    targets = result.descriptor["spec"]["targets"]
    assert [target["kind"] for target in targets] == ["Action"]


def test_compile_manifest_accepts_a_manifest_path(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    from_directory = compile_manifest(workspace)
    from_manifest = compile_manifest(workspace / "plugin.yaml")
    assert from_directory.digest == from_manifest.digest


def test_compile_manifest_surfaces_compilation_errors(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "steps/greet/manifest.yaml").write_text(
        yaml.safe_dump({"apiVersion": "syntara.io/v1alpha1", "kind": "NotAKind"}),
        encoding="utf-8",
    )
    with pytest.raises(CompilationError):
        compile_manifest(workspace)


def test_compile_manifest_accepts_an_in_memory_plugin() -> None:
    plugin_document: dict[str, Any] = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Plugin",
        "metadata": {
            "namespace": "testing",
            "name": "memory-plugin",
            "version": "0.1.0",
            "displayName": "Memory",
            "description": "In memory.",
        },
        "spec": {
            "documentation": {"path": "docs/README.md"},
            "workload": {"image": "memory-workload"},
            "targets": ["steps/greet/manifest.yaml"],
        },
    }
    result = compile_manifest(
        Plugin(plugin_document, assets={"docs/README.md": b"# Memory\n"}),
        [Target("steps/greet/manifest.yaml", ACTION_DOCUMENT)],
        image_bindings={"memory-workload": DIGEST},
    )
    assert result.descriptor["spec"]["targets"][0]["kind"] == "Action"


def test_compiled_targets_carry_identity_and_resolved_schemas(
    tmp_path: Path,
) -> None:
    result = compile_manifest(_workspace(tmp_path))
    target = result.descriptor["spec"]["targets"][0]
    assert target["identity"] == "testing.compile-plugin.greet"
    assert isinstance(target["input"], Mapping)
    assert target["runtime"]["entryPoint"] == "greet.steps:Greet"
