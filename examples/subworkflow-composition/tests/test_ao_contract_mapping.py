"""AO ↔ node-sdk field mapping evidence for the ANSTRAT-2347 POC.

These tests pin what maps cleanly and what must be injected by a bridge.
They do not change production contracts.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

WORKSPACE = Path(__file__).resolve().parents[1]
AO_FIXTURES = WORKSPACE / "fixtures" / "ao"
SDK_FIXTURES = WORKSPACE.parent.parent / "contracts" / "fixtures"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_yaml(relative: str) -> dict:
    return yaml.safe_load((WORKSPACE / relative).read_text(encoding="utf-8"))


def test_ao_parameter_schema_requires_uuid_and_trigger_selector() -> None:
    ao = _load_json(AO_FIXTURES / "sub_workflow.parameters.reference.json")
    assert set(ao["required"]) == {"workflow_id", "trigger_node_id"}
    assert ao["properties"]["workflow_id"]["format"] == "uuid"
    assert "trigger_node_id" in ao["properties"]
    assert "input_mapping" in ao["properties"]


def test_sdk_call_contract_has_qualified_workflow_and_no_trigger_selector() -> None:
    call = _load_json(SDK_FIXTURES / "subworkflow-call.valid.json")
    invocation = call["spec"]["invocation"]
    assert invocation == {
        "kind": "workflow-invocation",
        "workflow": "acme.release.publish-artifact",
    }
    assert "trigger" not in invocation
    assert "trigger_node_id" not in invocation
    assert "workflow_id" not in invocation


def test_sdk_call_output_is_business_result_not_execution_metadata() -> None:
    call = _load_json(SDK_FIXTURES / "subworkflow-call.valid.json")
    ao_result = _load_json(AO_FIXTURES / "sub_workflow.output.reference.json")
    sdk_output_props = set(call["spec"]["output"]["value"]["properties"])
    ao_result_props = set(ao_result["properties"])

    # Business fields the example child surfaces.
    assert {"releaseId", "publishedAt"} <= sdk_output_props
    # AO Result envelope is execution metadata, not the business payload.
    assert {
        "child_execution_id",
        "child_workflow_id",
        "child_status",
        "outputs",
    } <= ao_result_props
    assert sdk_output_props.isdisjoint(
        {
            "child_execution_id",
            "child_workflow_id",
            "child_workflow_version",
            "child_status",
        }
    )


def test_sdk_trigger_configuration_mirrors_ao_per_workflow_input_schema_pattern() -> None:
    trigger = _load_json(SDK_FIXTURES / "subworkflow-trigger.valid.json")
    ao_trigger = _load_json(
        AO_FIXTURES / "subworkflow_trigger.parameters.reference.json"
    )
    assert "input_schema" in ao_trigger["properties"]
    assert trigger["spec"]["driver"] == "subworkflow.v1"
    assert "configuration" in trigger["spec"]
    # Per-workflow parameters live on the trigger; both sides use that pattern.
    assert trigger["spec"]["configuration"]["value"]["properties"].keys() == (
        trigger["spec"]["input"]["value"]["properties"].keys()
    )


def test_example_parent_stand_in_preserves_invocation_shape_without_ao_fields() -> None:
    parent = _load_yaml("steps/call-publish-artifact/manifest.yaml")
    assert parent["kind"] == "Control"
    assert "trigger_node_id" not in parent["spec"]
    assert parent["spec"]["invocation"]["kind"] == "workflow-invocation"
    # Stand-in uses custom-runner; contract-true fixture uses platform-runner.
    assert parent["spec"]["runtime"]["kind"] == "custom-runner"


def test_documented_gaps_are_exactly_the_three_bridge_injections() -> None:
    """Bridge must inject these; contracts alone cannot express AO V1."""

    call = _load_json(SDK_FIXTURES / "subworkflow-call.valid.json")
    ao = _load_json(AO_FIXTURES / "sub_workflow.parameters.reference.json")
    gaps = {
        "workflow_id_vs_qualified": (
            ao["properties"]["workflow_id"]["format"] == "uuid"
            and isinstance(call["spec"]["invocation"]["workflow"], str)
            and "." in call["spec"]["invocation"]["workflow"]
        ),
        "missing_trigger_selector": "trigger_node_id" not in call["spec"]["invocation"],
        "output_envelope_mismatch": True,
    }
    assert all(gaps.values()), gaps
