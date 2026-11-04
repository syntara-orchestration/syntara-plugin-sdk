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


def test_required_drift_reports_the_diverging_member() -> None:
    drifted = {**RELEASE_SCHEMA, "required": ["releaseId"]}
    issues = schema_agreement_issues(RELEASE_SCHEMA, drifted)
    assert issues == (
        ".required: 'publishedAt' in the source schema "
        "but not the target schema",
    )


def test_set_valued_keywords_ignore_member_order() -> None:
    """``required`` and ``enum`` are unordered in Draft 2020-12."""

    reordered = {
        **RELEASE_SCHEMA,
        "required": ["publishedAt", "releaseId"],
        "properties": {
            **RELEASE_SCHEMA["properties"],
            "status": {"type": "string", "enum": ["draft", "published", "archived"]},
        },
    }
    differently_ordered = {
        **reordered,
        "properties": {
            **reordered["properties"],
            "status": {"type": "string", "enum": ["archived", "draft", "published"]},
        },
    }
    assert schema_agreement_issues(reordered, differently_ordered) == ()


def test_enum_drift_reports_the_diverging_member() -> None:
    source = {"properties": {"status": {"enum": ["draft", "published"]}}}
    target = {"properties": {"status": {"enum": ["draft", "archived"]}}}
    issues = schema_agreement_issues(source, target)
    assert issues == (
        ".properties.status.enum: 'published' in the source schema "
        "but not the target schema",
        ".properties.status.enum: 'archived' in the target schema "
        "but not the source schema",
    )


def test_mixed_type_mapping_keys_do_not_crash() -> None:
    """PyYAML parses unquoted ``on:`` / ``yes:`` keys as booleans."""

    source = {"type": "object", "properties": {"on": {"type": "boolean"}}}
    target = {"type": "object", "properties": {"on": {"type": "string"}}}
    issues = schema_agreement_issues(source, target)
    assert issues == (
        ".properties.on.type: 'boolean' in the source schema "
        "vs 'string' in the target schema",
    )


def test_yaml_boolean_keys_beside_string_keys_do_not_crash() -> None:
    mixed_source = {"properties": {True: {"type": "boolean"}, "name": {"type": "string"}}}
    mixed_target = {"properties": {True: {"type": "boolean"}, "name": {"type": "string"}}}
    assert schema_agreement_issues(mixed_source, mixed_target) == ()


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
    assert not message.startswith("declared schemas do not agree")
    assert message.count("do not agree") == 1
    assert any(
        issue.startswith(".properties.releaseId.type")
        for issue in excinfo.value.issues
    )


def test_property_named_description_is_compared() -> None:
    """Only the annotation is dropped, not a property with that name."""

    source = {
        "type": "object",
        "properties": {"description": {"type": "string"}},
    }
    target = {
        "type": "object",
        "properties": {"description": {"type": "integer"}},
    }
    issues = schema_agreement_issues(source, target)
    assert issues == (
        ".properties.description.type: 'string' in the source schema "
        "vs 'integer' in the target schema",
    )


def test_description_annotation_under_a_property_named_description_is_ignored() -> None:
    source = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "caller perspective"},
        },
    }
    target = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "callee perspective"},
        },
    }
    assert_schemas_agree(source, target)


def test_boolean_and_numeric_values_do_not_compare_equal() -> None:
    """``True == 1`` in Python, but ``const: true`` and ``const: 1`` differ."""

    source = {"properties": {"flag": {"const": True}}}
    target = {"properties": {"flag": {"const": 1}}}
    issues = schema_agreement_issues(source, target)
    assert issues == (
        ".properties.flag.const: True in the source schema "
        "vs 1 in the target schema",
    )


def test_boolean_enum_members_do_not_match_numeric_members() -> None:
    source = {"properties": {"flag": {"enum": [True]}}}
    target = {"properties": {"flag": {"enum": [1]}}}
    issues = schema_agreement_issues(source, target)
    assert issues == (
        ".properties.flag.enum: True in the source schema "
        "but not the target schema",
        ".properties.flag.enum: 1 in the target schema "
        "but not the source schema",
    )


def test_reordered_object_enum_members_agree() -> None:
    """``enum`` is unordered in Draft 2020-12, even for object members."""

    source = {
        "properties": {
            "state": {
                "enum": [
                    {"releaseId": "r-1", "status": "draft"},
                    {"releaseId": "r-2", "status": "published"},
                ]
            }
        }
    }
    target = {
        "properties": {
            "state": {
                "enum": [
                    {"status": "published", "releaseId": "r-2"},
                    {"status": "draft", "releaseId": "r-1"},
                ]
            }
        }
    }
    assert schema_agreement_issues(source, target) == ()


def test_object_enum_drift_reports_the_diverging_member() -> None:
    source = {
        "properties": {
            "state": {"enum": [{"releaseId": "r-1", "status": "draft"}]}
        }
    }
    target = {
        "properties": {
            "state": {"enum": [{"releaseId": "r-1", "status": "archived"}]}
        }
    }
    issues = schema_agreement_issues(source, target)
    assert issues == (
        ".properties.state.enum: {'releaseId': 'r-1', 'status': 'draft'} "
        "in the source schema but not the target schema",
        ".properties.state.enum: {'releaseId': 'r-1', 'status': 'archived'} "
        "in the target schema but not the source schema",
    )


def test_array_enum_member_order_is_meaningful_inside_the_member() -> None:
    """Member order is ignored, but array order inside a member is not."""

    source = {"properties": {"tags": {"enum": [["a", "b"]]}}}
    target = {"properties": {"tags": {"enum": [["b", "a"]]}}}
    issues = schema_agreement_issues(source, target)
    assert issues == (
        ".properties.tags.enum: ['a', 'b'] in the source schema "
        "but not the target schema",
        ".properties.tags.enum: ['b', 'a'] in the target schema "
        "but not the source schema",
    )


def test_mismatched_shapes_report_the_location() -> None:
    issues = schema_agreement_issues(
        {"type": "object"}, {"type": "array", "items": {"type": "string"}}
    )
    assert issues == (
        ".items: missing from the source schema",
        ".type: 'object' in the source schema vs 'array' in the target schema",
    )
