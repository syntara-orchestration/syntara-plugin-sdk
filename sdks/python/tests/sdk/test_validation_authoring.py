"""Authoring-path tests for contract drift validation against compiled descriptors."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel
from syntara_plugin.runtime import (
    ActionStep,
    DriftCode,
    ExecutionContext,
    SchemaDriftError,
    StepBindings,
    StepDescriptor,
    StepKind,
    TriggerStep,
    validate_plugin_bindings,
)
from syntara_plugin.sdk import Plugin, Target, compile_model

DIGEST = "quay.io/acme/issue-tools@sha256:" + "a" * 64

ENTRY_POINT = f"{__name__}:CreateIssueStep"


class CreateIssueInput(BaseModel):
    title: str
    priority: str = "low"


class CreateIssueOutput(BaseModel):
    issue_url: str


class CreateIssueStep(ActionStep[CreateIssueInput, CreateIssueOutput]):
    def run(self, inputs: CreateIssueInput, context: ExecutionContext) -> CreateIssueOutput:
        return CreateIssueOutput(issue_url=f"https://example.test/issues/{inputs.title}")


INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "priority": {"type": "string"},
    },
    "required": ["title"],
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"issue_url": {"type": "string"}},
    "required": ["issue_url"],
}


def _plugin() -> dict[str, object]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Plugin",
        "metadata": {
            "namespace": "acme",
            "name": "issue-tools",
            "version": "0.1.0",
            "displayName": "Issue tools",
            "description": "Creates GitHub issues.",
        },
        "spec": {
            "documentation": {"path": "docs/README.md"},
            "workload": {"image": "plugin-workload"},
            "targets": ["steps/create-issue/manifest.yaml"],
        },
    }


def _action(
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    entry_point: str = ENTRY_POINT,
) -> dict[str, object]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Action",
        "metadata": {
            "name": "create-issue",
            "displayName": "Create issue",
            "description": "Creates one issue.",
        },
        "spec": {
            "runtime": {"kind": "custom-runner", "entryPoint": entry_point},
            "input": {"kind": "inline", "value": input_schema or INPUT_SCHEMA},
            "output": {"kind": "inline", "value": output_schema or OUTPUT_SCHEMA},
        },
    }


def _compile(action: dict[str, object]) -> Any:
    return compile_model(
        Plugin(_plugin(), assets={"docs/README.md": b"# Issue tools\n"}),
        [Target("steps/create-issue/manifest.yaml", action)],
        image_bindings={"plugin-workload": DIGEST},
    )


def _bindings() -> StepBindings:
    bindings = StepBindings()
    bindings.bind(
        StepDescriptor(
            "acme.issue-tools.create-issue",
            StepKind.ACTION,
            CreateIssueInput.model_json_schema(),
            CreateIssueOutput.model_json_schema(),
        ),
        CreateIssueStep(),
    )
    return bindings


def test_compiled_descriptor_passes_validation_in_strict_mode() -> None:
    result = _compile(_action())

    validate_plugin_bindings(result.descriptor, _bindings(), strict=True)


def test_renamed_manifest_field_raises_with_schema_pointer() -> None:
    renamed = dict(INPUT_SCHEMA)
    renamed["properties"] = {
        "heading": {"type": "string"},
        "priority": {"type": "string"},
    }
    result = _compile(_action(input_schema=renamed))

    bindings = _bindings()
    with pytest.raises(SchemaDriftError) as raised:
        validate_plugin_bindings(result.descriptor, bindings, strict=True)

    assert "/properties/heading" in str(raised.value)
    assert DriftCode.FIELD_MISSING_FROM_MODEL in {
        issue.code for report in raised.value.report for issue in report.issues
    }


def test_loosened_output_bound_raises() -> None:
    loosened = {
        "type": "object",
        "properties": {"issue_url": {"type": "string", "maxLength": 5}},
        "required": ["issue_url"],
    }
    result = _compile(_action(output_schema=loosened))

    bindings = _bindings()
    with pytest.raises(SchemaDriftError) as raised:
        validate_plugin_bindings(result.descriptor, bindings, strict=True)

    assert DriftCode.CONSTRAINT_INCOMPATIBLE in {
        issue.code for report in raised.value.report for issue in report.issues
    }


def test_wrong_entry_point_raises() -> None:
    result = _compile(_action(entry_point="issue_tools.actions:CreateIssue"))

    bindings = _bindings()
    with pytest.raises(SchemaDriftError) as raised:
        validate_plugin_bindings(result.descriptor, bindings)

    assert DriftCode.ENTRY_POINT_MISMATCH in {
        issue.code for report in raised.value.report for issue in report.issues
    }


def test_unconstrained_manifest_fails_against_typed_model() -> None:
    result = _compile(_action(input_schema={"type": "object"}))

    bindings = _bindings()
    with pytest.raises(SchemaDriftError) as raised:
        validate_plugin_bindings(result.descriptor, bindings)

    codes = {issue.code for report in raised.value.report for issue in report.issues}
    assert DriftCode.FIELD_MISSING_FROM_SCHEMA in codes


def test_strict_mode_rejects_warning_only_drift() -> None:
    drifted = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
    }
    result = _compile(_action(input_schema=drifted))

    bindings = _bindings()
    validate_plugin_bindings(result.descriptor, bindings)

    with pytest.raises(SchemaDriftError):
        validate_plugin_bindings(result.descriptor, bindings, strict=True)


def test_platform_runner_target_is_not_validated_against_bindings() -> None:
    action = _action()
    plugin = _plugin()
    plugin["spec"] = {
        "documentation": {"path": "docs/README.md"},
        "targets": ["steps/create-issue/manifest.yaml"],
    }
    specification = action["spec"]
    assert isinstance(specification, dict)
    specification["runtime"] = {
        "kind": "platform-runner",
        "driver": "http.v1",
        "operation": {
            "method": "POST",
            "path": "/repos/{input.owner}/{input.repository}/issues",
            "requestMap": {"kind": "inline", "value": {"title": {"from": "/title"}}},
            "responseMap": {"kind": "inline", "value": {"id": {"from": "/id"}}},
        },
    }
    result = compile_model(
        Plugin(plugin, assets={"docs/README.md": b"# Issue tools\n"}),
        [Target("steps/create-issue/manifest.yaml", action)],
    )

    validate_plugin_bindings(result.descriptor, StepBindings())
    validate_plugin_bindings(result.descriptor, _bindings())


class IssueOpenedInput(BaseModel):
    repository: str


class IssueOpenedOutput(BaseModel):
    issue_url: str


class IssueOpenedTrigger(TriggerStep[IssueOpenedInput, IssueOpenedOutput]):
    def run(self, inputs: IssueOpenedInput, context: ExecutionContext) -> IssueOpenedOutput:
        return IssueOpenedOutput(issue_url=f"https://example.test/{inputs.repository}/1")


def _trigger() -> dict[str, object]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Trigger",
        "metadata": {
            "name": "issue-opened",
            "displayName": "Issue opened",
            "description": "Fires when an issue is opened.",
        },
        "spec": {
            "driver": "webhook.v1",
            "runtime": {"kind": "custom-runner", "entryPoint": f"{__name__}:IssueOpenedTrigger"},
            "configuration": {"kind": "inline", "value": {"type": "object"}},
            "input": {"kind": "inline", "value": IssueOpenedInput.model_json_schema()},
            "output": {"kind": "inline", "value": IssueOpenedOutput.model_json_schema()},
        },
    }


def test_compiled_trigger_binding_is_not_undeclared() -> None:
    plugin = _plugin()
    specification = plugin["spec"]
    assert isinstance(specification, dict)
    specification["targets"] = [
        "steps/create-issue/manifest.yaml",
        "triggers/issue-opened/manifest.yaml",
    ]
    result = compile_model(
        Plugin(plugin, assets={"docs/README.md": b"# Issue tools\n"}),
        [
            Target("steps/create-issue/manifest.yaml", _action()),
            Target("triggers/issue-opened/manifest.yaml", _trigger()),
        ],
        image_bindings={"plugin-workload": DIGEST},
    )
    bindings = _bindings()
    bindings.bind(
        StepDescriptor(
            "acme.issue-tools.issue-opened",
            StepKind.TRIGGER,
            IssueOpenedInput.model_json_schema(),
            IssueOpenedOutput.model_json_schema(),
        ),
        IssueOpenedTrigger(),
    )

    compiled_trigger = [
        target for target in result.descriptor["spec"]["targets"] if target.get("kind") == "Trigger"
    ]
    assert compiled_trigger
    assert compiled_trigger[0]["runtime"] == {
        "abi": "syntara.container/v1alpha1",
        "entryPoint": f"{__name__}:IssueOpenedTrigger",
        "kind": "custom-runner",
        "workload": "plugin-workload",
    }

    validate_plugin_bindings(result.descriptor, bindings, strict=True)
