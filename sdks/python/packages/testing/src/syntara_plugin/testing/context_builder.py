"""Construction of locally approved execution contexts for step tests."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from syntara_plugin.runtime import ExecutionContext

from .errors import HarnessUsageError

# The container ABI accepts only Syntara identity characters and at most 256
# of them; the harness derives ``{invocation_id}-run`` as the workflow-run id,
# so the accepted maximum leaves room for that four-character suffix.
_IDENTITY = re.compile(r"^[A-Za-z0-9._:-]{1,252}$")


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
        invocation_id: Stable invocation identifier; a unique test identity
            when omitted. Must be a Syntara identity (letters, digits,
            ``. _ : -``, at most 252 characters) so the context and its
            derived run id also work on the container path.
        workflow_run_id: Optional workflow-run correlation identifier.
        runtime_context: Optional non-secret runtime data surfaced to the step.
        cancelled: Pre-set the cooperative cancellation signal when true.
    """

    if invocation_id is not None and not _IDENTITY.fullmatch(invocation_id):
        message = (
            "invocation_id must be a Syntara identity (letters, digits, '. _ : -', "
            "at most 252 characters, leaving room for the derived run id) so both "
            "execution paths accept it"
        )
        raise HarnessUsageError(message)
    context = ExecutionContext(
        invocation_id=invocation_id if invocation_id is not None else f"test-invocation-{uuid4().hex}",
        workflow_run_id=workflow_run_id,
        runtime_context=dict(runtime_context or {}),
    )
    if cancelled:
        context.cancellation.set()
    return context


def normalize_run_id(context: ExecutionContext) -> ExecutionContext:
    """Give one context the deterministic run id both execution paths present.

    The container ABI requires a workflow-run identifier, so the container path
    can never present ``None``. When a context carries no run id, both
    invocation helpers substitute ``{invocation_id}-run`` so the two SDK paths
    present identical identifiers to the business hook.
    """

    if context.workflow_run_id is not None:
        return context
    return ExecutionContext(
        invocation_id=context.invocation_id,
        workflow_run_id=f"{context.invocation_id}-run",
        runtime_context=context.runtime_context,
        cancellation=context.cancellation,
    )
