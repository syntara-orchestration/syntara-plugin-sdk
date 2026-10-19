"""Example tests for the Python-bound Trigger step."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from python_steps.record_opened import RecordOpenedStep
from syntara_plugin.runtime import (
    StepBindingError,
    StepBindings,
    StepDescriptor,
    StepTaxonomy,
)
from syntara_plugin.testing import (
    assert_outcomes_equivalent,
    invoke_step,
    invoke_step_in_container,
)

WORKSPACE = Path(__file__).resolve().parents[1]


def test_trigger_manifest_validates_against_the_canonical_bundle(
    manifest_validator,
) -> None:
    document = yaml.safe_load(
        (WORKSPACE / "steps/record-opened/manifest.yaml").read_text(encoding="utf-8")
    )
    manifest_validator(document)


def test_python_bound_trigger_executes_directly(mock_execution_context, step_bindings) -> None:
    binding = step_bindings.bind(RecordOpenedStep)
    outcome = invoke_step(
        binding, {"repository": "syntara-plugin-sdk"}, context=mock_execution_context()
    )
    assert outcome.ok
    assert outcome.run_calls == 1
    assert outcome.output == {
        "issue_url": "https://issues.example.test/syntara-plugin-sdk/1",
        "repository": "syntara-plugin-sdk",
    }


def test_invalid_event_payload_blocks_the_hook(mock_execution_context, step_bindings) -> None:
    binding = step_bindings.bind(RecordOpenedStep)
    outcome = invoke_step(binding, {}, context=mock_execution_context())
    assert not outcome.ok
    assert outcome.run_calls == 0


def test_direct_and_container_paths_are_equivalent(
    mock_execution_context, step_bindings
) -> None:
    binding = step_bindings.bind(RecordOpenedStep)
    context = mock_execution_context(invocation_id="trigger-equivalence")
    inputs = {"repository": "syntara-plugin-sdk"}
    direct = invoke_step(binding, inputs, context=context)
    container = invoke_step_in_container(binding, inputs, context=context)
    assert_outcomes_equivalent(direct, container)


def test_platform_managed_drivers_are_not_python_steps() -> None:
    """The webhook.v1 driver is a declarative manifest runtime, not a class."""

    descriptor = StepDescriptor(
        step_identity="example.python-steps.webhook-driver",
        taxonomy=StepTaxonomy.TRIGGER,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )
    bindings = StepBindings()
    with pytest.raises(StepBindingError, match="platform-managed"):
        bindings.bind(descriptor, "http.v1")  # type: ignore[arg-type]
