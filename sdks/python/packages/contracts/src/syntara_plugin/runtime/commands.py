"""Invocation-scoped commands exchanged with the Execution Plane."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from uuid import UUID

from .protocol import container_pb2


@dataclass(frozen=True)
class ContainerCommand(ABC):
    """An invocation-scoped request for a host-provided service."""

    command_id: str
    invocation_id: str

    @abstractmethod
    def _protocol_command(self) -> container_pb2.ContainerCommand:
        """Return the language-neutral command payload."""


@dataclass(frozen=True)
class CredentialRefResolutionCommand(ContainerCommand):
    """Request resolution of one opaque credential reference."""

    credential_ref: UUID

    def _protocol_command(self) -> container_pb2.ContainerCommand:
        return container_pb2.ContainerCommand(
            command_id=self.command_id,
            credential_ref_resolution=container_pb2.CredentialRefResolutionCommand(
                credential_ref=str(self.credential_ref)
            ),
        )
