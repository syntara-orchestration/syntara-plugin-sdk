"""Phase 2: AO envelope wrap around the composition business result."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from subworkflow_composition.ao_envelope import wrap_business_result_as_ao_result
from subworkflow_composition.publish_artifact import PUBLISHED_AT

WORKSPACE = Path(__file__).resolve().parents[1]
AO_RESULT = WORKSPACE / "fixtures" / "ao" / "sub_workflow.output.reference.json"

CHILD_RESULT = {"releaseId": "stable:syntara-cli", "publishedAt": PUBLISHED_AT}


def test_wrap_places_business_result_under_step_keyed_outputs() -> None:
    envelope = wrap_business_result_as_ao_result(
        CHILD_RESULT,
        child_execution_id="11111111-1111-1111-1111-111111111111",
        child_workflow_id="22222222-2222-2222-2222-222222222222",
        child_workflow_name="publish-artifact",
        child_workflow_version=3,
    )
    assert envelope["child_status"] == "completed"
    assert envelope["outputs"]["publish_artifact"]["Result"] == CHILD_RESULT


def test_wrapped_envelope_validates_against_ao_result_reference() -> None:
    schema = json.loads(AO_RESULT.read_text(encoding="utf-8"))
    # Drop $comment for validator noise; schema itself is the AO Result shape.
    schema.pop("$comment", None)
    envelope = wrap_business_result_as_ao_result(
        CHILD_RESULT,
        child_execution_id="11111111-1111-1111-1111-111111111111",
        child_workflow_id="22222222-2222-2222-2222-222222222222",
        child_workflow_name="publish-artifact",
        child_workflow_version=3,
    )
    errors = list(Draft202012Validator(schema).iter_errors(envelope))
    assert not errors, errors
