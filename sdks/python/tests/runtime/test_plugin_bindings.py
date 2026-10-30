"""Tests for loading packaged plugins through the bindings wrapper."""

from __future__ import annotations

import importlib
import json
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from syntara_plugin.runtime import StepBindingError, load_plugin_bindings
from syntara_plugin.testing import (
    assert_outcomes_equivalent,
    invoke_step,
    invoke_step_in_container,
)

_PACKAGE_PREFIX = "wrapper_pkg_"
_STEPS_SOURCE = '''
"""Step implementations used by the packaged-plugin wrapper tests."""

from pydantic import BaseModel, ConfigDict

from syntara_plugin.runtime import (
    ActionStep,
    ControlStep,
    ExecutionContext,
    TriggerStep,
)


class SampleActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str


class SampleActionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str


class SampleActionStep(ActionStep[SampleActionInput, SampleActionOutput]):
    def run(self, inputs: SampleActionInput, context: ExecutionContext) -> SampleActionOutput:
        return SampleActionOutput(ticket_id=f"{context.invocation_id}:{inputs.title}")


class SampleControlInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[str]


class SampleControlOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int
    normalized: list[str]


class SampleControlStep(ControlStep[SampleControlInput, SampleControlOutput]):
    def run(
        self, inputs: SampleControlInput, context: ExecutionContext
    ) -> SampleControlOutput:
        return SampleControlOutput(
            count=len(inputs.records),
            normalized=[record.casefold() for record in inputs.records],
        )


class SampleTriggerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str


class SampleTriggerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    repository: str


class SampleTriggerStep(TriggerStep[SampleTriggerInput, SampleTriggerOutput]):
    def run(self, inputs: SampleTriggerInput, context: ExecutionContext) -> SampleTriggerOutput:
        workflow_run_id = context.workflow_run_id or "no-workflow"
        return SampleTriggerOutput(
            event_id=f"{workflow_run_id}:{inputs.repository}",
            repository=inputs.repository,
        )


class WrongKindStep(ControlStep[SampleActionInput, SampleActionOutput]):
    def run(self, inputs: SampleActionInput, context: ExecutionContext) -> SampleActionOutput:
        return SampleActionOutput(ticket_id=inputs.title)


class NotAStep:
    """A plain class that is not a BaseStep subclass."""
'''

_ACTION_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
}
_CONTROL_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"records": {"type": "array", "items": {"type": "string"}}},
    "required": ["records"],
}
_CONTROL_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "count": {"type": "integer"},
        "normalized": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["count", "normalized"],
}
_TRIGGER_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"repository": {"type": "string"}},
    "required": ["repository"],
}
_TRIGGER_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "event_id": {"type": "string"},
        "repository": {"type": "string"},
    },
    "required": ["event_id", "repository"],
}


@pytest.fixture
def plugin_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """An importable temporary root, cleaned of generated packages afterwards."""

    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path
    for module_name in [name for name in sys.modules if name.startswith(_PACKAGE_PREFIX)]:
        del sys.modules[module_name]


def _plugin_document(targets: list[str]) -> dict[str, Any]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Plugin",
        "metadata": {
            "namespace": "sample",
            "name": "demo",
            "version": "0.1.0",
            "displayName": "Demo",
            "description": "Demo plugin used by the wrapper tests.",
        },
        "spec": {
            "documentation": {"path": "docs/README.md"},
            "workload": {"image": "demo-workload"},
            "targets": targets,
        },
    }


def _action_document(
    entry_point: str, *, name: str = "create-ticket", input_source: Any = None
) -> dict[str, Any]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Action",
        "metadata": {
            "name": name,
            "displayName": "Create ticket",
            "description": "Creates one deterministic ticket identifier.",
        },
        "spec": {
            "runtime": {"kind": "custom-runner", "entryPoint": entry_point},
            "input": input_source
            or {"kind": "asset", "path": "schemas/action-input.json"},
            "output": {
                "kind": "inline",
                "value": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"ticket_id": {"type": "string"}},
                    "required": ["ticket_id"],
                },
            },
        },
    }


def _control_document(entry_point: str, *, name: str = "normalize-records") -> dict[str, Any]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Control",
        "metadata": {
            "name": name,
            "displayName": "Normalize records",
            "description": "Normalizes records before workflow selection.",
        },
        "spec": {
            "invocation": {"kind": "workflow-invocation", "workflow": "sample.demo.select"},
            "runtime": {"kind": "custom-runner", "entryPoint": entry_point},
            "input": {"kind": "inline", "value": _CONTROL_INPUT_SCHEMA},
            "output": {"kind": "inline", "value": _CONTROL_OUTPUT_SCHEMA},
        },
    }


def _trigger_document(entry_point: str, *, name: str = "record-opened") -> dict[str, Any]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Trigger",
        "metadata": {
            "name": name,
            "displayName": "Record opened",
            "description": "Produces one deterministic event identifier.",
        },
        "spec": {
            "driver": "webhook.v1",
            "runtime": {"kind": "custom-runner", "entryPoint": entry_point},
            "configuration": {"kind": "inline", "value": {"type": "object"}},
            "input": {"kind": "inline", "value": _TRIGGER_INPUT_SCHEMA},
            "output": {"kind": "inline", "value": _TRIGGER_OUTPUT_SCHEMA},
        },
    }


def _platform_action_document() -> dict[str, Any]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Action",
        "metadata": {
            "name": "fetch-http",
            "displayName": "Fetch HTTP",
            "description": "Fetches one URL through the platform runner.",
        },
        "spec": {
            "runtime": {"kind": "platform-runner", "implementation": "http.v1"},
            "input": {"kind": "inline", "value": {"type": "object"}},
            "output": {"kind": "inline", "value": {"type": "object"}},
        },
    }


def _integration_document() -> dict[str, Any]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "IntegrationType",
        "metadata": {
            "name": "tracker",
            "displayName": "Tracker",
            "description": "One external issue tracker.",
        },
        "spec": {"endpoint": "https://tracker.example.test"},
    }


def _credential_recipe_document() -> dict[str, Any]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "CredentialRecipe",
        "metadata": {
            "name": "tracker-token",
            "displayName": "Tracker token",
            "description": "One tracker API token.",
        },
        "spec": {"kind": "token"},
    }


def _standard_manifests(package: str, action_entry_point: str | None = None) -> dict[str, Any]:
    entry_point = action_entry_point or f"{package}.steps:SampleActionStep"
    return {
        "docs/README.md": "# Demo plugin\n",
        "schemas/action-input.json": _ACTION_INPUT_SCHEMA,
        "steps/create-ticket.yaml": _action_document(entry_point),
        "steps/normalize-records.yaml": _control_document(f"{package}.steps:SampleControlStep"),
        "steps/record-opened.yaml": _trigger_document(f"{package}.steps:SampleTriggerStep"),
        "steps/fetch-http.yaml": _platform_action_document(),
        "steps/tracker.yaml": _integration_document(),
    }


def _write_plugin(
    plugin_root: Path,
    *,
    plugin: Callable[[str], Any] | Any,
    manifests: Callable[[str], Mapping[str, Any]] | Mapping[str, Any],
    manifests_root: str = "manifests",
    serialize: Callable[[Mapping[str, Any]], str] | None = None,
) -> str:
    """Write one importable plugin package with its packaged manifests."""

    package = f"{_PACKAGE_PREFIX}{uuid.uuid4().hex}"
    root = plugin_root / package
    (root / manifests_root).mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "steps.py").write_text(_STEPS_SOURCE, encoding="utf-8")
    document = plugin(package) if callable(plugin) else plugin
    _write_document(root / manifests_root / "plugin.yaml", document, serialize)
    resolved = manifests(package) if callable(manifests) else manifests
    for relative, content in resolved.items():
        target = root / manifests_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            target.write_text(content, encoding="utf-8")
        elif relative.endswith(".json"):
            target.write_text(json.dumps(content), encoding="utf-8")
        else:
            _write_document(target, content, serialize)
    importlib.invalidate_caches()
    return package


def _write_document(
    path: Path,
    document: Any,
    serialize: Callable[[Mapping[str, Any]], str] | None,
) -> None:
    if isinstance(document, str):
        path.write_text(document, encoding="utf-8")
        return
    if serialize is not None:
        text = serialize(document)
    else:
        text = yaml.safe_dump(dict(document), sort_keys=False)
    path.write_text(text, encoding="utf-8")


def _standard_plugin(plugin_root: Path) -> str:
    return _write_plugin(
        plugin_root,
        plugin=_plugin_document(
            [
                "steps/create-ticket.yaml",
                "steps/normalize-records.yaml",
                "steps/record-opened.yaml",
                "steps/fetch-http.yaml",
                "steps/tracker.yaml",
            ]
        ),
        manifests=_standard_manifests,
    )


def test_binds_every_custom_runner_target_and_skips_other_kinds(plugin_root: Path) -> None:
    package = _standard_plugin(plugin_root)

    bindings = load_plugin_bindings(package)

    assert bindings.step_identities == (
        "sample.demo.create-ticket",
        "sample.demo.normalize-records",
        "sample.demo.record-opened",
    )


@pytest.mark.parametrize(
    ("identity", "inputs", "expected"),
    [
        (
            "sample.demo.create-ticket",
            {"title": "hello"},
            {"ticket_id": "wrapper-action:hello"},
        ),
        (
            "sample.demo.normalize-records",
            {"records": ["Alpha", "Beta"]},
            {"count": 2, "normalized": ["alpha", "beta"]},
        ),
        (
            "sample.demo.record-opened",
            {"repository": "demo-repo"},
            {"event_id": "wrapper-run:demo-repo", "repository": "demo-repo"},
        ),
    ],
)
def test_bindings_execute_on_direct_and_container_paths(
    plugin_root: Path,
    mock_execution_context: Any,
    identity: str,
    inputs: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    package = _standard_plugin(plugin_root)
    bindings = load_plugin_bindings(package)
    binding = bindings.binding(identity)
    context = mock_execution_context(
        invocation_id="wrapper-action", workflow_run_id="wrapper-run"
    )

    direct = invoke_step(binding, inputs, context=context)
    container = invoke_step_in_container(binding, inputs, context=context)

    assert direct.ok
    assert direct.run_calls == 1
    assert direct.output == expected
    assert_outcomes_equivalent(direct, container)


def test_yaml_and_json_manifests_bind_identically(plugin_root: Path) -> None:
    targets = [
        "steps/create-ticket.yaml",
        "steps/normalize-records.yaml",
        "steps/record-opened.yaml",
    ]
    yaml_package = _standard_plugin(plugin_root)
    json_package = _write_plugin(
        plugin_root,
        plugin=_plugin_document(targets),
        manifests=lambda package: {
            "docs/README.md": "# Demo plugin\n",
            "schemas/action-input.json": _ACTION_INPUT_SCHEMA,
            "steps/create-ticket.yaml": _action_document(f"{package}.steps:SampleActionStep"),
            "steps/normalize-records.yaml": _control_document(
                f"{package}.steps:SampleControlStep"
            ),
            "steps/record-opened.yaml": _trigger_document(
                f"{package}.steps:SampleTriggerStep"
            ),
        },
        serialize=lambda document: json.dumps(document),
    )

    yaml_bindings = load_plugin_bindings(yaml_package)
    json_bindings = load_plugin_bindings(json_package)

    assert yaml_bindings.step_identities == json_bindings.step_identities
    for identity in yaml_bindings.step_identities:
        yaml_descriptor = yaml_bindings.binding(identity).descriptor
        json_descriptor = json_bindings.binding(identity).descriptor
        assert yaml_descriptor.schema_digest("input") == json_descriptor.schema_digest(
            "input"
        )
        assert yaml_descriptor.schema_digest("output") == json_descriptor.schema_digest(
            "output"
        )


def test_repeated_loads_return_fresh_bindings(plugin_root: Path) -> None:
    package = _standard_plugin(plugin_root)

    first = load_plugin_bindings(package)
    second = load_plugin_bindings(package)

    assert first is not second
    assert first.step_identities == second.step_identities
    assert first.resolve("sample.demo.create-ticket") is not second.resolve(
        "sample.demo.create-ticket"
    )


def test_missing_plugin_manifest(plugin_root: Path) -> None:
    package = _write_plugin(
        plugin_root,
        plugin=_plugin_document(["steps/create-ticket.yaml"]),
        manifests={"docs/README.md": "# Demo plugin\n"},
    )
    (plugin_root / package / "manifests" / "plugin.yaml").unlink()

    with pytest.raises(StepBindingError, match="plugin.yaml"):
        load_plugin_bindings(package)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{ not: closed", "not a valid YAML document"),
        ("name: a\nname: b\n", "duplicate key"),
        ("value: &base 1\ncopy: *base\n", "anchors or aliases"),
        ("", "must be a YAML mapping"),
        ("- one\n- two\n", "must be a YAML mapping"),
    ],
)
def test_malformed_plugin_manifest(
    plugin_root: Path, content: str, message: str
) -> None:
    package = _write_plugin(
        plugin_root,
        plugin=content,
        manifests={"docs/README.md": "# Demo plugin\n"},
    )

    with pytest.raises(StepBindingError, match=message):
        load_plugin_bindings(package)


def test_contract_invalid_plugin_manifest(plugin_root: Path) -> None:
    package = _write_plugin(
        plugin_root,
        plugin={
            "apiVersion": "syntara.io/v1alpha1",
            "kind": "Plugin",
            "metadata": {
                "namespace": "sample",
                "name": "demo",
                "version": "0.1.0",
                "displayName": "Demo",
                "description": "Demo plugin used by the wrapper tests.",
            },
            "spec": {"documentation": {"path": "docs/README.md"}},
        },
        manifests={"docs/README.md": "# Demo plugin\n"},
    )

    with pytest.raises(StepBindingError, match="violates its contract"):
        load_plugin_bindings(package)


def test_missing_target_manifest(plugin_root: Path) -> None:
    package = _write_plugin(
        plugin_root,
        plugin=_plugin_document(["steps/missing.yaml", "steps/create-ticket.yaml"]),
        manifests=lambda package_name: {
            "docs/README.md": "# Demo plugin\n",
            "schemas/action-input.json": _ACTION_INPUT_SCHEMA,
            "steps/create-ticket.yaml": _action_document(
                f"{package_name}.steps:SampleActionStep"
            ),
        },
    )

    with pytest.raises(StepBindingError, match="steps/missing.yaml"):
        load_plugin_bindings(package)


def test_missing_asset_document(plugin_root: Path) -> None:
    package = _write_plugin(
        plugin_root,
        plugin=_plugin_document(["steps/create-ticket.yaml"]),
        manifests=lambda package_name: {
            "docs/README.md": "# Demo plugin\n",
            "steps/create-ticket.yaml": _action_document(
                f"{package_name}.steps:SampleActionStep"
            ),
        },
    )

    with pytest.raises(StepBindingError, match="schemas/action-input.json"):
        load_plugin_bindings(package)


@pytest.mark.parametrize(
    ("entry_point", "message"),
    [
        ("wrapper_pkg_missing.steps:SampleActionStep", "could not import"),
        ("NoColonHere", "entryPoint"),
        ("module_only", "entryPoint"),
        ("steps.SampleActionStep", "entryPoint"),
    ],
)
def test_unusable_entry_point(plugin_root: Path, entry_point: str, message: str) -> None:
    package = _write_plugin(
        plugin_root,
        plugin=_plugin_document(["steps/create-ticket.yaml"]),
        manifests=lambda package_name: {
            "docs/README.md": "# Demo plugin\n",
            "schemas/action-input.json": _ACTION_INPUT_SCHEMA,
            "steps/create-ticket.yaml": _action_document(entry_point),
        },
    )

    with pytest.raises(StepBindingError, match=message):
        load_plugin_bindings(package)


def test_entry_point_without_the_declared_class(plugin_root: Path) -> None:
    package = _write_plugin(
        plugin_root,
        plugin=_plugin_document(["steps/create-ticket.yaml"]),
        manifests=lambda package_name: {
            "docs/README.md": "# Demo plugin\n",
            "schemas/action-input.json": _ACTION_INPUT_SCHEMA,
            "steps/create-ticket.yaml": _action_document(
                f"{package_name}.steps:NoSuchStep"
            ),
        },
    )

    with pytest.raises(StepBindingError, match="has no attribute"):
        load_plugin_bindings(package)


def test_entry_point_that_is_not_a_base_step(plugin_root: Path) -> None:
    package = _write_plugin(
        plugin_root,
        plugin=_plugin_document(["steps/create-ticket.yaml"]),
        manifests=lambda package_name: {
            "docs/README.md": "# Demo plugin\n",
            "schemas/action-input.json": _ACTION_INPUT_SCHEMA,
            "steps/create-ticket.yaml": _action_document(f"{package_name}.steps:NotAStep"),
        },
    )

    with pytest.raises(StepBindingError, match="must name a BaseStep subclass"):
        load_plugin_bindings(package)


def test_kind_mismatch_between_manifest_and_implementation(plugin_root: Path) -> None:
    package = _write_plugin(
        plugin_root,
        plugin=_plugin_document(["steps/create-ticket.yaml"]),
        manifests=lambda package_name: {
            "docs/README.md": "# Demo plugin\n",
            "schemas/action-input.json": _ACTION_INPUT_SCHEMA,
            "steps/create-ticket.yaml": _action_document(
                f"{package_name}.steps:WrongKindStep"
            ),
        },
    )

    with pytest.raises(StepBindingError, match="does not match"):
        load_plugin_bindings(package)


def test_duplicate_step_identity(plugin_root: Path) -> None:
    def manifests(package_name: str) -> dict[str, Any]:
        entry_point = f"{package_name}.steps:SampleActionStep"
        return {
            "docs/README.md": "# Demo plugin\n",
            "schemas/action-input.json": _ACTION_INPUT_SCHEMA,
            "steps/create-ticket.yaml": _action_document(entry_point),
            "steps/create-ticket-copy.yaml": _action_document(
                entry_point, name="create-ticket"
            ),
        }

    package = _write_plugin(
        plugin_root,
        plugin=_plugin_document(["steps/create-ticket.yaml", "steps/create-ticket-copy.yaml"]),
        manifests=manifests,
    )

    with pytest.raises(StepBindingError, match="twice"):
        load_plugin_bindings(package)


def test_plugin_without_custom_runner_targets(plugin_root: Path) -> None:
    package = _write_plugin(
        plugin_root,
        plugin=_plugin_document(["steps/fetch-http.yaml", "steps/tracker.yaml"]),
        manifests=lambda package_name: {
            "docs/README.md": "# Demo plugin\n",
            "steps/fetch-http.yaml": _platform_action_document(),
            "steps/tracker.yaml": _integration_document(),
            "steps/credential.yaml": _credential_recipe_document(),
        },
    )

    with pytest.raises(StepBindingError, match="declares no custom-runner"):
        load_plugin_bindings(package)


def test_credential_recipe_targets_are_skipped(plugin_root: Path) -> None:
    package = _write_plugin(
        plugin_root,
        plugin=_plugin_document(["steps/credential.yaml", "steps/create-ticket.yaml"]),
        manifests=lambda package_name: {
            "docs/README.md": "# Demo plugin\n",
            "schemas/action-input.json": _ACTION_INPUT_SCHEMA,
            "steps/credential.yaml": _credential_recipe_document(),
            "steps/create-ticket.yaml": _action_document(
                f"{package_name}.steps:SampleActionStep"
            ),
        },
    )

    bindings = load_plugin_bindings(package)

    assert bindings.step_identities == ("sample.demo.create-ticket",)


def test_manifests_root_override(plugin_root: Path) -> None:
    package = _write_plugin(
        plugin_root,
        plugin=_plugin_document(["steps/create-ticket.yaml"]),
        manifests=lambda package_name: {
            "docs/README.md": "# Demo plugin\n",
            "schemas/action-input.json": _ACTION_INPUT_SCHEMA,
            "steps/create-ticket.yaml": _action_document(
                f"{package_name}.steps:SampleActionStep"
            ),
        },
        manifests_root="packaged-plugin",
    )

    bindings = load_plugin_bindings(package, manifests_root="packaged-plugin")

    assert bindings.step_identities == ("sample.demo.create-ticket",)
    with pytest.raises(StepBindingError, match="plugin.yaml"):
        load_plugin_bindings(package)


def test_non_json_yaml_values_are_rejected(plugin_root: Path) -> None:
    plugin = "apiVersion: syntara.io/v1alpha1\nkind: Plugin\nmetadata:\n"
    plugin += "  namespace: sample\n  name: demo\n  version: 0.1.0\n"
    plugin += "  displayName: Demo\n  description: Demo plugin.\n"
    plugin += "  released: 2026-01-30\n"
    package = _write_plugin(
        plugin_root,
        plugin=plugin,
        manifests={"docs/README.md": "# Demo plugin\n"},
    )

    with pytest.raises(StepBindingError, match="date"):
        load_plugin_bindings(package)


def test_non_string_mapping_keys_are_rejected(plugin_root: Path) -> None:
    package = _write_plugin(
        plugin_root,
        plugin="1: value\n",
        manifests={"docs/README.md": "# Demo plugin\n"},
    )

    with pytest.raises(StepBindingError, match="non-string mapping key"):
        load_plugin_bindings(package)


def test_complex_mapping_keys_are_rejected(plugin_root: Path) -> None:
    package = _write_plugin(
        plugin_root,
        plugin="? [a, b]\n: value\n",
        manifests={"docs/README.md": "# Demo plugin\n"},
    )

    with pytest.raises(StepBindingError, match="unhashable mapping key"):
        load_plugin_bindings(package)


def test_deeply_nested_document_is_rejected(plugin_root: Path) -> None:
    deep = "[" * 2000 + "1" + "]" * 2000
    package = _write_plugin(
        plugin_root,
        plugin=deep,
        manifests={"docs/README.md": "# Demo plugin\n"},
    )

    # Without the recursion guard, yaml.load itself raises RecursionError
    # before the document depth limit can fire.
    with pytest.raises(StepBindingError):
        load_plugin_bindings(package)


def test_unknown_target_kind_is_rejected(plugin_root: Path) -> None:
    package = _write_plugin(
        plugin_root,
        plugin=_plugin_document(["steps/workflow.yaml"]),
        manifests=lambda package_name: {
            "docs/README.md": "# Demo plugin\n",
            "steps/workflow.yaml": {
                "apiVersion": "syntara.io/v1alpha1",
                "kind": "Workflow",
                "metadata": {"name": "workflow"},
                "spec": {},
            },
        },
    )

    with pytest.raises(StepBindingError, match="unsupported kind"):
        load_plugin_bindings(package)


@pytest.mark.parametrize(
    "asset_path",
    ["../outside.json", "/etc/passwd", ".hidden.json", "schemas/../outside.json"],
)
def test_unsafe_asset_paths_are_rejected(plugin_root: Path, asset_path: str) -> None:
    package = _write_plugin(
        plugin_root,
        plugin=_plugin_document(["steps/create-ticket.yaml"]),
        manifests=lambda package_name: {
            "docs/README.md": "# Demo plugin\n",
            "steps/create-ticket.yaml": _action_document(
                f"{package_name}.steps:SampleActionStep",
                input_source={"kind": "asset", "path": asset_path},
            ),
        },
    )

    with pytest.raises(StepBindingError):
        load_plugin_bindings(package)
