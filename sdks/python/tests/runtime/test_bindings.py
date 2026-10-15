"""Tests for the transport-independent typed step bindings."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel
from syntara_plugin.runtime import (
    ActionStep,
    BaseStep,
    ExecutionContext,
    InstalledActionManifest,
    StepBinding,
    StepBindingError,
    StepBindings,
    StepDescriptor,
    StepExecutor,
    StepTaxonomy,
    TaskStep,
    TriggerStep,
    WorkflowStep,
)


class EchoInput(BaseModel):
    message: str


class EchoOutput(BaseModel):
    message: str
    invocation_id: str


def _make_step(base: type[BaseStep[Any, Any]]) -> BaseStep[EchoInput, EchoOutput]:
    class Step(base[EchoInput, EchoOutput]):  # type: ignore[valid-type]
        def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
            return EchoOutput(message=inputs.message, invocation_id=context.invocation_id)

    return Step()


def _manifest(input_schema: dict[str, object]) -> dict[str, object]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Action",
        "spec": {"input": {"kind": "inline", "value": input_schema}},
    }


def _installed_action(
    input_schema: dict[str, object],
) -> InstalledActionManifest:
    return InstalledActionManifest.from_manifest(
        "acme.plugin.echo",
        _manifest(input_schema),
        {},
    )


@pytest.mark.parametrize(
    ("base", "taxonomy"),
    [
        (ActionStep, StepTaxonomy.ACTION),
        (TaskStep, StepTaxonomy.TASK),
        (WorkflowStep, StepTaxonomy.WORKFLOW),
        (TriggerStep, StepTaxonomy.TRIGGER),
    ],
)
def test_bind_and_resolve_all_four_taxonomies(
    base: type[BaseStep[Any, Any]], taxonomy: StepTaxonomy
) -> None:
    bindings = StepBindings()
    step = _make_step(base)
    descriptor = (
        StepDescriptor.for_installed_action(
            step, _installed_action(EchoInput.model_json_schema())
        )
        if taxonomy is StepTaxonomy.ACTION
        else StepDescriptor.for_step("acme.plugin.echo", step)
    )

    assert descriptor.taxonomy is taxonomy
    bindings.bind(descriptor, step)
    assert bindings.resolve("acme.plugin.echo") is step
    assert bindings.step_identities == ("acme.plugin.echo",)


def test_binding_retains_the_descriptor_and_implementation() -> None:
    bindings = StepBindings()
    step = _make_step(TaskStep)
    descriptor = StepDescriptor.for_step("acme.plugin.echo", step)
    bindings.bind(descriptor, step)

    binding = bindings.binding("acme.plugin.echo")

    assert binding.descriptor is descriptor
    assert binding.step is step
    assert bindings.resolve("acme.plugin.echo") is step


def test_descriptor_for_step_derives_pydantic_schemas() -> None:
    step = _make_step(TaskStep)
    descriptor = StepDescriptor.for_step("acme.plugin.echo", step)

    assert descriptor.input_schema == EchoInput.model_json_schema()
    assert descriptor.output_schema == EchoOutput.model_json_schema()


def test_installed_action_descriptor_uses_the_manifest_schema() -> None:
    step = _make_step(ActionStep)
    manifest_schema = {
        "type": "object",
        "required": ["message"],
        "properties": {"message": {"type": "string"}},
        "additionalProperties": False,
    }

    installed_action = _installed_action(manifest_schema)
    descriptor = StepDescriptor.for_installed_action(step, installed_action)

    assert descriptor.input_schema == manifest_schema
    assert descriptor.input_schema_source == "manifest"


def test_installed_action_manifest_resolves_an_asset_backed_schema() -> None:
    input_schema = {
        "type": "object",
        "required": ["message"],
        "properties": {"message": {"type": "string"}},
    }
    manifest = _manifest({})
    manifest["spec"]["input"] = {"kind": "asset", "path": "schemas/input.json"}

    installed_action = InstalledActionManifest.from_manifest(
        "acme.plugin.echo",
        manifest,
        {"schemas/input.json": input_schema},
    )

    assert installed_action.input_schema == input_schema
    assert (
        StepDescriptor.for_installed_action(_make_step(ActionStep), installed_action).input_schema
        == input_schema
    )


def test_installed_action_manifests_are_created_by_the_manifest_factory() -> None:
    input_schema = EchoInput.model_json_schema()
    manifest = _manifest(input_schema)

    with pytest.raises(TypeError):
        InstalledActionManifest(  # type: ignore[call-arg]
            "acme.plugin.echo",
            manifest,
            input_schema,
        )


def test_installed_action_documents_and_schemas_are_immutable() -> None:
    installed_action = _installed_action(
        {
            "type": "object",
            "required": ["message"],
            "properties": {"message": {"type": "string"}},
        }
    )
    descriptor = StepDescriptor.for_installed_action(
        _make_step(ActionStep), installed_action
    )

    input_snapshot = json.loads(json.dumps(descriptor.input_schema))
    output_snapshot = json.loads(json.dumps(descriptor.output_schema))
    document_snapshot = json.loads(json.dumps(installed_action.document))

    def assert_unchanged() -> None:
        assert descriptor.input_schema == input_snapshot
        assert descriptor.output_schema == output_snapshot
        assert installed_action.document == document_snapshot

    with pytest.raises(TypeError):
        installed_action.input_schema["type"] = "array"
    assert_unchanged()
    with pytest.raises(TypeError):
        installed_action.input_schema["required"][0] = "changed"
    assert_unchanged()
    with pytest.raises(TypeError):
        descriptor.input_schema["required"].append("changed")
    assert_unchanged()
    with pytest.raises(TypeError):
        installed_action.document["spec"]["input"]["value"] = {}
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


def test_mutation_through_base_mapping_methods_fails_closed_at_execution() -> None:
    step = _make_step(ActionStep)
    installed_action = _installed_action(EchoInput.model_json_schema())
    descriptor = StepDescriptor.for_installed_action(step, installed_action)

    dict.__setitem__(descriptor.input_schema, "required", [])

    with pytest.raises(StepBindingError, match="unchanged installed contract"):
        StepExecutor(StepBinding(descriptor, step))


def test_mutated_installed_schema_cannot_be_rebound() -> None:
    step = _make_step(ActionStep)
    installed_action = _installed_action(EchoInput.model_json_schema())
    dict.__setitem__(installed_action.input_schema, "required", [])

    with pytest.raises(StepBindingError, match="changed after installation"):
        StepDescriptor.for_installed_action(step, installed_action)


def test_descriptor_schema_freezes_mutable_values_nested_in_tuples() -> None:
    descriptor = StepDescriptor.for_installed_action(
        _make_step(ActionStep),
        _installed_action(
            {
                "type": "object",
                "x-syntara-nested": ({"values": ["original"]},),
            }
        ),
    )
    def snapshot(value: object) -> object:
        if isinstance(value, dict):
            return {key: snapshot(item) for key, item in value.items()}
        if isinstance(value, list):
            return [snapshot(item) for item in value]
        if isinstance(value, tuple):
            return tuple(snapshot(item) for item in value)
        return value

    schema_snapshot = snapshot(descriptor.input_schema)

    assert isinstance(descriptor.input_schema["x-syntara-nested"], tuple)
    with pytest.raises(TypeError):
        descriptor.input_schema["x-syntara-nested"][0]["values"].append("changed")

    assert descriptor.input_schema == schema_snapshot


def test_every_descriptor_schema_is_deeply_immutable() -> None:
    descriptor = StepDescriptor.for_step("acme.plugin.echo", _make_step(TaskStep))
    input_snapshot = json.loads(json.dumps(descriptor.input_schema))
    output_snapshot = json.loads(json.dumps(descriptor.output_schema))

    def assert_unchanged() -> None:
        assert descriptor.input_schema == input_snapshot
        assert descriptor.output_schema == output_snapshot

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


def test_installed_action_descriptor_rejects_a_non_action_step() -> None:
    step = _make_step(TaskStep)
    installed_action = _installed_action(EchoInput.model_json_schema())

    with pytest.raises(StepBindingError, match="ActionStep"):
        StepDescriptor.for_installed_action(step, installed_action)


def test_installed_action_descriptor_rejects_an_invalid_manifest_schema() -> None:
    step = _make_step(ActionStep)
    installed_action = _installed_action({"type": "invalid"})

    with pytest.raises(StepBindingError, match="valid JSON Schema"):
        StepDescriptor.for_installed_action(step, installed_action)


def test_installed_action_descriptor_accepts_resolvable_schema_references() -> None:
    step = _make_step(ActionStep)
    input_schema = {
        "type": "object",
        "properties": {"message": {"$ref": "#/$defs/message"}},
        "$defs": {"message": {"type": "string"}},
    }

    descriptor = StepDescriptor.for_installed_action(
        step,
        _installed_action(input_schema),
    )

    assert descriptor.input_schema == input_schema


def test_installed_action_descriptor_rejects_a_dangling_schema_reference() -> None:
    step = _make_step(ActionStep)
    input_schema = {
        "type": "object",
        "properties": {"message": {"$ref": "#/$defs/missing"}},
    }

    installed_action = _installed_action(input_schema)

    with pytest.raises(StepBindingError, match=r"unresolvable \$ref: #/\$defs/missing"):
        StepDescriptor.for_installed_action(step, installed_action)


def test_installed_action_descriptor_rejects_nested_dangling_references() -> None:
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

    installed_action = _installed_action(input_schema)

    with pytest.raises(StepBindingError, match=r"unresolvable \$ref: #/\$defs/missing"):
        StepDescriptor.for_installed_action(step, installed_action)


def test_resolved_step_executes_directly_without_container_invocation() -> None:
    bindings = StepBindings()
    step = _make_step(TaskStep)
    bindings.bind(StepDescriptor.for_step("acme.plugin.echo", step), step)

    context = ExecutionContext(invocation_id="invoke-1")
    output = bindings.resolve("acme.plugin.echo").execute(EchoInput(message="hello"), context)

    assert output == EchoOutput(message="hello", invocation_id="invoke-1")


def test_duplicate_identity_is_rejected() -> None:
    bindings = StepBindings()
    step = _make_step(TaskStep)
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
            taxonomy=StepTaxonomy.ACTION,
            input_schema=input_schema,
            output_schema=output_schema,
        )


def test_taxonomy_mismatch_is_rejected() -> None:
    bindings = StepBindings()
    step = _make_step(TaskStep)
    descriptor = StepDescriptor(
        step_identity="acme.plugin.echo",
        taxonomy=StepTaxonomy.ACTION,
        input_schema=EchoInput.model_json_schema(),
        output_schema=EchoOutput.model_json_schema(),
    )

    with pytest.raises(StepBindingError, match="does not match"):
        bindings.bind(descriptor, step)


def test_taxonomy_strings_are_normalized() -> None:
    descriptor = StepDescriptor(
        step_identity="acme.plugin.echo",
        taxonomy="task",
        input_schema=EchoInput.model_json_schema(),
        output_schema=EchoOutput.model_json_schema(),
    )

    assert descriptor.taxonomy is StepTaxonomy.TASK


def test_invalid_taxonomy_is_rejected() -> None:
    input_schema = EchoInput.model_json_schema()
    output_schema = EchoOutput.model_json_schema()

    with pytest.raises(StepBindingError, match="valid step taxonomy"):
        StepDescriptor(
            step_identity="acme.plugin.echo",
            taxonomy="not-a-taxonomy",
            input_schema=input_schema,
            output_schema=output_schema,
        )


def test_callers_cannot_mark_a_plain_action_descriptor_as_manifest_trusted() -> None:
    with pytest.raises(StepBindingError, match="for_installed_action"):
        StepDescriptor(
            step_identity="acme.plugin.echo",
            taxonomy=StepTaxonomy.ACTION,
            input_schema=EchoInput.model_json_schema(),
            output_schema=EchoOutput.model_json_schema(),
            input_schema_source="manifest",
        )


def test_implementation_derived_action_descriptor_supports_authoring_validation() -> None:
    step = _make_step(ActionStep)
    descriptor = StepDescriptor.for_step("acme.plugin.echo", step)
    bindings = StepBindings()

    bindings.bind(descriptor, step)

    assert bindings.binding("acme.plugin.echo") == StepBinding(descriptor, step)
    with pytest.raises(StepBindingError, match="installed contract"):
        StepExecutor(bindings.binding("acme.plugin.echo"))


def test_binding_and_execution_reject_a_duck_typed_action_descriptor() -> None:
    step = _make_step(ActionStep)
    fake_descriptor = SimpleNamespace(
        step_identity="acme.plugin.echo",
        taxonomy=StepTaxonomy.ACTION,
        input_schema={"type": "object"},
        output_schema=EchoOutput.model_json_schema(),
        require_executable_contract=lambda: None,
    )
    bindings = StepBindings()

    with pytest.raises(StepBindingError, match="StepDescriptor"):
        bindings.bind(fake_descriptor, step)  # type: ignore[arg-type]

    with pytest.raises(StepBindingError, match="StepDescriptor"):
        StepExecutor(StepBinding(fake_descriptor, step))  # type: ignore[arg-type]


def test_binding_and_execution_reject_an_action_descriptor_subclass() -> None:
    class ForgedDescriptor(StepDescriptor):
        def require_executable_contract(self) -> None:
            return None

    step = _make_step(ActionStep)
    forged_descriptor = ForgedDescriptor(
        step_identity="acme.plugin.echo",
        taxonomy=StepTaxonomy.ACTION,
        input_schema={"type": "object"},
        output_schema=EchoOutput.model_json_schema(),
    )
    bindings = StepBindings()

    with pytest.raises(StepBindingError, match="StepDescriptor"):
        bindings.bind(forged_descriptor, step)

    with pytest.raises(StepBindingError, match="StepDescriptor"):
        StepExecutor(StepBinding(forged_descriptor, step))


def test_installed_action_execution_requires_factory_provenance() -> None:
    step = _make_step(ActionStep)
    duck_typed_manifest = SimpleNamespace(
        step_identity="acme.plugin.echo",
        input_schema=EchoInput.model_json_schema(),
    )

    with pytest.raises(TypeError, match="InstalledActionManifest"):
        StepDescriptor.for_installed_action(
            step,
            duck_typed_manifest,  # type: ignore[arg-type]
        )

    forged_manifest = InstalledActionManifest()
    object.__setattr__(forged_manifest, "step_identity", "acme.plugin.echo")
    object.__setattr__(forged_manifest, "input_schema", EchoInput.model_json_schema())

    with pytest.raises(StepBindingError, match="verified installed manifest"):
        StepDescriptor.for_installed_action(step, forged_manifest)

    class ForgedManifest(InstalledActionManifest):
        pass

    with pytest.raises(TypeError, match="InstalledActionManifest"):
        StepDescriptor.for_installed_action(step, ForgedManifest())

    forged_descriptor = StepDescriptor(
        step_identity="acme.plugin.echo",
        taxonomy=StepTaxonomy.ACTION,
        input_schema=EchoInput.model_json_schema(),
        output_schema=EchoOutput.model_json_schema(),
    )
    bindings = StepBindings()

    bindings.bind(forged_descriptor, step)

    with pytest.raises(StepBindingError, match="installed contract"):
        StepExecutor(StepBinding(forged_descriptor, step))


def test_platform_managed_driver_is_rejected_as_a_python_implementation() -> None:
    bindings = StepBindings()
    descriptor = StepDescriptor(
        step_identity="acme.plugin.echo",
        taxonomy=StepTaxonomy.ACTION,
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
