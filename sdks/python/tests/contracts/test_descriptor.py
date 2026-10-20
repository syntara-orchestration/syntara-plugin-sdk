"""Tests for the contract-owned descriptor schema digest rule."""

from __future__ import annotations

import pytest
from syntara_plugin.contracts import (
    canonical_json_digest,
    is_digest_image_reference,
    materialized_schemas,
    schema_digests,
)


def test_materialized_schema_fields_and_digests_are_canonical() -> None:
    target = {
        "configuration": {"type": "object"},
        "input": {"properties": {"name": {"type": "string"}}, "type": "object"},
        "output": [],
        "sourcePath": "steps/example.yaml",
        "schemaDigests": {"input": "stale"},
    }

    assert materialized_schemas(target) == {
        "configuration": target["configuration"],
        "input": target["input"],
    }
    assert schema_digests(target) == {
        "configuration": canonical_json_digest(target["configuration"]),
        "input": canonical_json_digest(target["input"]),
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("quay.io/acme/runner@sha256:" + "a" * 64, True),
        ("localhost:8443/acme/runner@sha256:" + "b" * 64, True),
        ("quay..io/acme/runner@sha256:" + "a" * 64, False),
        ("quay.io/acme/runner-@sha256:" + "a" * 64, False),
        ("quay.io/acme/runner:latest", False),
        (None, False),
    ],
)
def test_digest_image_reference_rejects_mutable_and_malformed_values(
    value: object, expected: bool
) -> None:
    assert is_digest_image_reference(value) is expected
