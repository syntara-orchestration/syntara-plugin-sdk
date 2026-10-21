"""Tests for normative contract canonicalization and request hashing."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

import pytest
from syntara_plugin.contracts import (
    CanonicalJsonError,
    canonical_json_bytes,
    canonical_json_digest,
    container_workload_request_digest,
)


def _request() -> dict[str, Any]:
    """Return a compact generic request with already-populated hash fields."""
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "ContainerWorkloadRequest",
        "workItem": {
            "inputs": {"title": "Example"},
            "requestHash": "sha256:" + "a" * 64,
        },
        "policySnapshot": {
            "effectiveCapabilities": ["network.github-api"],
            "requestHash": "sha256:" + "b" * 64,
        },
        "credentialLeases": [],
    }


def test_canonical_json_uses_stable_compact_key_ordering() -> None:
    """Mapping insertion order cannot change a contract digest."""
    first = {"z": [True, None], "a": {"number": 1}}
    second = {"a": {"number": 1}, "z": [True, None]}

    assert canonical_json_bytes(first) == b'{"a":{"number":1},"z":[true,null]}'
    assert canonical_json_digest(first) == canonical_json_digest(second)


@pytest.mark.parametrize("value", [math.nan, {"value": math.inf}, {"set": {"not-json"}}])
def test_canonical_json_rejects_non_json_values(value: object) -> None:
    """Hashes never silently coerce lossy or non-portable Python values."""
    with pytest.raises(CanonicalJsonError):
        canonical_json_bytes(value)


def test_container_request_hash_ignores_only_the_mirrored_hash_fields() -> None:
    """A claimed hash cannot influence its own recomputation."""
    request = _request()
    expected = container_workload_request_digest(request)

    request["workItem"]["requestHash"] = "sha256:" + "c" * 64
    request["policySnapshot"]["requestHash"] = "sha256:" + "d" * 64
    assert container_workload_request_digest(request) == expected

    request["workItem"]["inputs"]["title"] = "Tampered"
    assert container_workload_request_digest(request) != expected


def test_capture_budget_is_hashed_without_inserting_or_rewriting_defaults() -> None:
    request = _request()
    original = deepcopy(request)
    omitted_digest = container_workload_request_digest(request)
    assert request == original
    assert "maxOutputBytes" not in request["workItem"]

    request["workItem"]["maxOutputBytes"] = 1_048_576
    default_digest = container_workload_request_digest(request)
    assert default_digest != omitted_digest
    request["workItem"]["maxOutputBytes"] = 2_097_152
    assert container_workload_request_digest(request) not in {omitted_digest, default_digest}
    assert request["workItem"]["maxOutputBytes"] == 2_097_152
