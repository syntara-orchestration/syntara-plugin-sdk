"""Construction of locally approved execution contexts for step tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from syntara_plugin.runtime import ExecutionContext


def build_execution_context(
    *,
    invocation_id: str | None = None,
    workflow_run_id: str | None = None,
    runtime_context: Mapping[str, Any] | None = None,
    cancelled: bool = False,
) -> ExecutionContext:
    """Build one valid, locally approved ``ExecutionContext`` for test execution.

    "Approved" here means constructed by this harness: valid, free of real
    credentials, and authorized only for local test execution. Platform
    authorization and credential binding are owned by later platform stories
    and are deliberately not simulated.

    Args:
        invocation_id: Stable invocation identifier; a unique test identity when omitted.
        workflow_run_id: Optional workflow-run correlation identifier.
        runtime_context: Optional non-secret runtime data surfaced to the step.
        cancelled: Pre-set the cooperative cancellation signal when true.
    """

    context = ExecutionContext(
        invocation_id=invocation_id if invocation_id is not None else f"test-invocation-{uuid4().hex}",
        workflow_run_id=workflow_run_id,
        runtime_context=dict(runtime_context or {}),
    )
    if cancelled:
        context.cancellation.set()
    return context
