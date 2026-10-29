"""Execute both sides of the composition and check the seams between them."""

from __future__ import annotations

from pathlib import Path

from subworkflow_composition.publish_artifact import PUBLISHED_AT
from syntara_plugin.testing import (
    assert_outcomes_equivalent,
    chain_steps,
    check_seam_compatible,
    invoke_step,
    invoke_step_in_container,
)

WORKSPACE = Path(__file__).resolve().parents[1]

PARENT = "subworkflow_composition.call_publish_artifact:CallPublishArtifactStep"
TRIGGER = (
    "subworkflow_composition.publish_artifact_requested:PublishArtifactRequestedStep"
)
TERMINAL = "subworkflow_composition.publish_artifact:PublishArtifactStep"

PARAMETERS = {"artifact": "syntara-cli", "channel": "stable"}
CHILD_RESULT = {"release_id": "stable:syntara-cli", "published_at": PUBLISHED_AT}


def test_child_trigger_emits_the_invocation_event(
    mock_execution_context, step_bindings
) -> None:
    binding = step_bindings.bind_step(TRIGGER, workspace=WORKSPACE)
    outcome = invoke_step(
        binding, PARAMETERS, context=mock_execution_context(workflow_run_id="parent-run-1")
    )
    assert outcome.ok
    assert outcome.run_calls == 1
    assert outcome.output == {**PARAMETERS, "parent_run_id": "parent-run-1"}


def test_child_trigger_rejects_a_payload_missing_a_parameter(
    mock_execution_context, step_bindings
) -> None:
    binding = step_bindings.bind_step(TRIGGER, workspace=WORKSPACE)
    outcome = invoke_step(
        binding, {"artifact": "syntara-cli"}, context=mock_execution_context()
    )
    assert not outcome.ok
    assert outcome.run_calls == 0


def test_parent_control_surfaces_the_child_workflow_result(
    mock_execution_context, step_bindings
) -> None:
    binding = step_bindings.bind_step(PARENT, workspace=WORKSPACE)
    outcome = invoke_step(binding, PARAMETERS, context=mock_execution_context())
    assert outcome.ok
    assert outcome.run_calls == 1
    assert outcome.output == CHILD_RESULT


def test_child_workflow_seam_accepts_the_trigger_event(step_bindings) -> None:
    """The trigger's event payload satisfies the terminal step's input."""

    trigger = step_bindings.bind_step(TRIGGER, workspace=WORKSPACE)
    terminal = step_bindings.bind_step(TERMINAL, workspace=WORKSPACE)
    report = check_seam_compatible(trigger, terminal)
    assert not report.issues, report.issues


def test_running_the_child_workflow_reaches_the_parent_declared_result(
    mock_execution_context, step_bindings
) -> None:
    """Chaining the child's steps produces exactly what the parent declares.

    ``chain_steps`` is sequential dataflow for tests, not a workflow engine.
    It stands in here for the child run the platform would start.
    """

    trigger = step_bindings.bind_step(TRIGGER, workspace=WORKSPACE)
    terminal = step_bindings.bind_step(TERMINAL, workspace=WORKSPACE)
    outcome = chain_steps(
        [trigger, terminal],
        PARAMETERS,
        context=mock_execution_context(workflow_run_id="parent-run-1"),
    )
    assert outcome.ok, outcome.seam_issues
    assert outcome.output == CHILD_RESULT


def test_parent_direct_and_container_paths_are_equivalent(
    mock_execution_context, step_bindings
) -> None:
    binding = step_bindings.bind_step(PARENT, workspace=WORKSPACE)
    context = mock_execution_context(invocation_id="subworkflow-equivalence")
    direct = invoke_step(binding, PARAMETERS, context=context)
    container = invoke_step_in_container(binding, PARAMETERS, context=context)
    assert_outcomes_equivalent(direct, container)
