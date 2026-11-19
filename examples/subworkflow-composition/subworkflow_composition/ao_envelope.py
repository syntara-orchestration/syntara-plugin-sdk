"""Demonstrate wrapping an SDK business result into an AO-shaped output envelope.

AO's ``SubWorkflowOutput.Result`` is child execution metadata. The SDK Control
``spec.output`` is the child's business result. A future platform-runner (or
bridge) can place the business payload under ``outputs`` while filling the
metadata fields from the control plane.
"""

from __future__ import annotations

from typing import Any


def wrap_business_result_as_ao_result(
    business_result: dict[str, Any],
    *,
    child_execution_id: str,
    child_workflow_id: str,
    child_workflow_name: str,
    child_workflow_version: int,
    child_status: str = "completed",
    terminal_step_id: str = "publish_artifact",
) -> dict[str, Any]:
    """Build an AO-like Result object from an SDK business payload.

    The business result is stored under ``outputs[<terminal_step_id>].Result``
    using the StandardOutputWrapper convention AO already uses for steps.
    """

    return {
        "child_execution_id": child_execution_id,
        "child_workflow_id": child_workflow_id,
        "child_workflow_name": child_workflow_name,
        "child_workflow_version": child_workflow_version,
        "child_status": child_status,
        "outputs": {
            terminal_step_id: {
                "Result": business_result,
                "StatusCode": 0,
                "StatusMessage": "Completed",
                "ErrorMessage": None,
            }
        },
    }
