"""Tests for the transport-independent typed step bindings."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel
from syntara_plugin.contracts import load_bundle, validate_document
from syntara_plugin.runtime import (
    ActionStep,
    BaseStep,
    ControlStep,
    ExecutionContext,
    InstalledStepManifest,
    StepBinding,
    StepBindingError,
    StepBindings,
    StepCategory,
    StepDescriptor,
    StepExecutor,
    StepKind,
    TriggerStep,
    to_json_document,
)


class EchoInput(BaseModel):
    message: str


class EchoOutput(BaseModel):
    message: str
    invocation_id: str


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _make_step(base: type[BaseStep[Any, Any]]) -> BaseStep[EchoInput, EchoOutput]:
    class Step(base[EchoInput, EchoOutput]):  # type: ignore[valid-type]
        def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
            return EchoOutput(message=inputs.message, invocation_id=context.invocation_id)

    if issubclass(base, ActionStep):
        Step.__module__ = "steps"
        Step.__qualname__ = "Echo"
    elif issubclass(base, ControlStep):
        Step.__module__ = "steps"
        Step.__qualname__ = "ControlEcho"
    else:
        Step.__module__ = "steps"
        Step.__qualname__ = "TriggerEcho"
    # Synthetic classes need a resolvable module binding because installed
    # entry points are verified against the exact loaded class, not just the
    # class's mutable module and qualname strings.
    module = sys.modules.setdefault(Step.__module__, ModuleType(Step.__module__))
    setattr(module, Step.__qualname__, Step)
    return Step()


def _manifest(
    input_schema: dict[str, object],
    output_schema: dict[str, object] | None = None,
    *,
    kind: str = "Action",
    entry_point: str = "steps:Echo",
) -> dict[str, object]:
    specification: dict[str, object] = {
        "runtime": {"kind": "custom-runner", "entryPoint": entry_point},
        "input": {"kind": "inline", "value": input_schema},
        "output": {
            "kind": "inline",
            "value": output_schema if output_schema is not None else {"type": "object"},
        },
    }
    if kind == "Control":
        specification["invocation"] = {"kind": "orchestration"}
    elif kind == "Trigger":
        specification.update(
            {
                "driver": "webhook.v1",
                "configuration": {"kind": "inline", "value": {"type": "object"}},
            }
        )
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": kind,
        "metadata": {
            "name": "echo",
            "displayName": "Echo",
            "description": "An echo Step type.",
        },
        "spec": specification,
    }


def _installed_step(
    input_schema: dict[str, object],
    output_schema: dict[str, object] | None = None,
    *,
    kind: str = "Action",
    entry_point: str = "steps:Echo",
) -> InstalledStepManifest:
    return InstalledStepManifest.from_manifest(
        "acme.plugin.echo",
        _manifest(input_schema, output_schema, kind=kind, entry_point=entry_point),
        {},
    )


@pytest.mark.parametrize(
    ("base", "kind", "category"),
    [
        (ActionStep, StepKind.ACTION, StepCategory.ACTION),
        (ControlStep, StepKind.CONTROL, StepCategory.CONTROL),
        (TriggerStep, StepKind.TRIGGER, StepCategory.TRIGGER),
    ],
)
def test_bind_and_resolve_all_step_categories(
    base: type[BaseStep[Any, Any]], kind: StepKind, category: StepCategory
) -> None:
    bindings = StepBindings()
    step = _make_step(base)
    descriptor = (
        StepDescriptor.for_installed_step(step, _installed_step(EchoInput.model_json_schema()))
        if kind is StepKind.ACTION
        else StepDescriptor.for_step("acme.plugin.echo", step)
    )

    assert descriptor.kind is kind
    assert descriptor.category is category
    bindings.bind(descriptor, step)
    assert bindings.resolve("acme.plugin.echo") is step
    assert bindings.step_identities == ("acme.plugin.echo",)


def test_binding_retains_the_descriptor_and_implementation() -> None:
    bindings = StepBindings()
    step = _make_step(ControlStep)
    descriptor = StepDescriptor.for_step("acme.plugin.echo", step)
    bindings.bind(descriptor, step)

    binding = bindings.binding("acme.plugin.echo")

    assert binding.descriptor is descriptor
    assert binding.step is step
    assert bindings.resolve("acme.plugin.echo") is step


def test_descriptor_for_step_derives_pydantic_schemas() -> None:
    step = _make_step(ControlStep)
    descriptor = StepDescriptor.for_step("acme.plugin.echo", step)

    assert _plain_json(descriptor.input_schema) == EchoInput.model_json_schema()
    assert _plain_json(descriptor.output_schema) == EchoOutput.model_json_schema()


def test_installed_step_descriptor_uses_the_manifest_schema() -> None:
    step = _make_step(ActionStep)
    manifest_schema = {
        "type": "object",
        "required": ["message"],
        "properties": {"message": {"type": "string"}},
        "additionalProperties": False,
    }

    output_schema = {
        "type": "object",
        "required": ["message"],
        "properties": {"message": {"type": "string"}},
        "additionalProperties": False,
    }

    installed_step = _installed_step(manifest_schema, output_schema)
    descriptor = StepDescriptor.for_installed_step(step, installed_step)

    assert _plain_json(descriptor.input_schema) == manifest_schema
    assert descriptor.input_schema_source == "manifest"
    assert _plain_json(installed_step.output_schema) == output_schema
    assert _plain_json(descriptor.output_schema) == output_schema


def test_installed_step_rejects_a_tampered_validation_mode() -> None:
    step = _make_step(ActionStep)
    descriptor = StepDescriptor.for_installed_step(
        step, _installed_step(EchoInput.model_json_schema(), EchoOutput.model_json_schema())
    )
    object.__setattr__(descriptor, "input_schema_source", "implementation")

    with pytest.raises(StepBindingError, match="installed contract"):
        StepExecutor(StepBinding(descriptor, step))


def test_installed_step_rejects_a_mismatched_implementation_before_execution() -> None:
    class MismatchedAction(ActionStep[EchoInput, EchoOutput]):
        def __init__(self) -> None:
            self.run_calls = 0

        def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
            self.run_calls += 1
            return EchoOutput(message=inputs.message, invocation_id=context.invocation_id)

    installed_step = _installed_step(
        EchoInput.model_json_schema(), EchoOutput.model_json_schema()
    )
    matching_step = _make_step(ActionStep)
    descriptor = StepDescriptor.for_installed_step(matching_step, installed_step)
    mismatched_step = MismatchedAction()
    mismatched_binding = StepBinding(descriptor, mismatched_step)

    with pytest.raises(StepBindingError, match="entry point"):
        StepDescriptor.for_installed_step(mismatched_step, installed_step)

    with pytest.raises(StepBindingError, match="entry point"):
        StepExecutor(mismatched_binding)

    assert mismatched_step.run_calls == 0


@pytest.mark.parametrize(
    ("base", "kind", "installed_entry_point"),
    [
        (ActionStep, "Action", "steps:ControlEcho"),
        (ControlStep, "Control", "steps:Echo"),
        (TriggerStep, "Trigger", "steps:Echo"),
    ],
    ids=["action", "control", "trigger"],
)
def test_installed_step_rejects_entry_point_mismatch_for_every_kind(
    base: type[BaseStep[Any, Any]], kind: str, installed_entry_point: str
) -> None:
    class MismatchedStep(base[EchoInput, EchoOutput]):  # type: ignore[valid-type]
        def __init__(self) -> None:
            self.run_calls = 0

        def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
            self.run_calls += 1
            return EchoOutput(message=inputs.message, invocation_id=context.invocation_id)

    step = MismatchedStep()
    installed_step = _installed_step(
        EchoInput.model_json_schema(),
        EchoOutput.model_json_schema(),
        kind=kind,
        entry_point=installed_entry_point,
    )

    with pytest.raises(StepBindingError, match="entry point"):
        StepDescriptor.for_installed_step(step, installed_step)

    assert step.run_calls == 0


def test_installed_step_accepts_a_loaded_reexported_entry_point() -> None:
    step = _make_step(ActionStep)
    module = ModuleType("acme_reexport")
    module.Echo = type(step)
    sys.modules["acme_reexport"] = module
    try:
        manifest = _manifest(EchoInput.model_json_schema())
        manifest["spec"]["runtime"]["entryPoint"] = "acme_reexport:Echo"
        installed_step = InstalledStepManifest.from_manifest(
            "acme.plugin.echo", manifest, {}
        )

        descriptor = StepDescriptor.for_installed_step(step, installed_step)

        assert descriptor.input_schema_source == "manifest"
    finally:
        del sys.modules["acme_reexport"]


def test_installed_step_accepts_a_loaded_current_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    step = _make_step(ActionStep)
    module = ModuleType("steps")
    module.Echo = type(step)
    monkeypatch.setitem(sys.modules, "steps", module)

    installed_step = _installed_step(EchoInput.model_json_schema())

    descriptor = StepDescriptor.for_installed_step(step, installed_step)

    assert descriptor.input_schema_source == "manifest"


def test_installed_step_rejects_matching_entry_point_when_module_is_unloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    step = _make_step(ActionStep)
    monkeypatch.delitem(sys.modules, "steps", raising=False)

    installed_step = _installed_step(EchoInput.model_json_schema())

    with pytest.raises(StepBindingError, match="entry point"):
        StepDescriptor.for_installed_step(step, installed_step)


def test_installed_step_rejects_unresolved_entry_point_in_loaded_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    step = _make_step(ActionStep)
    monkeypatch.setitem(sys.modules, "steps", ModuleType("steps"))
    installed_step = _installed_step(EchoInput.model_json_schema())

    with pytest.raises(StepBindingError, match="entry point"):
        StepDescriptor.for_installed_step(step, installed_step)


def test_installed_step_rejects_a_stale_class_after_module_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_step = _make_step(ActionStep)

    class ReloadedEcho(ActionStep[EchoInput, EchoOutput]):
        def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
            return EchoOutput(message=inputs.message, invocation_id=context.invocation_id)

    ReloadedEcho.__module__ = "steps"
    ReloadedEcho.__qualname__ = "Echo"
    module = ModuleType("steps")
    module.Echo = ReloadedEcho
    monkeypatch.setitem(sys.modules, "steps", module)
    installed_step = _installed_step(EchoInput.model_json_schema())

    with pytest.raises(StepBindingError, match="entry point"):
        StepDescriptor.for_installed_step(stale_step, installed_step)


def test_immutable_documents_have_a_supported_json_export() -> None:
    installed_step = _installed_step(EchoInput.model_json_schema())

    exported = to_json_document(installed_step.document)

    assert json.loads(json.dumps(exported)) == _plain_json(installed_step.document)


def test_descriptor_rejects_mutable_non_json_schema_values() -> None:
    with pytest.raises(TypeError, match="JSON-compatible"):
        StepDescriptor(
            step_identity="acme.plugin.echo",
            kind=StepKind.CONTROL,
            input_schema={"type": "object", "x-value": bytearray(b"secret")},
            output_schema={"type": "object"},
        )


@pytest.mark.parametrize(
    ("kind", "entry_point"),
    [
        ("Action", "steps:Echo"),
        ("Control", "steps:ControlEcho"),
        ("Trigger", "steps:TriggerEcho"),
    ],
    ids=["action", "control", "trigger"],
)
def test_installed_platform_runner_cannot_be_bound_to_python(
    kind: str, entry_point: str
) -> None:
    manifest = _manifest(
        EchoInput.model_json_schema(), kind=kind, entry_point=entry_point
    )
    manifest["spec"]["runtime"] = {
        "kind": "platform-runner",
        "driver": "transform.v1",
    }

    with pytest.raises(StepBindingError, match="custom-runner"):
        InstalledStepManifest.from_manifest("acme.plugin.echo", manifest, {})


@pytest.mark.parametrize(
    "entry_point",
    [
        "",
        ":Echo",
        ".steps:Echo",
        "steps.:Echo",
        "steps..api:Echo",
        "steps:Echo.More",
        "steps:Echo\n",
    ],
)
def test_installed_step_rejects_malformed_entry_points(entry_point: str) -> None:
    with pytest.raises(StepBindingError, match="manifest violates its contract"):
        _installed_step(EchoInput.model_json_schema(), entry_point=entry_point)


@pytest.mark.parametrize(
    ("kind", "missing_field"),
    [
        ("Control", "metadata"),
        ("Control", "invocation"),
        ("Trigger", "driver"),
        ("Trigger", "configuration"),
    ],
)
def test_installed_step_rejects_each_missing_category_field(
    kind: str, missing_field: str
) -> None:
    manifest = _manifest(EchoInput.model_json_schema(), kind=kind)
    if missing_field == "metadata":
        del manifest["metadata"]
    else:
        del manifest["spec"][missing_field]

    errors = validate_document(load_bundle(), kind.lower(), manifest)
    assert any(missing_field in error for error in errors)
    with pytest.raises(StepBindingError, match=missing_field):
        InstalledStepManifest.from_manifest("acme.plugin.echo", manifest, {})


@pytest.mark.parametrize("kind", ["Control", "Trigger"])
def test_installed_step_manifest_rejects_unknown_category_fields(kind: str) -> None:
    manifest = _manifest(EchoInput.model_json_schema(), kind=kind)
    manifest["spec"]["unexpected"] = True

    errors = validate_document(load_bundle(), kind.lower(), manifest)
    assert any("unexpected" in error for error in errors)
    with pytest.raises(StepBindingError, match="unexpected"):
        InstalledStepManifest.from_manifest("acme.plugin.echo", manifest, {})


def test_installed_step_manifest_resolves_an_asset_backed_schema() -> None:
    input_schema = {
        "type": "object",
        "required": ["message"],
        "properties": {"message": {"type": "string"}},
    }
    manifest = _manifest({})
    manifest["spec"]["input"] = {"kind": "asset", "path": "schemas/input.json"}

    installed_step = InstalledStepManifest.from_manifest(
        "acme.plugin.echo",
        manifest,
        {"schemas/input.json": input_schema},
    )

    assert _plain_json(installed_step.input_schema) == input_schema
    assert (
        _plain_json(
            StepDescriptor.for_installed_step(_make_step(ActionStep), installed_step).input_schema
        )
        == input_schema
    )


def test_installed_step_manifests_are_created_by_the_manifest_factory() -> None:
    input_schema = EchoInput.model_json_schema()
    manifest = _manifest(input_schema)

    with pytest.raises(TypeError):
        InstalledStepManifest(  # type: ignore[call-arg]
            "acme.plugin.echo",
            manifest,
            input_schema,
        )


@pytest.mark.parametrize(
    ("base", "kind", "entry_point"),
    [
        (ActionStep, "Action", "steps:Echo"),
        (ControlStep, "Control", "steps:ControlEcho"),
        (TriggerStep, "Trigger", "steps:TriggerEcho"),
    ],
    ids=["action", "control", "trigger"],
)
def test_installed_step_documents_and_schemas_are_immutable(
    base: type[BaseStep[Any, Any]], kind: str, entry_point: str
) -> None:
    input_schema = {
        "type": "object",
        "required": ["message"],
        "properties": {"message": {"type": "string"}},
    }
    output_schema = {
        "type": "object",
        "required": ["message"],
        "properties": {"message": {"type": "string"}},
    }
    installed_step = InstalledStepManifest.from_manifest(
        "acme.plugin.echo",
        _manifest(input_schema, output_schema, kind=kind, entry_point=entry_point),
        {},
    )
    descriptor = StepDescriptor.for_installed_step(_make_step(base), installed_step)

    input_snapshot = _plain_json(descriptor.input_schema)
    output_snapshot = _plain_json(descriptor.output_schema)
    document_snapshot = _plain_json(installed_step.document)

    def assert_unchanged() -> None:
        assert _plain_json(descriptor.input_schema) == input_snapshot
        assert _plain_json(descriptor.output_schema) == output_snapshot
        assert _plain_json(installed_step.document) == document_snapshot

    with pytest.raises(TypeError):
        installed_step.input_schema["type"] = "array"
    assert_unchanged()
    with pytest.raises(TypeError):
        installed_step.input_schema["required"][0] = "changed"
    assert_unchanged()
    with pytest.raises(TypeError):
        descriptor.input_schema["required"][0] = "changed"
    assert_unchanged()
    with pytest.raises(TypeError):
        installed_step.document["spec"]["input"]["value"] = {}
    assert_unchanged()
    with pytest.raises(TypeError):
        descriptor.input_schema |= {"title": "changed"}
    assert_unchanged()
    with pytest.raises(TypeError):
        descriptor.input_schema["properties"] |= {"other": {"type": "string"}}
    assert_unchanged()
    with pytest.raises(TypeError):
        descriptor.input_schema["required"] += ["other"]
    assert_unchanged()
    with pytest.raises(TypeError):
        descriptor.input_schema["required"] *= 2
    assert_unchanged()
    with pytest.raises(TypeError):
        descriptor.output_schema |= {"title": "changed"}
    assert_unchanged()
    with pytest.raises(TypeError):
        descriptor.output_schema["required"] += ["other"]
    assert_unchanged()
    with pytest.raises(TypeError):
        descriptor.output_schema["required"] *= 2
    assert_unchanged()

    with pytest.raises(TypeError):
        dict.__setitem__(descriptor.input_schema, "title", "changed")
    assert_unchanged()
    with pytest.raises(TypeError):
        dict.__ior__(descriptor.input_schema, {"title": "changed"})
    assert_unchanged()
    with pytest.raises(TypeError):
        list.__iadd__(descriptor.input_schema["required"], ["other"])
    assert_unchanged()


def test_base_mapping_mutation_is_rejected() -> None:
    step = _make_step(ActionStep)
    installed_step = _installed_step(EchoInput.model_json_schema())
    descriptor = StepDescriptor.for_installed_step(step, installed_step)

    with pytest.raises(TypeError):
        dict.__setitem__(descriptor.input_schema, "required", [])


def test_base_list_mutation_is_rejected() -> None:
    step = _make_step(ActionStep)
    installed_step = _installed_step(EchoInput.model_json_schema())
    descriptor = StepDescriptor.for_installed_step(step, installed_step)

    with pytest.raises(TypeError):
        list.__setitem__(descriptor.input_schema["required"], 0, "changed")


def test_installed_schema_cannot_be_mutated() -> None:
    installed_step = _installed_step(EchoInput.model_json_schema())
    with pytest.raises(TypeError):
        dict.__setitem__(installed_step.input_schema, "required", [])


def test_installed_step_entry_point_cannot_be_mutated() -> None:
    installed_step = _installed_step(EchoInput.model_json_schema())
    runtime = installed_step.document["spec"]["runtime"]
    assert isinstance(runtime, Mapping)

    with pytest.raises(TypeError):
        dict.__setitem__(runtime, "entryPoint", "steps:Other")


def test_descriptor_schema_freezes_mutable_values_nested_in_tuples() -> None:
    descriptor = StepDescriptor.for_installed_step(
        _make_step(ActionStep),
        _installed_step(
            {
                "type": "object",
                "x-syntara-nested": ({"values": ["original"]},),
            }
        ),
    )

    schema_snapshot = _plain_json(descriptor.input_schema)

    assert isinstance(descriptor.input_schema["x-syntara-nested"], tuple)
    with pytest.raises(TypeError):
        descriptor.input_schema["x-syntara-nested"][0]["values"][0] = "changed"

    assert _plain_json(descriptor.input_schema) == schema_snapshot


def test_every_descriptor_schema_is_deeply_immutable() -> None:
    descriptor = StepDescriptor.for_step("acme.plugin.echo", _make_step(ControlStep))
    input_snapshot = _plain_json(descriptor.input_schema)
    output_snapshot = _plain_json(descriptor.output_schema)

    def assert_unchanged() -> None:
        assert _plain_json(descriptor.input_schema) == input_snapshot
        assert _plain_json(descriptor.output_schema) == output_snapshot

    with pytest.raises(TypeError):
        descriptor.input_schema |= {"title": "changed"}
    assert_unchanged()
    with pytest.raises(TypeError):
        descriptor.input_schema["required"] += ["other"]
    assert_unchanged()
    with pytest.raises(TypeError):
        descriptor.input_schema["required"] *= 2
    assert_unchanged()
    with pytest.raises(TypeError):
        descriptor.output_schema["properties"] |= {"other": {"type": "string"}}
    assert_unchanged()
    with pytest.raises(TypeError):
        descriptor.output_schema["required"] += ["other"]
    assert_unchanged()
    with pytest.raises(TypeError):
        descriptor.output_schema["required"] *= 2
    assert_unchanged()


def test_installed_step_descriptor_rejects_a_mismatched_step_kind() -> None:
    step = _make_step(ControlStep)
    installed_step = _installed_step(EchoInput.model_json_schema())

    with pytest.raises(StepBindingError, match="manifest kind 'Action'"):
        StepDescriptor.for_installed_step(step, installed_step)


@pytest.mark.parametrize(
    ("base", "kind", "entry_point"),
    [
        (ControlStep, "Control", "steps:ControlEcho"),
        (TriggerStep, "Trigger", "steps:TriggerEcho"),
    ],
)
def test_installed_step_descriptor_supports_non_action_custom_runners(
    base: type[BaseStep[Any, Any]], kind: str, entry_point: str
) -> None:
    step = _make_step(base)
    installed_step = _installed_step(
        EchoInput.model_json_schema(),
        kind=kind,
        entry_point=entry_point,
    )

    descriptor = StepDescriptor.for_installed_step(step, installed_step)

    assert descriptor.kind is StepKind(kind)
    assert descriptor.input_schema_source == "manifest"
    assert descriptor.is_installed_step


@pytest.mark.parametrize(
    ("schema_name", "invalid_schema"),
    [
        ("input_schema", {"type": "invalid"}),
        ("output_schema", {"type": "invalid"}),
        (
            "input_schema",
            {"type": "object", "properties": {"message": {"$ref": "#/$defs/missing"}}},
        ),
        (
            "output_schema",
            {"type": "object", "properties": {"message": {"$ref": "#/$defs/missing"}}},
        ),
    ],
    ids=["invalid-input-schema", "invalid-output-schema", "input-reference", "output-reference"],
)
def test_installed_step_descriptor_identifies_the_invalid_manifest_schema(
    schema_name: str, invalid_schema: dict[str, object]
) -> None:
    step = _make_step(ActionStep)
    input_schema = (
        invalid_schema if schema_name == "input_schema" else EchoInput.model_json_schema()
    )
    output_schema = (
        invalid_schema if schema_name == "output_schema" else EchoOutput.model_json_schema()
    )
    installed_step = _installed_step(input_schema, output_schema)

    with pytest.raises(StepBindingError, match=f"manifest {schema_name}"):
        StepDescriptor.for_installed_step(step, installed_step)


def test_installed_step_descriptor_accepts_resolvable_schema_references() -> None:
    step = _make_step(ActionStep)
    input_schema = {
        "type": "object",
        "properties": {"message": {"$ref": "#/$defs/message"}},
        "$defs": {"message": {"type": "string"}},
    }

    descriptor = StepDescriptor.for_installed_step(
        step,
        _installed_step(input_schema),
    )

    assert _plain_json(descriptor.input_schema) == input_schema


def test_installed_step_descriptor_rejects_a_dangling_schema_reference() -> None:
    step = _make_step(ActionStep)
    input_schema = {
        "type": "object",
        "properties": {"message": {"$ref": "#/$defs/missing"}},
    }

    installed_step = _installed_step(input_schema)

    with pytest.raises(StepBindingError, match=r"unresolvable \$ref: #/\$defs/missing"):
        StepDescriptor.for_installed_step(step, installed_step)


def test_installed_step_descriptor_rejects_nested_dangling_references() -> None:
    step = _make_step(ActionStep)
    input_schema = {
        "type": "object",
        "properties": {"message": {"$ref": "#/$defs/message"}},
        "$defs": {
            "message": {
                "type": "object",
                "properties": {"value": {"$ref": "#/$defs/missing"}},
            }
        },
    }

    installed_step = _installed_step(input_schema)

    with pytest.raises(StepBindingError, match=r"unresolvable \$ref: #/\$defs/missing"):
        StepDescriptor.for_installed_step(step, installed_step)


def test_resolved_step_executes_directly_without_container_invocation() -> None:
    bindings = StepBindings()
    step = _make_step(ControlStep)
    bindings.bind(StepDescriptor.for_step("acme.plugin.echo", step), step)

    context = ExecutionContext(invocation_id="invoke-1")
    output = bindings.resolve("acme.plugin.echo").execute(EchoInput(message="hello"), context)

    assert output == EchoOutput(message="hello", invocation_id="invoke-1")


def test_duplicate_identity_is_rejected() -> None:
    bindings = StepBindings()
    step = _make_step(ControlStep)
    descriptor = StepDescriptor.for_step("acme.plugin.echo", step)
    bindings.bind(descriptor, step)

    with pytest.raises(StepBindingError, match="already bound"):
        bindings.bind(descriptor, step)


def test_malformed_identity_is_rejected() -> None:
    input_schema = EchoInput.model_json_schema()
    output_schema = EchoOutput.model_json_schema()

    with pytest.raises(StepBindingError, match="step_identity"):
        StepDescriptor(
            step_identity="not a valid identity",
            kind=StepKind.ACTION,
            input_schema=input_schema,
            output_schema=output_schema,
        )


def test_kind_mismatch_is_rejected() -> None:
    bindings = StepBindings()
    step = _make_step(ControlStep)
    descriptor = StepDescriptor(
        step_identity="acme.plugin.echo",
        kind=StepKind.ACTION,
        input_schema=EchoInput.model_json_schema(),
        output_schema=EchoOutput.model_json_schema(),
    )

    with pytest.raises(StepBindingError, match="does not match"):
        bindings.bind(descriptor, step)


def test_kind_strings_are_normalized_and_category_is_derived() -> None:
    descriptor = StepDescriptor(
        step_identity="acme.plugin.echo",
        kind="Control",
        input_schema=EchoInput.model_json_schema(),
        output_schema=EchoOutput.model_json_schema(),
    )

    assert descriptor.kind is StepKind.CONTROL
    assert descriptor.category is StepCategory.CONTROL


def test_invalid_kind_is_rejected() -> None:
    input_schema = EchoInput.model_json_schema()
    output_schema = EchoOutput.model_json_schema()

    with pytest.raises(StepBindingError, match="valid concrete Step kind"):
        StepDescriptor(
            step_identity="acme.plugin.echo",
            kind="not-a-kind",
            input_schema=input_schema,
            output_schema=output_schema,
        )


def test_callers_cannot_mark_a_plain_action_descriptor_as_manifest_trusted() -> None:
    input_schema = EchoInput.model_json_schema()
    output_schema = EchoOutput.model_json_schema()

    with pytest.raises(StepBindingError, match="for_installed_step"):
        StepDescriptor(
            step_identity="acme.plugin.echo",
            kind=StepKind.ACTION,
            input_schema=input_schema,
            output_schema=output_schema,
            input_schema_source="manifest",
        )


def test_implementation_derived_action_descriptor_supports_authoring_validation() -> None:
    step = _make_step(ActionStep)
    descriptor = StepDescriptor.for_step("acme.plugin.echo", step)
    bindings = StepBindings()

    bindings.bind(descriptor, step)

    binding = bindings.binding("acme.plugin.echo")
    assert binding == StepBinding(descriptor, step)
    with pytest.raises(StepBindingError, match="installed contract"):
        StepExecutor(binding)


def test_binding_and_execution_reject_a_duck_typed_action_descriptor() -> None:
    step = _make_step(ActionStep)
    fake_descriptor = SimpleNamespace(
        step_identity="acme.plugin.echo",
        kind=StepKind.ACTION,
        input_schema={"type": "object"},
        output_schema=EchoOutput.model_json_schema(),
        require_executable_contract=lambda: None,
    )
    bindings = StepBindings()
    binding = StepBinding(fake_descriptor, step)  # type: ignore[arg-type]

    with pytest.raises(StepBindingError, match="StepDescriptor"):
        bindings.bind(fake_descriptor, step)  # type: ignore[arg-type]

    with pytest.raises(StepBindingError, match="StepDescriptor"):
        StepExecutor(binding)


def test_binding_and_execution_reject_an_action_descriptor_subclass() -> None:
    class ForgedDescriptor(StepDescriptor):
        def require_executable_contract(self) -> None:
            return None

    step = _make_step(ActionStep)
    forged_descriptor = ForgedDescriptor(
        step_identity="acme.plugin.echo",
        kind=StepKind.ACTION,
        input_schema={"type": "object"},
        output_schema=EchoOutput.model_json_schema(),
    )
    bindings = StepBindings()
    binding = StepBinding(forged_descriptor, step)

    with pytest.raises(StepBindingError, match="StepDescriptor"):
        bindings.bind(forged_descriptor, step)

    with pytest.raises(StepBindingError, match="StepDescriptor"):
        StepExecutor(binding)


@pytest.mark.parametrize(
    ("base", "kind"),
    [(ActionStep, StepKind.ACTION), (ControlStep, StepKind.CONTROL), (TriggerStep, StepKind.TRIGGER)],
    ids=["action", "control", "trigger"],
)
def test_installed_step_execution_requires_factory_provenance(
    base: type[BaseStep[Any, Any]], kind: StepKind
) -> None:
    step = _make_step(base)
    duck_typed_manifest = SimpleNamespace(
        step_identity="acme.plugin.echo",
        input_schema=EchoInput.model_json_schema(),
    )

    with pytest.raises(TypeError, match="InstalledStepManifest"):
        StepDescriptor.for_installed_step(
            step,
            duck_typed_manifest,  # type: ignore[arg-type]
        )

    forged_manifest = InstalledStepManifest()
    object.__setattr__(forged_manifest, "step_identity", "acme.plugin.echo")
    object.__setattr__(forged_manifest, "input_schema", EchoInput.model_json_schema())

    with pytest.raises(StepBindingError, match="verified installed manifest"):
        StepDescriptor.for_installed_step(step, forged_manifest)

    class ForgedManifest(InstalledStepManifest):
        pass

    forged_manifest_subclass = ForgedManifest()
    with pytest.raises(TypeError, match="InstalledStepManifest"):
        StepDescriptor.for_installed_step(step, forged_manifest_subclass)

    forged_descriptor = StepDescriptor(
        step_identity="acme.plugin.echo",
        kind=kind,
        input_schema=EchoInput.model_json_schema(),
        output_schema=EchoOutput.model_json_schema(),
    )
    bindings = StepBindings()

    bindings.bind(forged_descriptor, step)
    binding = StepBinding(forged_descriptor, step)

    with pytest.raises(StepBindingError, match="installed contract"):
        StepExecutor(binding)


def test_platform_managed_driver_is_rejected_as_a_python_implementation() -> None:
    bindings = StepBindings()
    descriptor = StepDescriptor(
        step_identity="acme.plugin.echo",
        kind=StepKind.ACTION,
        input_schema=EchoInput.model_json_schema(),
        output_schema=EchoOutput.model_json_schema(),
    )

    def http_driver(_invocation: Any) -> Any:
        return None

    with pytest.raises(StepBindingError, match="platform-managed"):
        bindings.bind(descriptor, http_driver)  # type: ignore[arg-type]


def test_resolve_unknown_identity_is_rejected() -> None:
    bindings = StepBindings()

    with pytest.raises(StepBindingError, match="not bound"):
        bindings.resolve("acme.plugin.missing")
