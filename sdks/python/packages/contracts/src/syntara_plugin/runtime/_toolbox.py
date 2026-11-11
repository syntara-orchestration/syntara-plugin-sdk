"""Private invocation-scoped access to container host services."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from threading import Event
from typing import Protocol

from .protocol import container_pb2


@dataclass(frozen=True)
class _InvocationRequestContext:
    invocation_id: str
    cancellation: Event
    deadline: float


class _CredentialRequestTransport(Protocol):
    """Transport the existing credential messages for one invocation."""

    def request_credential(
        self, name: str, context: _InvocationRequestContext
    ) -> AbstractContextManager[container_pb2.CredentialResponse]: ...

    def receive_credential_response(
        self, response: container_pb2.CredentialResponse, context: _InvocationRequestContext
    ) -> None: ...

    def close(self) -> None: ...


class _ContainerToolbox:
    """Own private, invocation-scoped host transport capabilities."""

    def __init__(
        self, context: _InvocationRequestContext, credential_transport: _CredentialRequestTransport
    ) -> None:
        self._context = context
        self._credential_transport = credential_transport

    def _request_credential(
        self, name: str
    ) -> AbstractContextManager[container_pb2.CredentialResponse]:
        return self._credential_transport.request_credential(name, self._context)

    def _receive_credential_response(self, response: container_pb2.CredentialResponse) -> None:
        self._credential_transport.receive_credential_response(response, self._context)

    def _close(self) -> None:
        self._credential_transport.close()
