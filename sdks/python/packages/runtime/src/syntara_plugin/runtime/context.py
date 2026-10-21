"""Transport-independent execution context for typed plugin steps."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from threading import Event
from typing import Any

from .limits import DEFAULT_MAX_OUTPUT_BYTES, validate_max_output_bytes


@dataclass(frozen=True)
class ExecutionContext:
    """Invocation-scoped data passed to a step without transport types.

    The context deliberately excludes protobuf messages, gRPC objects, container
    invocation types, and raw credential values. Credential access is owned by the
    credential-binding story; this class only carries documented, non-secret
    invocation and runtime data plus a cooperative cancellation signal.
    """

    invocation_id: str
    workflow_run_id: str | None = None
    runtime_context: Mapping[str, Any] = field(default_factory=dict)
    cancellation: Event = field(default_factory=Event, compare=False, repr=False)
    # Per-stream script capture budget; independent of inline transport bounds.
    max_output_bytes: int = field(default=DEFAULT_MAX_OUTPUT_BYTES, kw_only=True)

    def __post_init__(self) -> None:
        """Validate the minimal invariants of an invocation-scoped context."""

        if not self.invocation_id:
            raise ValueError("invocation_id must not be empty")
        validate_max_output_bytes(self.max_output_bytes)

    @property
    def cancelled(self) -> bool:
        """Whether cooperative cancellation has been requested."""

        return self.cancellation.is_set()
