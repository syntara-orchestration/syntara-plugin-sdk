"""Example tests for a Control-category Step (composition inside run())."""

from __future__ import annotations

from python_steps.compose_remediation import ComposeRemediationStep
from syntara_plugin.testing import (
    assert_outcomes_equivalent,
    invoke_step,
    invoke_step_in_container,
)


def test_workflow_composes_operations_inside_run(
    mock_execution_context, step_bindings
) -> None:
    binding = step_bindings.bind(ComposeRemediationStep)
    outcome = invoke_step(
        binding, {"records": ["  Alpha ", "Beta"]}, context=mock_execution_context()
    )
    assert outcome.ok
    assert outcome.run_calls == 1
    assert outcome.output == {
        "plan": ["remediate:alpha", "remediate:beta"],
        "total": 2,
    }


def test_invalid_input_blocks_the_hook(mock_execution_context, step_bindings) -> None:
    binding = step_bindings.bind(ComposeRemediationStep)
    outcome = invoke_step(
        binding, {"records": "not-a-list"}, context=mock_execution_context()
    )
    assert not outcome.ok
    assert outcome.run_calls == 0


def test_workflow_direct_and_container_paths_are_equivalent(
    mock_execution_context, step_bindings
) -> None:
    binding = step_bindings.bind(ComposeRemediationStep)
    context = mock_execution_context(invocation_id="workflow-equivalence")
    inputs = {"records": ["Alpha"]}
    direct = invoke_step(binding, inputs, context=context)
    container = invoke_step_in_container(binding, inputs, context=context)
    assert_outcomes_equivalent(direct, container)
