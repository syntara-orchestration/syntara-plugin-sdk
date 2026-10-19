"""Shared schema materialization and digest helpers for plugin descriptors."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from syntara_plugin.contracts import canonical_json_digest

MATERIALIZED_SCHEMA_FIELDS = frozenset(
    {"configuration", "credentialSchema", "error", "input", "output"}
)


def materialized_schemas(target: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Return the descriptor fields that contain materialized JSON Schemas."""

    return {
        field_name: value
        for field_name, value in target.items()
        if field_name in MATERIALIZED_SCHEMA_FIELDS and isinstance(value, Mapping)
    }


def schema_digests(target: Mapping[str, Any]) -> dict[str, str]:
    """Return canonical digests for every materialized schema in a target."""

    return {
        field_name: canonical_json_digest(schema)
        for field_name, schema in sorted(materialized_schemas(target).items())
    }
