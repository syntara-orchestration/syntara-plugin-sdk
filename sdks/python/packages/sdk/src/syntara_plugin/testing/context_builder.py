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
# so a context that needs derivation leaves room for that suffix.
_IDENTITY_CHARSET = re.compile(r"^[A-Za-z0-9._:-]+$")
_ABI_MAX = 256
_DERIVED_SUFFIX = "-run"
_DERIVATION_MAX = _ABI_MAX - len(_DERIVED_SUFFIX)


def _require_identity(
    value: str,
    *,
    field: str,
    maximum: int,
    note: str = "",
) -> None:
    """Reject identifier values the container ABI would refuse later."""

    if not _IDENTITY_CHARSET.fullmatch(value) or len(value) > maximum:
        message = (
            f"{field} must be a Syntara identity (letters, digits, '. _ : -', "
            f"at most {maximum} characters{note}) so both execution paths accept it"
        )
        raise HarnessUsageError(message)


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
        workflow_run_id: Optional workflow-run correlation identifier; when
            supplied it must be a Syntara identity (letters, digits,
            ``. _ : -``, at most 256 characters) so both execution paths
            accept it.
        runtime_context: Optional non-secret runtime data surfaced to the step.
        cancelled: Pre-set the cooperative cancellation signal when true.
    """

    if invocation_id is not None:
        _require_identity(
            invocation_id,
            field="invocation_id",
            maximum=_DERIVATION_MAX,
            note=", leaving room for the derived run id",
        )
    if workflow_run_id is not None:
        _require_identity(workflow_run_id, field="workflow_run_id", maximum=_ABI_MAX)
    context = ExecutionContext(
        invocation_id=(
            invocation_id if invocation_id is not None else f"test-invocation-{uuid4().hex}"
        ),
        workflow_run_id=workflow_run_id,
        runtime_context=dict(runtime_context or {}),
    )
    if cancelled:
        context.cancellation.set()
    return context


def normalize_run_id(context: ExecutionContext) -> ExecutionContext:
    """Validate one context's identifiers and give it the deterministic run id.

    Both invocation helpers run this before executing, so raw
    ``ExecutionContext`` objects are held to the same rules as harness-built
    ones: identifiers must be Syntara identities the container ABI accepts,
    and an invocation id that needs a derived run id must leave room for the
    suffix. When the context already carries a run id, its length may use the
    full ABI maximum because nothing is derived.

    The container ABI requires a workflow-run identifier, so the container
    path can never present ``None``. When a context carries no run id, both
    invocation helpers substitute ``{invocation_id}-run`` so the two SDK paths
    present identical identifiers to the business hook.
    """

    _require_identity(context.invocation_id, field="invocation_id", maximum=_ABI_MAX)
    if context.workflow_run_id is not None:
        _require_identity(context.workflow_run_id, field="workflow_run_id", maximum=_ABI_MAX)
        return context
    _require_identity(
        context.invocation_id,
        field="invocation_id",
        maximum=_DERIVATION_MAX,
        note=", leaving room for the derived run id",
    )
    return ExecutionContext(
        invocation_id=context.invocation_id,
        workflow_run_id=f"{context.invocation_id}{_DERIVED_SUFFIX}",
        runtime_context=context.runtime_context,
        cancellation=context.cancellation,
    )
