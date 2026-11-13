"""Construction of locally approved execution contexts for step type tests."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from syntara_plugin.runtime import ExecutionContext, FileCredentialProvider

from .errors import HarnessUsageError

# The container ABI accepts only Syntara identity characters and at most 256
# of them. Caller correlation data belongs in runtime_context.
_IDENTITY_CHARSET = re.compile(r"^[A-Za-z0-9._:-]+$")
_ABI_MAX = 256


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
    runtime_context: Mapping[str, Any] | None = None,
    cancelled: bool = False,
    credential_file_paths: Mapping[str, str] | None = None,
    credential_directory: Path | None = None,
) -> ExecutionContext:
    """Build one valid, locally approved ``ExecutionContext`` for test execution.

    "Approved" here means constructed by this harness: valid, free of real
    credentials, and authorized only for local test execution. Platform
    authorization and credential binding are owned by later platform stories
    and are deliberately not simulated.

    Args:
        invocation_id: Stable invocation identifier; a unique test identity
            when omitted. Must be a Syntara identity (letters, digits,
            ``. _ : -``, at most 256 characters).
        runtime_context: Optional non-secret runtime data surfaced to the step type.
        cancelled: Pre-set the cooperative cancellation signal when true.
    """

    if invocation_id is not None:
        _require_identity(
            invocation_id,
            field="invocation_id",
            maximum=_ABI_MAX,
        )
    context = ExecutionContext(
        invocation_id=(
            invocation_id if invocation_id is not None else f"test-invocation-{uuid4().hex}"
        ),
        runtime_context=dict(runtime_context or {}),
        credential_provider=(
            FileCredentialProvider(credential_file_paths, local_directory=credential_directory)
            if credential_file_paths is not None else None
        ),
    )
    if cancelled:
        context.cancellation.set()
    return context


def validate_execution_context(context: ExecutionContext) -> ExecutionContext:
    """Require an invocation identity accepted by both local and container paths."""
    _require_identity(context.invocation_id, field="invocation_id", maximum=_ABI_MAX)
    return context
