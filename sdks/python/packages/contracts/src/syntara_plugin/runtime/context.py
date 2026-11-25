"""Transport-independent execution context for step types."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import InitVar, dataclass, field
from enum import Enum
from threading import Event
from typing import Any

from .credentials import (
    CredentialAccessError,
    CredentialBinding,
    CredentialProvider,
    _CredentialScope,
)
from .toolbox import ContainerToolbox, _new_toolbox


class _NoCredentialProvider(Enum):
    VALUE = "not supplied"


@dataclass(frozen=True)
class ExecutionContext:
    """Invocation-scoped data passed to a step type without transport types.

    The context deliberately excludes protobuf messages, gRPC objects, container
    invocation types, and raw credential values. A trusted host supplies a
    provider; the executor binds it to the installed declaration for one invocation.

    Attributes:
        invocation_id: Host-assigned identity of this particular invocation,
            used for event and cancellation correlation.
        runtime_context: Optional non-secret caller-defined data visible to the
            plugin, defaulting to {}. Keys are defined by the caller.
        cancellation: Host-controlled cooperative cancellation signal; inspect
            cancelled to decide whether to stop work.
        credential_provider: Host-supplied credential access. Plugin code uses
            credential(name), scoped to the installed step's declared bindings.
        partial_result_reporter: Runtime-provided transport-neutral event callback.
    """

    invocation_id: str
    runtime_context: Mapping[str, Any] = field(default_factory=dict)
    cancellation: Event = field(default_factory=Event, compare=False, repr=False)
    credential_provider: InitVar[CredentialProvider | None | _NoCredentialProvider] = (
        _NoCredentialProvider.VALUE
    )
    _partial_result_reporter: Callable[[Mapping[str, Any], bool], None] | None = field(
        default=None, compare=False, repr=False
    )
    _provider: CredentialProvider | None = field(
        default=None, init=False, compare=False, repr=False
    )
    _credential_scope: _CredentialScope | None = field(
        default=None, init=False, compare=False, repr=False
    )
    _toolbox: ContainerToolbox = field(init=False, compare=False, repr=False)

    def __post_init__(
        self, credential_provider: CredentialProvider | None | _NoCredentialProvider
    ) -> None:
        """Validate the minimal invariants of an invocation-scoped context."""

        if not self.invocation_id:
            raise ValueError("invocation_id must not be empty")
        provider = (
            None if credential_provider is _NoCredentialProvider.VALUE else credential_provider
        )
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_toolbox", _new_toolbox(self.cancellation, self.invocation_id))

    def credential(self, name: str) -> CredentialBinding:
        """Request a declared binding during execution; the handle expires on return."""
        if self._credential_scope is None:
            raise CredentialAccessError("expired")
        return self._credential_scope.handle(name)

    @property
    def toolbox(self) -> ContainerToolbox:
        """Return this invocation's host-service surface."""

        return self._toolbox

    def _with_credentials(
        self, names: tuple[str, ...], schemas: Mapping[str, Any] | None = None
    ) -> ExecutionContext:
        if self._credential_scope is not None:
            raise CredentialAccessError("unauthorized")
        context = ExecutionContext(
            invocation_id=self.invocation_id,
            runtime_context=self.runtime_context,
            cancellation=self.cancellation,
            _partial_result_reporter=self._partial_result_reporter,
        )
        object.__setattr__(context, "_toolbox", self._toolbox)
        object.__setattr__(
            context, "_credential_scope", _CredentialScope(names, self._provider, schemas)
        )
        return context

    def report_partial_result(
        self, data: Mapping[str, Any], *, is_last: bool = False
    ) -> None:
        """Emit a bounded snapshot and optionally mark it as the last update.

        The event is best-effort and does not wait for host acknowledgement.
        It does not complete the step. Container execution checks each partial
        object against the step's output schema while allowing object fields
        required only by the final output to be omitted. Included values must
        still satisfy their declared schemas. Include any branch discriminator
        when reporting fields under a conditional output schema. Partial objects
        have at most 32 top-level properties and canonical representations of
        at most 16384 bytes. Pass an empty object with ``is_last=True`` when no
        partial data is available.
        """

        if self._partial_result_reporter is None:
            raise RuntimeError("partial-result reporting is unavailable for this execution")
        self._partial_result_reporter(data, is_last)

    @property
    def cancelled(self) -> bool:
        """Whether cooperative cancellation has been requested."""

        return self.cancellation.is_set()
