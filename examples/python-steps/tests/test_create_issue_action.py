"""Example tests for the custom-workload Action step (full manifest path)."""

from __future__ import annotations

from pathlib import Path

from python_steps.create_issue import CreateIssueStep
from syntara_plugin.testing import (
    assert_no_secrets,
    assert_outcomes_equivalent,
    invoke_step,
    invoke_step_in_container,
    secret_sentinel,
)

ENTRY_POINT = "python_steps.create_issue:CreateIssueStep"
WORKSPACE = Path(__file__).resolve().parents[1]


def test_valid_input_reaches_the_hook_once(mock_execution_context, step_bindings) -> None:
    binding = step_bindings.bind_action(ENTRY_POINT, workspace=WORKSPACE)
    outcome = invoke_step(
        binding, {"title": "Broken build", "priority": "high"}, context=mock_execution_context()
    )
    assert outcome.ok
    assert outcome.run_calls == 1
    assert outcome.output == {"issue_url": "https://issues.example.test/broken-build"}


def test_missing_required_field_blocks_the_hook(mock_execution_context, step_bindings) -> None:
    binding = step_bindings.bind_action(ENTRY_POINT, workspace=WORKSPACE)
    outcome = invoke_step(binding, {"priority": "high"}, context=mock_execution_context())
    assert not outcome.ok
    assert outcome.run_calls == 0
    assert outcome.error is not None
    assert outcome.error.code == "INVALID_INPUT"


def test_invalid_enum_value_blocks_the_hook(mock_execution_context, step_bindings) -> None:
    binding = step_bindings.bind_action(ENTRY_POINT, workspace=WORKSPACE)
    outcome = invoke_step(
        binding, {"title": "Broken build", "priority": "urgent"}, context=mock_execution_context()
    )
    assert not outcome.ok
    assert outcome.run_calls == 0


def test_secrets_never_leak_into_outcomes_or_messages(
    mock_execution_context, step_bindings
) -> None:
    token = secret_sentinel()
    binding = step_bindings.bind_action(ENTRY_POINT, workspace=WORKSPACE)
    outcome = invoke_step(
        binding,
        {"title": "Broken build", "api_token": token},
        context=mock_execution_context(),
    )
    assert outcome.ok
    assert_no_secrets(outcome, [token])


def test_direct_and_container_paths_are_equivalent(
    mock_execution_context, step_bindings
) -> None:
    binding = step_bindings.bind_action(ENTRY_POINT, workspace=WORKSPACE)
    context = mock_execution_context(invocation_id="example-equivalence")
    direct = invoke_step(binding, {"title": "Broken build"}, context=context)
    container = invoke_step_in_container(binding, {"title": "Broken build"}, context=context)
    assert_outcomes_equivalent(direct, container)


def test_conformance_in_one_call(mock_execution_context, step_bindings) -> None:
    from syntara_plugin.testing import check_step_conformance

    binding = step_bindings.bind_action(CreateIssueStep, workspace=WORKSPACE)
    report = check_step_conformance(
        binding,
        valid_cases=[{"title": "Broken build"}],
        invalid_cases=[{}, {"title": "x", "priority": "urgent"}],
        context=mock_execution_context(),
    )
    assert report.passed, report.failures
