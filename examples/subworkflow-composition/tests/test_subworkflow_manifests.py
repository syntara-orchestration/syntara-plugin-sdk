"""Every composition manifest validates against the canonical contract bundle."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

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


def test_parent_input_and_child_trigger_input_declare_the_same_parameters() -> None:
    """Parameter passing is an authoring obligation, not a validated constraint.

    Nothing in the contracts binds the parent's declared input to the child
    trigger's declared event payload; this test is the kind of check an author
    has to write themselves.
    """

    parent = yaml.safe_load(
        (WORKSPACE / "steps/call-publish-artifact/manifest.yaml").read_text(encoding="utf-8")
    )
    child = yaml.safe_load(
        (WORKSPACE / "steps/publish-artifact-requested/manifest.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert (
        parent["spec"]["input"]["value"]["properties"].keys()
        == child["spec"]["input"]["value"]["properties"].keys()
    )


def test_parent_output_and_child_result_declare_the_same_result() -> None:
    """The parent surfaces the child workflow's terminal result, unenforced."""

    parent = yaml.safe_load(
        (WORKSPACE / "steps/call-publish-artifact/manifest.yaml").read_text(encoding="utf-8")
    )
    terminal = yaml.safe_load(
        (WORKSPACE / "steps/publish-artifact/manifest.yaml").read_text(encoding="utf-8")
    )
    parent_output = parent["spec"]["output"]["value"]
    terminal_output = terminal["spec"]["output"]["value"]
    # Descriptions differ by perspective; the result contract must not.
    assert parent_output["properties"].keys() == terminal_output["properties"].keys()
    assert parent_output["required"] == terminal_output["required"]
