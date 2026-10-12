"""Minimal, inert runtime contracts shared by workloads and provider sandboxes.

These helpers validate data envelopes.  They do not start a process, read a
credential file, contact a broker, or make a network request.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from syntara_plugin.contracts import ContractBundle, load_bundle, validate_document

CONTAINER_ABI = "syntara.container/v1alpha1"
PROVIDER_ABI = "syntara.provider/v1alpha1"


class RuntimeOwnership(StrEnum):
    """The only execution ownership classes recognized by the shared ABI."""

    PLATFORM_RUNNER = "platform-runner"
    PUBLISHER_WORKLOAD = "custom-workload"


class FailureCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    AUTHORIZATION_FAILED = "AUTHORIZATION_FAILED"
    REMOTE_REJECTED = "REMOTE_REJECTED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    UPSTREAM_TRANSIENT = "UPSTREAM_TRANSIENT"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    POLICY_DENIED = "POLICY_DENIED"
    ABI_VIOLATION = "ABI_VIOLATION"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


@dataclass(frozen=True)
class RuntimeBinding:
    """An installed immutable image binding; never a source-authoring binding."""

    ownership: RuntimeOwnership
    image_digest: str
    abi: str = CONTAINER_ABI

    def to_document(self) -> dict[str, str]:
        return {
            "ownership": self.ownership.value,
            "imageDigest": self.image_digest,
            "abi": self.abi,
        }


@dataclass(frozen=True)
class ConformanceResult:
    valid: bool
    errors: tuple[str, ...]


class ConformanceHarness:
    """Schema-only conformance checks for untrusted workload/provider output."""

    def __init__(self, bundle: ContractBundle | None = None) -> None:
        self._bundle = bundle or load_bundle()

    def validate_runtime(self, document: Mapping[str, Any]) -> ConformanceResult:
        return _result(validate_document(self._bundle, "runtime", document))

    def validate_provider(self, document: Mapping[str, Any]) -> ConformanceResult:
        return _result(validate_document(self._bundle, "provider", document))


def validate_runtime(
    document: Mapping[str, Any], *, bundle: ContractBundle | None = None
) -> ConformanceResult:
    return ConformanceHarness(bundle).validate_runtime(document)


def validate_provider(
    document: Mapping[str, Any], *, bundle: ContractBundle | None = None
) -> ConformanceResult:
    return ConformanceHarness(bundle).validate_provider(document)


def _result(errors: list[str]) -> ConformanceResult:
    return ConformanceResult(valid=not errors, errors=tuple(errors))
