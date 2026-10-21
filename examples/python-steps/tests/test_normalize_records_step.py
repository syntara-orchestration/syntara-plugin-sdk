"""Example tests for a Control-category Step (descriptor path, no source manifest)."""

from __future__ import annotations

from python_steps.normalize_records import NormalizeRecordsStep
from syntara_plugin.testing import (
    assert_outcomes_equivalent,
    chain_steps,
    check_seam_compatible,
    identity_seam,
    invoke_step,
    invoke_step_in_container,
)


def test_control_executes_through_the_descriptor_path(
    mock_execution_context, step_bindings
) -> None:
    binding = step_bindings.bind(NormalizeRecordsStep)
    outcome = invoke_step(
        binding, {"records": ["  Alpha ", "Beta"]}, context=mock_execution_context()
    )
    assert outcome.ok
    assert outcome.run_calls == 1
    assert outcome.output == {"normalized": ["alpha", "beta"], "count": 2}


def test_control_rejects_wrong_input_types(mock_execution_context, step_bindings) -> None:
    binding = step_bindings.bind(NormalizeRecordsStep)
    outcome = invoke_step(binding, {"records": "not-a-list"}, context=mock_execution_context())
    assert not outcome.ok
    assert outcome.run_calls == 0


def test_direct_and_container_paths_are_equivalent(
    mock_execution_context, step_bindings
) -> None:
    binding = step_bindings.bind(NormalizeRecordsStep)
    context = mock_execution_context(invocation_id="control-equivalence")
    inputs = {"records": ["Alpha"]}
    direct = invoke_step(binding, inputs, context=context)
    container = invoke_step_in_container(binding, inputs, context=context)
    assert_outcomes_equivalent(direct, container)


def test_chained_control_steps_pass_outputs_through_an_explicit_seam(
    mock_execution_context, step_bindings
) -> None:
    first = step_bindings.bind(NormalizeRecordsStep, identity="example.python-steps.normalize-1")
    second = step_bindings.bind(NormalizeRecordsStep, identity="example.python-steps.normalize-2")
    seam_report = check_seam_compatible(first, second)
    assert not seam_report.passed(), "normalize output is not directly normalize input"

    outcome = chain_steps(
        [first, second],
        {"records": ["Alpha"]},
        context=mock_execution_context(workflow_run_id="example-chain"),
        seams=[lambda output: {"records": output["normalized"]}],
    )
    assert outcome.ok
    assert outcome.output == {"normalized": ["alpha"], "count": 1}


def test_identity_seam_documented_default(mock_execution_context, step_bindings) -> None:
    """The identity seam only fits when schemas actually match; here they do not."""

    binding = step_bindings.bind(NormalizeRecordsStep)
    outcome = chain_steps(
        [binding, binding],
        {"records": ["Alpha"]},
        context=mock_execution_context(),
        seams=[identity_seam],
    )
    assert not outcome.ok
    assert outcome.failed_link == 1
