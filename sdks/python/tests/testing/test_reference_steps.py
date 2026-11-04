"""Reference contracts exercised through the SDK and container adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from syntara_plugin.runtime import InstalledStepManifest, StepBinding, StepDescriptor, StepKind
from syntara_plugin.runtime.container import ContainerResult
from syntara_plugin.runtime.outcome import thaw_json
from syntara_plugin.testing import (
    CREDENTIAL_REFERENCE_CASES,
    INVALID_INPUT_CASES,
    VALID_INPUT_CASES,
    CredentialReferenceCase,
    CredentialReferenceStep,
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


def _credential_reference_binding(credential_name: str) -> StepBinding:
    step = CredentialReferenceStep()
    metadata = {
        "name": "credential-reference",
        "displayName": "Credential reference",
        "description": "A reference Action that consumes a declared credential.",
    }
    manifest = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Action",
        "metadata": metadata,
        "spec": {
            "runtime": {
                "kind": "custom-runner",
                "entryPoint": entry_point_of(CredentialReferenceStep),
            },
            "integrationType": "syntara.testing.reference-integration",
            "input": {"kind": "inline", "value": INPUT_SCHEMA},
            "output": {"kind": "inline", "value": OUTPUT_SCHEMA},
        },
    }
    credential_targets = [
        {
            "apiVersion": "syntara.io/v1alpha1",
            "kind": "IntegrationType",
            "metadata": metadata,
            "spec": {
                "id": "syntara.testing.reference-integration",
                "endpointKinds": ["https"],
                "credentialTypes": [credential_name],
                "configuration": {"kind": "inline", "value": {}},
            },
        },
        {
            "apiVersion": "syntara.io/v1alpha1",
            "kind": "CredentialRecipe",
            "metadata": metadata,
            "spec": {
                "id": credential_name,
                "integrationType": "syntara.testing.reference-integration",
                "credentialSchema": {"kind": "inline", "value": {"type": "object"}},
                "secretFields": ["token"],
                "issuance": {"kind": "static-bearer.v1"},
            },
        },
    ]
    installed = InstalledStepManifest.from_manifest(
        "syntara.testing.credential-reference",
        manifest,
        {},
        credential_targets=credential_targets,
    )
    return StepBinding(StepDescriptor.for_installed_step(step, installed), step)


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


@pytest.mark.parametrize("case", CREDENTIAL_REFERENCE_CASES, ids=lambda case: case.name)
def test_credential_reference_fixture_matches_on_both_paths(
    tmp_path: Path, case: CredentialReferenceCase
) -> None:
    path = tmp_path / "credential"
    path.write_text(json.dumps({"token": "synthetic-reference-secret"}), encoding="utf-8")
    binding = _credential_reference_binding(case.credential_name)
    context = build_execution_context(
        invocation_id="reference-credential",
        credential_file_paths={case.credential_name: str(path)},
        credential_directory=tmp_path,
    )

    direct = invoke_step(binding, case.inputs, context=context)
    container = invoke_step_in_container(binding, case.inputs, context=context)

    assert binding.descriptor.credential_names == (case.credential_name,)
    assert_outcomes_equivalent(direct, container)
    assert direct.ok
    assert direct.status == 0
    assert direct.status_message == "Completed"
    assert direct.error is None
    assert direct.output == case.expected_output
    assert container.output == case.expected_output
    assert "synthetic-reference-secret" not in repr(direct)
    assert "synthetic-reference-secret" not in repr(container)


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
