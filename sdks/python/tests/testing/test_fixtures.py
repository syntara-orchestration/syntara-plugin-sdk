"""Dogfood tests: the four distributed fixtures used as an author would."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from syntara_plugin.runtime import ExecutionContext
from syntara_plugin.sdk import CompilationResult
from syntara_plugin.testing import invoke_step
from syntara_plugin.testing.payloads import ReferenceControlStep


def test_mock_execution_context_fixture_builds_contexts(
    mock_execution_context: Callable[..., ExecutionContext],
) -> None:
    context = mock_execution_context(invocation_id="fixture-1", workflow_run_id="run-1")
    assert context.invocation_id == "fixture-1"
    assert context.workflow_run_id == "run-1"


def test_step_bindings_fixture_binds_and_resolves(
    step_bindings: object,
) -> None:
    from syntara_plugin.testing import StepBindingsFactory

    assert isinstance(step_bindings, StepBindingsFactory)
    binding = step_bindings.bind(ReferenceControlStep)
    outcome = invoke_step(binding, {"title": "fixture"})
    assert outcome.ok


def test_manifest_validator_fixture_validates_documents(
    manifest_validator: object,
) -> None:
    from syntara_plugin.testing import ManifestValidationError, ManifestValidator

    assert isinstance(manifest_validator, ManifestValidator)
    valid = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Action",
        "metadata": {
            "name": "fixture-action",
            "displayName": "Fixture action",
            "description": "A fixture action.",
        },
        "spec": {
            "runtime": {
                "kind": "custom-runner",
                "entryPoint": "fixture_plugin.actions:FixtureAction",
            },
            "input": {"kind": "inline", "value": {"type": "object"}},
            "output": {"kind": "inline", "value": {"type": "object"}},
            "error": {"kind": "inline", "value": {"type": "object"}},
        },
    }
    manifest_validator(valid)
    try:
        manifest_validator({"kind": "Action"})
    except ManifestValidationError:
        pass
    else:
        raise AssertionError("expected ManifestValidationError for an invalid document")


def test_compile_manifest_fixture_compiles_a_workspace(
    tmp_path: Path,
    compile_manifest: Callable[..., CompilationResult],
) -> None:
    import yaml

    workspace = tmp_path / "fixture-plugin"
    (workspace / "steps/fixture-action").mkdir(parents=True)
    (workspace / "docs").mkdir()
    (workspace / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "syntara.io/v1alpha1",
                "kind": "Plugin",
                "metadata": {
                    "namespace": "testing",
                    "name": "fixture-plugin",
                    "version": "0.1.0",
                    "displayName": "Fixture",
                    "description": "A fixture plugin.",
                },
                "spec": {
                    "documentation": {"path": "docs/README.md"},
                    "targets": ["steps/fixture-action/manifest.yaml"],
                },
            }
        ),
        encoding="utf-8",
    )
    (workspace / "steps/fixture-action/manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "syntara.io/v1alpha1",
                "kind": "Action",
                "metadata": {
                    "name": "fixture-action",
                    "displayName": "Fixture action",
                    "description": "A fixture action.",
                },
                "spec": {
                    "runtime": {
                        "kind": "platform-runner",
                        "driver": "http.v1",
                        "operation": {
                            "method": "GET",
                            "path": "/items",
                            "responseMap": {
                                "kind": "inline",
                                "value": {"id": {"from": "/id"}},
                            },
                        },
                    },
                    "input": {"kind": "inline", "value": {"type": "object"}},
                    "output": {"kind": "inline", "value": {"type": "object"}},
                },
            }
        ),
        encoding="utf-8",
    )
    (workspace / "docs/README.md").write_text("# Fixture\n", encoding="utf-8")
    result = compile_manifest(workspace)
    assert result.descriptor["kind"] == "PluginDescriptor"
