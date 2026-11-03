"""Every composition manifest validates against the canonical contract bundle."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from syntara_plugin.testing import assert_schemas_agree, schema_agreement_issues

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
        "workflow": "example.release.publish-artifact",
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
    assert_schemas_agree(
        parent["spec"]["input"]["value"],
        child["spec"]["input"]["value"],
        source_label="the parent's spec.input",
        target_label="the child trigger's spec.input",
    )


def test_child_trigger_input_passes_the_workflow_parameters_through() -> None:
    """The trigger's input is the workflow's parameter schema, passed through.

    AO's subworkflow_trigger carries the child contract as the per-workflow
    parameter schema (``parameters.input_schema``) and does not fix input per
    step type: ``spec.configuration`` defines the parameters and ``spec.input``
    mirrors them unchanged.
    """

    child = _load("steps/publish-artifact-requested/manifest.yaml")
    assert_schemas_agree(
        child["spec"]["configuration"]["value"],
        child["spec"]["input"]["value"],
        source_label="the child trigger's spec.configuration",
        target_label="the child trigger's spec.input",
    )


def test_parent_output_and_child_result_declare_the_same_result() -> None:
    """The parent surfaces the child workflow's terminal result, unenforced."""

    parent = _load("steps/call-publish-artifact/manifest.yaml")
    terminal = _load("steps/publish-artifact/manifest.yaml")
    assert_schemas_agree(
        parent["spec"]["output"]["value"],
        terminal["spec"]["output"]["value"],
        source_label="the parent's spec.output",
        target_label="the terminal step's spec.output",
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
    """The agreement check must fail when a field type or constraint diverges.

    Guards the guard: if the comparison ever degrades back to comparing
    property names only, these mutations would slip through.
    """

    parent = _load("steps/call-publish-artifact/manifest.yaml")
    drifted = _load("steps/publish-artifact/manifest.yaml")
    mutate(drifted["spec"]["output"]["value"])
    issues = schema_agreement_issues(
        parent["spec"]["output"]["value"], drifted["spec"]["output"]["value"]
    )
    assert issues, "the drifted schema must produce at least one issue"
