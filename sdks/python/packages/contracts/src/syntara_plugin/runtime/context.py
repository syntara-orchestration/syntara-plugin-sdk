"""Transport-independent execution context for step types."""

from __future__ import annotations

from collections.abc import Mapping
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


class _NoCredentialProvider(Enum):
    VALUE = "not supplied"


@dataclass(frozen=True)
class ExecutionContext:
    """Invocation-scoped data passed to a step type without transport types.

    The context deliberately excludes protobuf messages, gRPC objects, container
    invocation types, and raw credential values. A trusted host supplies a
    provider; the executor binds it to the installed declaration for one invocation.
    """

    invocation_id: str
    workflow_run_id: str | None = None
    runtime_context: Mapping[str, Any] = field(default_factory=dict)
    cancellation: Event = field(default_factory=Event, compare=False, repr=False)
    credential_provider: InitVar[CredentialProvider | None | _NoCredentialProvider] = (
        _NoCredentialProvider.VALUE
    )
    _provider: CredentialProvider | None = field(default=None, init=False, compare=False, repr=False)
    _credential_scope: _CredentialScope | None = field(default=None, init=False, compare=False, repr=False)

    def __post_init__(
        self, credential_provider: CredentialProvider | None | _NoCredentialProvider
    ) -> None:
        """Validate the minimal invariants of an invocation-scoped context."""

        if not self.invocation_id:
            raise ValueError("invocation_id must not be empty")
        provider = None if credential_provider is _NoCredentialProvider.VALUE else credential_provider
        object.__setattr__(self, "_provider", provider)

    def credential(self, name: str) -> CredentialBinding:
        """Request a declared binding during execution; the handle expires on return."""
        if self._credential_scope is None:
            raise CredentialAccessError("expired")
        return self._credential_scope.handle(name)

    def _with_credentials(self, names: tuple[str, ...]) -> ExecutionContext:
        if self._credential_scope is not None:
            raise CredentialAccessError("unauthorized")
        context = ExecutionContext(
            invocation_id=self.invocation_id, workflow_run_id=self.workflow_run_id,
            runtime_context=self.runtime_context, cancellation=self.cancellation,
        )
        object.__setattr__(context, "_credential_scope", _CredentialScope(names, self._provider))
        return context

    @property
    def cancelled(self) -> bool:
        """Whether cooperative cancellation has been requested."""

        return self.cancellation.is_set()
