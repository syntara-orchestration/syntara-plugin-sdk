"""Unit checks for the schema-derived input summaries and templates."""

from __future__ import annotations

from typing import Any

from syntara_plugin.sdk.input_template import (
    field_summaries,
    is_declared_secret,
    is_sensitive,
    render_summary_lines,
    template_for,
)

_CREATE_ISSUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "priority": {"type": "string", "default": "low", "enum": ["low", "medium", "high"]},
        "api_token": {"type": "string", "default": ""},
    },
    "required": ["title"],
}


def test_template_prefers_examples_over_default_const_and_enum() -> None:
    schema = {
        "type": "object",
        "properties": {
            "with_examples": {"type": "string", "examples": ["from-examples"], "default": "from-default"},
            "with_default": {"type": "string", "default": "from-default", "const": "from-const"},
            "with_const": {"type": "string", "const": "from-const", "enum": ["from-enum"]},
            "with_enum": {"type": "string", "enum": ["first", "second"]},
        },
        "required": ["with_examples", "with_default", "with_const", "with_enum"],
    }

    template = template_for(schema)

    assert template == {
        "with_examples": "from-examples",
        "with_default": "from-default",
        "with_const": "from-const",
        "with_enum": "first",
    }


def test_template_includes_required_properties_with_typed_empty_values() -> None:
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "count": {"type": "integer"},
            "ratio": {"type": "number"},
            "enabled": {"type": "boolean"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["name", "count", "ratio", "enabled", "tags"],
    }

    template = template_for(schema)

    assert template == {
        "name": "",
        "count": 0,
        "ratio": 0,
        "enabled": False,
        "tags": [],
    }


def test_template_includes_optional_properties_only_with_declared_values() -> None:
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "priority": {"type": "string", "default": "low"},
            "free_text": {"type": "string"},
            "level": {"type": "string", "enum": ["a", "b"]},
        },
        "required": ["title"],
    }

    template = template_for(schema)

    assert template == {"title": "", "priority": "low", "level": "a"}


def test_template_expands_required_nested_objects_recursively() -> None:
    schema = {
        "type": "object",
        "properties": {
            "target": {
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "port": {"type": "integer", "default": 443},
                    "tls": {
                        "type": "object",
                        "properties": {"enabled": {"type": "boolean"}},
                        "required": ["enabled"],
                    },
                },
                "required": ["host", "tls"],
            },
        },
        "required": ["target"],
    }

    template = template_for(schema)

    assert template == {"target": {"host": "", "port": 443, "tls": {"enabled": False}}}


def test_template_uses_format_hints_for_known_formats() -> None:
    schema = {
        "type": "object",
        "properties": {
            "created": {"type": "string", "format": "date-time"},
            "id": {"type": "string", "format": "uuid"},
            "owner": {"type": "string", "format": "email"},
        },
        "required": ["created", "id", "owner"],
    }

    template = template_for(schema)

    assert template == {
        "created": "2026-01-01T12:00:00Z",
        "id": "123e4567-e89b-42d3-a456-426614174000",
        "owner": "example@example.test",
    }


def test_template_is_deterministic_for_the_same_schema() -> None:
    # The literal pins the output, so any nondeterminism regresses this test;
    # api_token is sensitive: its declared default is masked out of the
    # template rather than placed in the redirectable output.
    assert template_for(_CREATE_ISSUE_SCHEMA) == {"title": "", "priority": "low"}
    assert template_for(dict(_CREATE_ISSUE_SCHEMA)) == {"title": "", "priority": "low"}


def test_template_terminates_on_self_referential_schemas() -> None:
    node: dict[str, Any] = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }
    node["properties"]["child"] = node

    template = template_for(node)

    assert template == {"value": ""}


def test_template_rejects_non_json_declared_values() -> None:
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer", "default": float("nan")}},
        "required": ["count"],
    }

    template = template_for(schema)

    assert template == {"count": 0}


def test_field_summaries_report_every_declared_property() -> None:
    summaries = field_summaries(_CREATE_ISSUE_SCHEMA)

    by_name = {summary.name: summary for summary in summaries}
    assert set(by_name) == {"title", "priority", "api_token"}
    assert by_name["title"].required is True
    assert by_name["priority"].required is False
    assert by_name["priority"].has_default is True
    assert by_name["priority"].default == "low"
    assert by_name["priority"].enum == ("low", "medium", "high")
    assert by_name["api_token"].sensitive is True
    assert by_name["title"].sensitive is False


def test_field_summaries_expand_required_nested_objects() -> None:
    schema = {
        "type": "object",
        "properties": {
            "target": {
                "type": "object",
                "properties": {"host": {"type": "string"}},
                "required": ["host"],
            },
        },
        "required": ["target"],
    }

    summaries = field_summaries(schema)

    assert len(summaries) == 1
    assert summaries[0].name == "target"
    assert [nested.name for nested in summaries[0].nested] == ["host"]


def test_is_sensitive_detects_write_only_password_format_and_names() -> None:
    assert is_sensitive("api_token", {"type": "string"}) is True
    assert is_sensitive("note", {"type": "string", "writeOnly": True}) is True
    assert is_sensitive("note", {"type": "string", "format": "password"}) is True
    assert is_sensitive("title", {"type": "string"}) is False


def test_is_sensitive_does_not_flag_credential_like_prefixes() -> None:
    """Plain words containing short markers stay readable in summaries."""

    assert is_sensitive("author", {"type": "string"}) is False
    assert is_sensitive("authorized_by", {"type": "string"}) is False
    assert is_sensitive("auth_token", {"type": "string"}) is True


def test_is_sensitive_matches_header_and_dashed_credential_names() -> None:
    """Hyphenated and header-style credential names are detected."""

    assert is_sensitive("Authorization", {}) is True
    assert is_sensitive("X-Api-Key", {}) is True
    assert is_sensitive("access-key", {}) is True
    assert is_sensitive("pwd", {}) is True
    assert is_sensitive("passphrase", {}) is True
    assert is_sensitive("private-key", {}) is True


def test_is_sensitive_honors_declared_secrets_inside_compositions() -> None:
    """writeOnly inside anyOf/oneOf/allOf still marks the field sensitive."""

    schema = {"anyOf": [{"type": "string", "writeOnly": True}, {"type": "null"}]}

    assert is_sensitive("value", schema) is True


def test_template_masks_declared_values_of_sensitive_fields() -> None:
    """A credential pasted into examples or default never reaches the template."""

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "api_token": {
                "type": "string",
                "default": "DECLARED-DEFAULT-SECRET",
                "examples": ["DECLARED-EXAMPLE-SECRET"],
            },
        },
        "required": ["title", "api_token"],
    }

    template = template_for(schema)
    lines = render_summary_lines("example.plugin.step", "Action", schema)
    joined = "\n".join(lines)

    assert template == {"title": "", "api_token": ""}
    assert "DECLARED-DEFAULT-SECRET" not in joined
    assert "DECLARED-EXAMPLE-SECRET" not in joined
    assert "default ***" in joined
    assert "examples: ***" in joined


def test_render_summary_lines_lists_fields_constraints_and_sensitivity() -> None:
    lines = render_summary_lines("example.python-steps.create-issue", "Action", _CREATE_ISSUE_SCHEMA)

    assert lines[0] == "Inputs for example.python-steps.create-issue (Action):"
    assert any("title" in line and "required" in line for line in lines)
    assert any("priority" in line and 'default "low"' in line for line in lines)
    assert any("low, medium, high" in line for line in lines)
    assert any("api_token" in line and "sensitive" in line for line in lines)


def test_render_summary_lines_handles_schemas_without_properties() -> None:
    lines = render_summary_lines("example.plugin.thing", "Action", {"type": "object"})

    assert lines == [
        "Inputs for example.plugin.thing (Action):",
        "  (no declared properties)",
    ]


def test_is_declared_secret_resolves_local_refs() -> None:
    """A secret declared in $defs is honored through a local $ref pointer."""

    document: dict[str, Any] = {
        "$defs": {"secretText": {"type": "string", "writeOnly": True}},
    }

    assert is_declared_secret({"$ref": "#/$defs/secretText"}, document) is True
    assert is_declared_secret({"$ref": "#/$defs/missing"}, document) is False
    # Without the document the pointer cannot be resolved; only direct
    # declarations and names apply there.
    assert is_declared_secret({"$ref": "#/$defs/secretText"}) is False


def test_is_declared_secret_terminates_on_ref_cycles() -> None:
    document: dict[str, Any] = {}
    document["$defs"] = {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}}

    assert is_declared_secret({"$ref": "#/$defs/a"}, document) is False


def test_template_masks_declared_values_behind_local_refs() -> None:
    """A $ref to a writeOnly $defs entry masks the field's declared values."""

    schema: dict[str, Any] = {
        "$defs": {"secretText": {"type": "string", "writeOnly": True}},
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "note": {"$ref": "#/$defs/secretText", "examples": ["REF-EXAMPLE-SECRET"]},
        },
        "required": ["title", "note"],
    }

    template = template_for(schema)
    lines = render_summary_lines("example.plugin.step", "Action", schema)
    joined = "\n".join(lines)

    assert template == {"title": "", "note": ""}
    assert "REF-EXAMPLE-SECRET" not in joined
    assert "sensitive" in joined


def test_chained_root_references_resolve_to_the_final_target() -> None:
    """A root $ref pointing at another $ref still yields the full template."""

    schema: dict[str, Any] = {
        "$defs": {
            "leaf": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
            },
            "mid": {"$ref": "#/$defs/leaf"},
            "root": {"$ref": "#/$defs/mid"},
        },
        "$ref": "#/$defs/root",
    }

    assert template_for(schema) == {"title": ""}


def test_reference_cycles_terminate() -> None:
    """A $ref cycle yields an empty template instead of looping."""

    schema: dict[str, Any] = {
        "$defs": {
            "a": {"$ref": "#/$defs/b"},
            "b": {"$ref": "#/$defs/a"},
        },
        "$ref": "#/$defs/a",
    }

    assert template_for(schema) == {}
