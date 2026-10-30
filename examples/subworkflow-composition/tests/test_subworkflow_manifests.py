"""Every composition manifest validates against the canonical contract bundle."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from syntara_plugin.testing import schema_without_descriptions

WORKSPACE = Path(__file__).resolve().parents[1]

MANIFESTS = (
    "steps/call-publish-artifact/manifest.yaml",
    "steps/publish-artifact-requested/manifest.yaml",
    "steps/publish-artifact/manifest.yaml",
)


@pytest.mark.parametrize("relative_path", MANIFESTS)
def test_manifest_validates_against_the_canonical_bundle(
    relative_path: str, manifest_validator
) -> None:
    document = yaml.safe_load((WORKSPACE / relative_path).read_text(encoding="utf-8"))
    manifest_validator(document)


def test_parent_declares_a_workflow_invocation_of_the_child() -> None:
    """The parent Control names the child workflow it invokes.

    The identity carries no version: the contract has no field for pinning a
    published child version, so resolution is a control-plane concern. See
    ``contracts/fixtures/subworkflow-call.invalid.json``.
    """

    parent = yaml.safe_load(
        (WORKSPACE / "steps/call-publish-artifact/manifest.yaml").read_text(encoding="utf-8")
    )
    assert parent["spec"]["invocation"] == {
        "kind": "workflow-invocation",
        "workflow": "example.subworkflow-composition.publish-artifact",
    }


def _load(relative_path: str) -> dict:
    return yaml.safe_load((WORKSPACE / relative_path).read_text(encoding="utf-8"))


def test_parent_input_and_child_trigger_input_declare_the_same_parameters() -> None:
    """Parameter passing is an authoring obligation, not a validated constraint.

    Nothing in the contracts binds the parent's declared input to the child
    trigger's declared event payload; this test is the kind of check an author
    has to write themselves.
    """

    parent = _load("steps/call-publish-artifact/manifest.yaml")
    child = _load("steps/publish-artifact-requested/manifest.yaml")
    assert schema_without_descriptions(parent["spec"]["input"]["value"]) == (
        schema_without_descriptions(child["spec"]["input"]["value"])
    )


def test_parent_output_and_child_result_declare_the_same_result() -> None:
    """The parent surfaces the child workflow's terminal result, unenforced."""

    parent = _load("steps/call-publish-artifact/manifest.yaml")
    terminal = _load("steps/publish-artifact/manifest.yaml")
    parent_output = parent["spec"]["output"]["value"]
    terminal_output = terminal["spec"]["output"]["value"]
    # Descriptions differ by perspective; the result contract must not.
    assert schema_without_descriptions(parent_output) == schema_without_descriptions(
        terminal_output
    )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda schema: schema["properties"]["releaseId"].update(
                {"type": "integer"}
            ),
            id="field-type-drift",
        ),
        pytest.param(
            lambda schema: schema["properties"]["publishedAt"].update(
                {"format": "date-time-strict"}
            ),
            id="constraint-drift",
        ),
        pytest.param(
            lambda schema: schema["required"].remove("publishedAt"),
            id="required-drift",
        ),
    ],
)
def test_result_comparison_detects_schema_drift(mutate) -> None:
    """The deep comparison must fail when a field type or constraint diverges.

    Guards the guard: if the equality check above ever degrades back to
    comparing property names only, these mutations would slip through.
    """

    parent = _load("steps/call-publish-artifact/manifest.yaml")
    drifted = _load("steps/publish-artifact/manifest.yaml")
    mutate(drifted["spec"]["output"]["value"])
    assert schema_without_descriptions(parent["spec"]["output"]["value"]) != (
        schema_without_descriptions(drifted["spec"]["output"]["value"])
    )
