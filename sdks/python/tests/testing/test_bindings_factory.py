"""Tests for the drift-verified step-bindings factory."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel
from syntara_plugin.runtime import (
    ActionStep,
    ExecutionContext,
    SchemaDriftError,
    StepTaxonomy,
    TaskStep,
    TriggerStep,
)
from syntara_plugin.testing import (
    HarnessUsageError,
    StepBindingsFactory,
    assert_outcomes_equivalent,
    invoke_step,
    invoke_step_in_container,
)
from syntara_plugin.testing.payloads import ReferenceTaskStep

ENTRY_POINT = f"{__name__}:GreetActionStep"
IDENTITY = "testing.greet-plugin.greet"


class GreetInput(BaseModel):
    name: str
    enthusiasm: int = 1


class GreetOutput(BaseModel):
    message: str


class GreetActionStep(ActionStep[GreetInput, GreetOutput]):
    def run(self, inputs: GreetInput, context: ExecutionContext) -> GreetOutput:
        return GreetOutput(message=f"hello {inputs.name}" + "!" * inputs.enthusiasm)


class NormalizeInput(BaseModel):
    records: list[str]


class NormalizeOutput(BaseModel):
    normalized: list[str]


class NormalizeTask(TaskStep[NormalizeInput, NormalizeOutput]):
    def run(self, inputs: NormalizeInput, context: ExecutionContext) -> NormalizeOutput:
        return NormalizeOutput(normalized=[item.lower() for item in inputs.records])


class EmitEventInput(BaseModel):
    repository: str


class EmitEventOutput(BaseModel):
    issue_url: str


class EmitEventTrigger(TriggerStep[EmitEventInput, EmitEventOutput]):
    def run(self, inputs: EmitEventInput, context: ExecutionContext) -> EmitEventOutput:
        return EmitEventOutput(issue_url=f"https://example.test/{inputs.repository}/1")


def _action_manifest(
    input_schema: Mapping[str, Any],
    output_schema: Mapping[str, Any],
    entry_point: str = ENTRY_POINT,
    name: str = "greet",
) -> dict[str, Any]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Action",
        "metadata": {
            "name": name,
            "displayName": "Greet",
            "description": "Greets one person.",
        },
        "spec": {
            "runtime": {"kind": "custom-workload", "entryPoint": entry_point},
            "input": {"kind": "inline", "value": input_schema},
            "output": {"kind": "inline", "value": output_schema},
            "error": {"kind": "inline", "value": {"type": "object"}},
        },
    }


def _workspace(tmp_path: Path, manifest: Mapping[str, Any]) -> Path:
    workspace = tmp_path / "greet-plugin"
    (workspace / "steps/greet").mkdir(parents=True)
    (workspace / "docs").mkdir()
    (workspace / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "syntara.io/v1alpha1",
                "kind": "Plugin",
                "metadata": {
                    "namespace": "testing",
                    "name": "greet-plugin",
                    "version": "0.1.0",
                    "displayName": "Greet plugin",
                    "description": "Greets people.",
                },
                "spec": {
                    "documentation": {"path": "docs/README.md"},
                    "workload": {"image": "greet-workload"},
                    "targets": ["steps/greet/manifest.yaml"],
                },
            }
        ),
        encoding="utf-8",
    )
    (workspace / "steps/greet/manifest.yaml").write_text(
        yaml.safe_dump(manifest), encoding="utf-8"
    )
    (workspace / "docs/README.md").write_text("# Greet\n", encoding="utf-8")
    return workspace


def _matching_workspace(tmp_path: Path) -> Path:
    return _workspace(
        tmp_path,
        _action_manifest(GreetInput.model_json_schema(), GreetOutput.model_json_schema()),
    )


def _two_action_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "two-actions"
    (workspace / "steps/greet").mkdir(parents=True)
    (workspace / "steps/wave").mkdir(parents=True)
    (workspace / "docs").mkdir()
    (workspace / "plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "syntara.io/v1alpha1",
                "kind": "Plugin",
                "metadata": {
                    "namespace": "testing",
                    "name": "two-actions",
                    "version": "0.1.0",
                    "displayName": "Two actions",
                    "description": "Two custom-workload actions.",
                },
                "spec": {
                    "documentation": {"path": "docs/README.md"},
                    "workload": {"image": "two-actions-workload"},
                    "targets": ["steps/greet/manifest.yaml", "steps/wave/manifest.yaml"],
                },
            }
        ),
        encoding="utf-8",
    )
    (workspace / "steps/greet/manifest.yaml").write_text(
        yaml.safe_dump(
            _action_manifest(GreetInput.model_json_schema(), GreetOutput.model_json_schema())
        ),
        encoding="utf-8",
    )
    (workspace / "steps/wave/manifest.yaml").write_text(
        yaml.safe_dump(
            _action_manifest(
                GreetInput.model_json_schema(),
                GreetOutput.model_json_schema(),
                entry_point=f"{__name__}:WaveActionStep",
                name="wave",
            )
        ),
        encoding="utf-8",
    )
    (workspace / "docs/README.md").write_text("# Two actions\n", encoding="utf-8")
    return workspace


class WaveActionStep(ActionStep[GreetInput, GreetOutput]):
    def run(self, inputs: GreetInput, context: ExecutionContext) -> GreetOutput:
        return GreetOutput(message=f"wave {inputs.name}")


def test_bind_action_supports_multi_action_workspaces(tmp_path: Path) -> None:
    """Each call validates only its own target, so siblings never block it."""

    workspace = _two_action_workspace(tmp_path)
    factory = StepBindingsFactory()
    first = factory.bind_action(GreetActionStep, workspace=workspace)
    second = factory.bind_action(WaveActionStep, workspace=workspace)
    assert first.descriptor.step_identity == "testing.two-actions.greet"
    assert second.descriptor.step_identity == "testing.two-actions.wave"
    assert invoke_step(first, {"name": "a"}).ok
    assert invoke_step(second, {"name": "b"}).ok


def test_bind_action_matches_caller_supplied_entry_point_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-export entry point matches the manifest as written."""

    import sys
    import types

    reexport = types.ModuleType("reexport_helper")
    reexport.GreetActionStep = GreetActionStep
    monkeypatch.setitem(sys.modules, "reexport_helper", reexport)
    workspace = _workspace(
        tmp_path,
        _action_manifest(
            GreetInput.model_json_schema(),
            GreetOutput.model_json_schema(),
            entry_point="reexport_helper:GreetActionStep",
        ),
    )
    binding = StepBindingsFactory().bind_action(
        "reexport_helper:GreetActionStep", workspace=workspace
    )
    assert invoke_step(binding, {"name": "a"}).ok


def test_bind_task_builds_a_drift_verified_binding() -> None:
    factory = StepBindingsFactory()
    binding = factory.bind(NormalizeTask)
    assert binding.descriptor.taxonomy is StepTaxonomy.TASK
    assert factory.step_identities == (binding.descriptor.step_identity,)
    outcome = invoke_step(binding, {"records": ["Alpha", "Beta"]})
    assert outcome.ok
    assert outcome.output == {"normalized": ["alpha", "beta"]}


def test_bind_accepts_instances_and_entry_points() -> None:
    factory = StepBindingsFactory()
    binding = factory.bind(NormalizeTask())
    assert factory.resolve(f"{__name__}:NormalizeTask").descriptor is binding.descriptor
    from_entry_point = StepBindingsFactory().bind(f"{__name__}:NormalizeTask")
    assert from_entry_point.descriptor.taxonomy is StepTaxonomy.TASK
    assert from_entry_point.descriptor.step_identity == binding.descriptor.step_identity


def test_bind_supports_task_workflow_and_trigger_classes() -> None:
    factory = StepBindingsFactory()
    for step, taxonomy in (
        (NormalizeTask, StepTaxonomy.TASK),
        (EmitEventTrigger, StepTaxonomy.TRIGGER),
    ):
        binding = factory.bind(step)
        assert binding.descriptor.taxonomy is taxonomy


def test_bind_with_drifted_declared_schema_raises() -> None:
    drifted = {
        "type": "object",
        "properties": {"records": {"type": "string"}},
        "required": ["records"],
    }
    factory = StepBindingsFactory()
    with pytest.raises(SchemaDriftError):
        factory.bind(NormalizeTask, input_schema=drifted)


def test_bind_action_installs_the_compiled_manifest(tmp_path: Path) -> None:
    factory = StepBindingsFactory()
    binding = factory.bind_action(GreetActionStep, workspace=_matching_workspace(tmp_path))
    assert binding.descriptor.step_identity == IDENTITY
    assert binding.descriptor.taxonomy is StepTaxonomy.ACTION
    outcome = invoke_step(binding, {"name": "world", "enthusiasm": 3})
    assert outcome.ok
    assert outcome.output == {"message": "hello world!!!"}


def test_bind_action_accepts_a_directory_or_manifest_path(tmp_path: Path) -> None:
    workspace = _matching_workspace(tmp_path)
    from_directory = StepBindingsFactory().bind_action(GreetActionStep, workspace=workspace)
    from_manifest = StepBindingsFactory().bind_action(
        GreetActionStep, workspace=workspace / "plugin.yaml"
    )
    assert from_directory.descriptor.step_identity == from_manifest.descriptor.step_identity


def test_bind_action_resolves_by_identity_and_entry_point(tmp_path: Path) -> None:
    factory = StepBindingsFactory()
    factory.bind_action(GreetActionStep, workspace=_matching_workspace(tmp_path))
    assert factory.resolve(IDENTITY).descriptor.step_identity == IDENTITY
    assert factory.resolve(ENTRY_POINT).descriptor.step_identity == IDENTITY
    assert factory.step_identities == (IDENTITY,)


def test_bind_action_executes_over_the_container_path(tmp_path: Path) -> None:
    factory = StepBindingsFactory()
    binding = factory.bind_action(GreetActionStep, workspace=_matching_workspace(tmp_path))
    context_id = "bind-action-container"
    from syntara_plugin.testing import build_execution_context

    context = build_execution_context(invocation_id=context_id)
    direct = invoke_step(binding, {"name": "world"}, context=context)
    container = invoke_step_in_container(binding, {"name": "world"}, context=context)
    assert_outcomes_equivalent(direct, container)
    assert container.ok


def test_bind_action_fails_on_schema_drift(tmp_path: Path) -> None:
    drifted_input = {
        "type": "object",
        "properties": {"whom": {"type": "string"}},
        "required": ["whom"],
    }
    workspace = _workspace(
        tmp_path, _action_manifest(drifted_input, GreetOutput.model_json_schema())
    )
    factory = StepBindingsFactory()
    with pytest.raises(SchemaDriftError):
        factory.bind_action(GreetActionStep, workspace=workspace)
    # A failed drift check must not leave an unverified binding resolvable.
    assert factory.step_identities == ()
    with pytest.raises(HarnessUsageError, match="has not been bound yet"):
        factory.resolve(IDENTITY)


def test_resolve_accepts_identities_containing_colons() -> None:
    factory = StepBindingsFactory()
    identity = "testing.greet-plugin:qualified-action"
    binding = factory.bind(NormalizeTask, identity=identity)
    assert factory.resolve(identity).descriptor is binding.descriptor


def test_bind_action_fails_on_entry_point_drift(tmp_path: Path) -> None:
    workspace = _workspace(
        tmp_path,
        _action_manifest(
            GreetInput.model_json_schema(),
            GreetOutput.model_json_schema(),
            entry_point=f"{__name__}:NormalizeTask",
        ),
    )
    factory = StepBindingsFactory()
    with pytest.raises(SchemaDriftError) as raised:
        factory.bind_action(GreetActionStep, workspace=workspace, identity=IDENTITY)
    assert any(
        issue.code == "ENTRY_POINT_MISMATCH" for issue in raised.value.report.issues
    )


def test_bind_action_rejects_unknown_entry_points(tmp_path: Path) -> None:
    workspace = _workspace(
        tmp_path,
        _action_manifest(
            GreetInput.model_json_schema(),
            GreetOutput.model_json_schema(),
            entry_point="somewhere.else:Missing",
        ),
    )
    factory = StepBindingsFactory()
    with pytest.raises(HarnessUsageError, match="no Action target matches"):
        factory.bind_action(GreetActionStep, workspace=workspace)


def test_action_bound_with_bind_is_not_executable(tmp_path: Path) -> None:
    factory = StepBindingsFactory()
    binding = factory.bind(GreetActionStep)
    assert binding.descriptor.taxonomy is StepTaxonomy.ACTION
    with pytest.raises(HarnessUsageError, match="bind_action"):
        invoke_step(binding, {"name": "world"})


def test_strict_factory_fails_on_warning_drift(tmp_path: Path) -> None:
    loosened_output = {
        "type": "object",
        "properties": {"message": {"type": "string", "maxLength": 3}},
        "required": ["message"],
    }
    workspace = _workspace(
        tmp_path, _action_manifest(GreetInput.model_json_schema(), loosened_output)
    )
    factory = StepBindingsFactory(strict=True)
    with pytest.raises(SchemaDriftError):
        factory.bind_action(GreetActionStep, workspace=workspace)


def test_reference_task_step_binds_directly() -> None:
    factory = StepBindingsFactory()
    binding = factory.bind(ReferenceTaskStep)
    assert invoke_step(binding, {"title": "x"}).ok


def test_declared_view_excludes_bindings_from_other_workspaces(tmp_path: Path) -> None:
    factory = StepBindingsFactory()
    factory.bind(NormalizeTask)
    factory.bind_action(GreetActionStep, workspace=_matching_workspace(tmp_path))
    task_identity = f"{__name__}.{NormalizeTask.__qualname__}"
    assert set(factory.step_identities) == {task_identity, IDENTITY}
