"""Tests for the field-level schema agreement check."""

from __future__ import annotations

import pytest
from syntara_plugin.testing import (
    SchemaAgreementError,
    assert_schemas_agree,
    schema_agreement_issues,
)

RELEASE_SCHEMA = {
    "type": "object",
    "properties": {
        "releaseId": {"type": "string", "description": "caller perspective"},
        "publishedAt": {"type": "string", "format": "date-time"},
    },
    "required": ["releaseId", "publishedAt"],
}


def test_identical_schemas_agree() -> None:
    assert schema_agreement_issues(RELEASE_SCHEMA, RELEASE_SCHEMA) == ()


def test_descriptions_are_ignored() -> None:
    """Perspective prose differs by design; only the contract shape matters."""

    reworded = {
        **RELEASE_SCHEMA,
        "properties": {
            "releaseId": {"type": "string", "description": "callee perspective"},
            "publishedAt": {
                "type": "string",
                "format": "date-time",
                "description": "callee perspective",
            },
        },
    }
    assert_schemas_agree(RELEASE_SCHEMA, reworded)


def test_type_drift_names_the_field_and_both_values() -> None:
    drifted = dict(
        RELEASE_SCHEMA,
        properties={
            "releaseId": {"type": "integer"},
            "publishedAt": RELEASE_SCHEMA["properties"]["publishedAt"],
        },
    )
    issues = schema_agreement_issues(RELEASE_SCHEMA, drifted)
    assert issues == (
        ".properties.releaseId.type: 'string' in the source schema "
        "vs 'integer' in the target schema",
    )


def test_missing_property_is_reported_per_side() -> None:
    issues = schema_agreement_issues(RELEASE_SCHEMA, {"type": "object"})
    assert ".properties: missing from the target schema" in issues
    assert ".required: missing from the target schema" in issues


def test_required_drift_reports_the_diverging_list() -> None:
    drifted = {**RELEASE_SCHEMA, "required": ["releaseId"]}
    issues = schema_agreement_issues(RELEASE_SCHEMA, drifted)
    assert issues == (
        ".required: 2 item(s) in the source schema vs 1 in the target schema",
    )


def test_assert_raises_with_labels_and_every_issue() -> None:
    drifted = {
        "properties": {
            "releaseId": {"type": "integer"},
            "publishedAt": {"type": "string", "format": "date-time"},
        },
        "required": ["releaseId", "publishedAt"],
    }
    with pytest.raises(SchemaAgreementError) as excinfo:
        assert_schemas_agree(
            RELEASE_SCHEMA,
            drifted,
            source_label="the parent's spec.output",
            target_label="the terminal step's spec.output",
        )
    message = str(excinfo.value)
    assert "the parent's spec.output and the terminal step's spec.output" in message
    assert ".properties.releaseId.type" in message
    assert ".type: missing from the target schema" in message
    assert excinfo.value.issues


def test_mismatched_shapes_report_the_location() -> None:
    issues = schema_agreement_issues(
        {"type": "object"}, {"type": "array", "items": {"type": "string"}}
    )
    assert issues == (
        ".items: missing from the source schema",
        ".type: 'object' in the source schema vs 'array' in the target schema",
    )
