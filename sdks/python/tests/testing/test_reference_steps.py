"""Reference contracts exercised through the SDK and container adapter."""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator
from syntara_plugin.runtime import StepBinding, StepKind
from syntara_plugin.runtime.container import ContainerResult
from syntara_plugin.runtime.outcome import thaw_json
from syntara_plugin.testing import (
    INVALID_INPUT_CASES,
    VALID_INPUT_CASES,
    PayloadCase,
    ReferenceActionStep,
    ReferenceControlStep,
    ReferenceTriggerStep,
    StepBindingsFactory,
    assert_outcomes_equivalent,
    build_execution_context,
    entry_point_of,
    invoke_step,
    invoke_step_in_container,
    load_step_class,
)

# Independent manifest schemas exercise drift checking against the typed models.
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "priority": {"type": "string", "enum": ["low", "medium", "high"], "default": "low"},
        "count": {"type": "integer", "default": 1},
        "tags": {"type": "array", "items": {"type": "string"}, "default": []},
        "api_token": {"type": "string", "default": ""},
    },
    "required": ["title"],
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "priority": {"type": "string"}},
    "required": ["summary", "priority"],
}


@pytest.fixture(
    params=[
        (ReferenceActionStep, StepKind.ACTION),
        (ReferenceControlStep, StepKind.CONTROL),
        (ReferenceTriggerStep, StepKind.TRIGGER),
    ],
    ids=["action", "control", "trigger"],
)
def reference_binding(request: pytest.FixtureRequest) -> StepBinding:
    step_class, kind = request.param
    entry_point = entry_point_of(step_class)
    assert load_step_class(entry_point) is step_class
    binding = StepBindingsFactory(strict=True).bind(
        entry_point, input_schema=INPUT_SCHEMA, output_schema=OUTPUT_SCHEMA
    )
    assert binding.descriptor.kind is kind
    assert binding.descriptor.is_installed_step
    assert binding.descriptor.installed_entry_point == entry_point
    assert thaw_json(binding.descriptor.input_schema) == INPUT_SCHEMA
    assert thaw_json(binding.descriptor.output_schema) == OUTPUT_SCHEMA
    return binding


@pytest.mark.parametrize("case", VALID_INPUT_CASES, ids=lambda case: case.name)
def test_reference_success_matches_the_installed_contract_on_both_paths(
    reference_binding: StepBinding, case: PayloadCase
) -> None:
    context = build_execution_context(invocation_id="reference-valid")
    direct = invoke_step(reference_binding, case.inputs, context=context)
    container = invoke_step_in_container(reference_binding, case.inputs, context=context)

    assert_outcomes_equivalent(direct, container)
    assert direct.ok
    assert direct.status == 0
    assert direct.status_message == "Completed"
    assert direct.error is None
    assert direct.raised is None
    assert direct.run_calls == 1
    assert container.run_calls == 1
    assert direct.output == {
        "summary": f"processed {case.inputs['title']}",
        "priority": case.inputs.get("priority", "low"),
    }
    validator = Draft202012Validator(thaw_json(reference_binding.descriptor.output_schema))
    validator.validate(direct.output)
    validator.validate(container.output)

    # This digest belongs to the ABI result, not the direct SDK wrapper.
    assert isinstance(container.raw, ContainerResult)
    assert container.raw.output_schema_digest == reference_binding.descriptor.schema_digest("output")


@pytest.mark.parametrize("case", INVALID_INPUT_CASES, ids=lambda case: case.name)
def test_reference_invalid_input_short_circuits_on_both_paths(
    reference_binding: StepBinding, case: PayloadCase
) -> None:
    context = build_execution_context(invocation_id="reference-invalid")
    direct = invoke_step(reference_binding, case.inputs, context=context)
    container = invoke_step_in_container(reference_binding, case.inputs, context=context)

    assert_outcomes_equivalent(direct, container)
    assert not direct.ok
    assert direct.status == 1
    assert direct.raised is None
    assert direct.output is None
    assert direct.error is not None
    assert direct.error.code == "INVALID_INPUT"
    assert direct.run_calls == 0
    assert container.run_calls == 0
