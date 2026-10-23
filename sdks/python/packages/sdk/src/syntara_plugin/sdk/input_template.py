"""Schema-derived input summaries and templates for ``syntara-plugin run``.

The compiled manifest's input schema is the single contract for the run
command. This module only describes that contract: it renders a human field
summary and a deterministic skeleton template. JSON Schema validation remains
the authority on what is accepted; the template is intentionally a starting
point, never a business-valid payload, and is never executed automatically.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Bounds keep the template walk terminating on adversarial or self-referential
# schemas; the compiler already bounds documents, this guards the interpreter.
_MAX_SUMMARY_DEPTH = 8
_MAX_TEMPLATE_DEPTH = 16

_UNSET = object()

# Deterministic, schema-shaped hints for well-known formats. They mirror the
# scaffold placeholders used by ``test-init`` so both commands suggest the
# same value for the same schema.
_FORMAT_HINTS: Mapping[str, str] = {
    "date-time": "2026-01-01T12:00:00Z",
    "date": "2026-01-01",
    "time": "12:00:00Z",
    "duration": "P1D",
    "uuid": "123e4567-e89b-42d3-a456-426614174000",
    "email": "example@example.test",
    "idn-email": "example@example.test",
    "uri": "https://example.test/",
    "uri-reference": "/example",
    "iri": "https://example.test/",
    "hostname": "example.test",
    "idn-hostname": "example.test",
    "ipv4": "192.0.2.1",
    "ipv6": "2001:db8::1",
    "json-pointer": "/example",
    "regex": "example",
}

_SENSITIVE_NAME_MARKERS = (
    "access_key",
    "apikey",
    "api_key",
    "authorization",
    "credential",
    "passwd",
    "password",
    "passphrase",
    "private_key",
    "pwd",
    "secret",
    "token",
)

_CONSTRAINT_KEYS = (
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minItems",
    "maxItems",
    "pattern",
)

_TYPE_COLUMN_WIDTH = 9


@dataclass(frozen=True)
class FieldSummary:
    """One declared input property, rendered for the human field summary."""

    name: str
    type: str
    required: bool
    sensitive: bool
    has_default: bool = False
    default: Any = None
    has_const: bool = False
    const: Any = None
    enum: tuple[Any, ...] = ()
    examples: tuple[Any, ...] = ()
    format: str | None = None
    constraints: tuple[str, ...] = ()
    description: str | None = None
    nested: tuple[FieldSummary, ...] = ()


def field_summaries(schema: Mapping[str, Any]) -> tuple[FieldSummary, ...]:
    """Summarize every declared top-level property of one input schema.

    Required object properties are expanded recursively into ``nested`` so the
    author sees every value they must supply; optional properties are listed
    even when the template omits them.
    """

    return _field_summaries(_deref(schema, schema), depth=0, seen=(), document=schema)


def is_declared_secret(
    property_schema: Mapping[str, Any], document: Mapping[str, Any] | None = None
) -> bool:
    """Return whether the schema itself declares the field a secret.

    Honors ``writeOnly`` and ``format: password`` directly and inside
    ``anyOf``/``oneOf``/``allOf`` composition keywords. When ``document`` is
    the schema's root, local ``$ref`` pointers are resolved before their
    target is checked, so a secret declared in ``$defs`` is honored.
    """

    return _is_declared_secret(property_schema, document, depth=0)


_MAX_REF_DEPTH = 8


def _is_declared_secret(
    property_schema: Mapping[str, Any],
    document: Mapping[str, Any] | None,
    *,
    depth: int,
) -> bool:
    if not isinstance(property_schema, Mapping):
        return False
    if property_schema.get("writeOnly") is True:
        return True
    declared_format = property_schema.get("format")
    if isinstance(declared_format, str) and declared_format == "password":
        return True
    for keyword in ("anyOf", "oneOf", "allOf"):
        options = property_schema.get(keyword)
        if isinstance(options, list) and any(
            isinstance(option, Mapping)
            and _is_declared_secret(option, document, depth=depth)
            for option in options
        ):
            return True
    reference = property_schema.get("$ref")
    if (
        depth < _MAX_REF_DEPTH
        and isinstance(reference, str)
        and reference.startswith("#/")
        and isinstance(document, Mapping)
    ):
        resolved = _resolve_local_ref(reference, document)
        if resolved is not None:
            return _is_declared_secret(resolved, document, depth=depth + 1)
    return False


def _resolve_local_ref(reference: str, document: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Resolve one local JSON pointer (RFC 6901) within the schema document."""

    node: Any = document
    for part in reference[2:].split("/"):
        # RFC 6901: ~1 encodes "/", ~0 encodes "~"; decode in this order.
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, Mapping) or key not in node:
            return None
        node = node[key]
    return node if isinstance(node, Mapping) else None


def _deref(
    schema: Mapping[str, Any], document: Mapping[str, Any] | None
) -> Mapping[str, Any]:
    """Resolve a local ``$ref`` node to its target, or return the node itself.

    Cycle and depth safety come from the callers' ``seen`` guards: a chain of
    references eventually revisits the same schema object, whose ``id`` is
    already recorded.
    """

    if document is None:
        return schema
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/"):
        resolved = _resolve_local_ref(reference, document)
        if resolved is not None:
            return resolved
    return schema


def is_sensitive(
    name: str,
    property_schema: Mapping[str, Any],
    document: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether one property looks like a credential the CLI must not echo.

    Detection is deliberately conservative: a schema declaration wins, and the
    name markers match hyphenated and spaced variants (``X-Api-Key``,
    ``access-key``), so a public field like ``authorization_url`` may be
    over-masked rather than risk showing a credential.
    """

    if is_declared_secret(property_schema, document):
        return True
    lowered = name.lower()
    normalized = lowered.replace("-", "_").replace(" ", "_")
    return any(
        marker in lowered or marker in normalized for marker in _SENSITIVE_NAME_MARKERS
    )


def template_for(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Build the deterministic, schema-shaped input skeleton.

    Required properties always appear. Optional properties appear only when
    the schema declares a value for them (``examples``, ``default``,
    ``const``, or ``enum``). Required nested objects are expanded recursively;
    required array items are never fabricated. The result is not guaranteed to
    pass validation unchanged: the validator is the guide for completing it.
    """

    return _template_object(_deref(schema, schema), depth=0, seen=(), document=schema)


def render_summary_lines(identity: str, kind: str, schema: Mapping[str, Any]) -> list[str]:
    """Render the human field summary printed to stderr in template mode."""

    summaries = field_summaries(schema)
    lines = [f"Inputs for {identity} ({kind}):"]
    if not summaries:
        lines.append("  (no declared properties)")
        return lines
    flattened = _flatten(summaries)
    width = max(len(field.name) for field in flattened) + 2
    type_width = max(len(field.type) for field in flattened) + 2
    for summary in summaries:
        _render_summary_field(lines, summary, width, type_width=type_width, indent=2)
    return lines


def _field_summaries(
    schema: Mapping[str, Any],
    *,
    depth: int,
    seen: tuple[int, ...],
    document: Mapping[str, Any],
) -> tuple[FieldSummary, ...]:
    if depth >= _MAX_SUMMARY_DEPTH:
        return ()
    summaries: list[FieldSummary] = []
    required = _required_names(schema)
    for name, property_schema in _properties(schema).items():
        if not isinstance(name, str) or not isinstance(property_schema, Mapping):
            continue
        property_schema = _deref(property_schema, document)
        nested: tuple[FieldSummary, ...] = ()
        if name in required and _schema_type(property_schema) == "object":
            marker = id(property_schema)
            if marker not in seen:
                nested = _field_summaries(
                    property_schema,
                    depth=depth + 1,
                    seen=(*seen, marker),
                    document=document,
                )
        summaries.append(
            FieldSummary(
                name=name,
                type=_schema_type(property_schema),
                required=name in required,
                sensitive=is_sensitive(name, property_schema, document),
                has_default="default" in property_schema,
                default=property_schema.get("default"),
                has_const="const" in property_schema,
                const=property_schema.get("const"),
                enum=_json_values(property_schema.get("enum")),
                examples=_json_values(property_schema.get("examples")),
                format=_optional_str(property_schema.get("format")),
                constraints=_constraints(property_schema),
                description=_optional_str(property_schema.get("description")),
                nested=nested,
            )
        )
    return tuple(summaries)


def _template_object(
    schema: Mapping[str, Any],
    *,
    depth: int,
    seen: tuple[int, ...],
    document: Mapping[str, Any],
) -> dict[str, Any]:
    template: dict[str, Any] = {}
    if depth >= _MAX_TEMPLATE_DEPTH:
        return template
    required = _required_names(schema)
    for name, property_schema in _properties(schema).items():
        if not isinstance(name, str) or not isinstance(property_schema, Mapping):
            continue
        marker = id(property_schema)
        if marker in seen:
            continue
        entry = _template_entry(
            name,
            property_schema,
            required=name in required,
            depth=depth,
            seen=(*seen, marker),
            document=document,
        )
        if entry is not _UNSET:
            template[name] = entry
    return template


def _template_entry(
    name: str,
    property_schema: Mapping[str, Any],
    *,
    required: bool,
    depth: int,
    seen: tuple[int, ...],
    document: Mapping[str, Any],
) -> Any:
    """Pick one property's template value, or ``_UNSET`` to omit the property."""

    property_schema = _deref(property_schema, document)
    sensitive = is_sensitive(name, property_schema, document)
    if sensitive:
        # Never place a declared value of a credential-shaped field into the
        # template, even though the schema declares it; a required sensitive
        # property still appears as a typed empty value.
        return _typed_empty(_schema_type(property_schema)) if required else _UNSET
    if required:
        return _template_value(property_schema, depth=depth + 1, seen=seen, document=document)
    declared = _declared_value(property_schema)
    return declared


def _template_value(
    property_schema: Mapping[str, Any],
    *,
    depth: int,
    seen: tuple[int, ...],
    document: Mapping[str, Any],
) -> Any:
    """Pick one deterministic skeleton value for a required property."""

    declared = _declared_value(property_schema)
    if declared is not _UNSET:
        return declared
    declared_format = property_schema.get("format")
    if isinstance(declared_format, str) and declared_format in _FORMAT_HINTS:
        return _FORMAT_HINTS[declared_format]
    schema_type = _schema_type(property_schema)
    if schema_type == "object":
        return _template_object(property_schema, depth=depth, seen=seen, document=document)
    return _typed_empty(schema_type)


def _declared_value(property_schema: Mapping[str, Any]) -> Any:
    """Return the first declared value: examples[0], default, const, enum[0]."""

    for key in ("examples", "default", "const", "enum"):
        if key not in property_schema:
            continue
        value = property_schema[key]
        if key in {"examples", "enum"}:
            if isinstance(value, list) and value and _is_json_native(value[0]):
                return value[0]
        elif _is_json_native(value):
            return value
    return _UNSET


def _typed_empty(schema_type: str) -> Any:
    if "|" in schema_type:
        # A union uses its first non-null member's empty value.
        for member in schema_type.split("|"):
            if member and member != "null":
                schema_type = member
                break
    if schema_type in {"string", "-"}:
        return ""
    if schema_type in {"integer", "number"}:
        return 0
    if schema_type == "boolean":
        return False
    if schema_type == "array":
        return []
    if schema_type == "object":
        return {}
    return None


def _schema_type(property_schema: Mapping[str, Any]) -> str:
    """Render the declared type label, joining union members."""

    declared = property_schema.get("type")
    types: Sequence[Any]
    if isinstance(declared, list):
        types = declared
    elif isinstance(declared, str):
        types = [declared]
    elif "properties" in property_schema or "required" in property_schema:
        return "object"
    elif "items" in property_schema:
        return "array"
    else:
        return "-"
    rendered = [str(item) for item in types if item != "null"]
    if not rendered:
        return "null"
    return "|".join(rendered)


def _constraints(property_schema: Mapping[str, Any]) -> tuple[str, ...]:
    constraints: list[str] = []
    for key in _CONSTRAINT_KEYS:
        value = property_schema.get(key)
        if isinstance(value, (int, float, str)) and not isinstance(value, bool):
            constraints.append(f"{key} {value}")
    return tuple(constraints)


def _properties(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    properties = schema.get("properties", {})
    return properties if isinstance(properties, Mapping) else {}


def _required_names(schema: Mapping[str, Any]) -> set[str]:
    required = schema.get("required", [])
    if not isinstance(required, list):
        return set()
    return {name for name in required if isinstance(name, str)}


def _json_values(value: Any) -> tuple[Any, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if _is_json_native(item))


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _is_json_native(value: Any) -> bool:
    if value is None or isinstance(value, str | bool | int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_native(item) for item in value)
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _is_json_native(item) for key, item in value.items()
        )
    return False


def _flatten(summaries: tuple[FieldSummary, ...]) -> list[FieldSummary]:
    flattened: list[FieldSummary] = []
    for summary in summaries:
        flattened.append(summary)
        flattened.extend(_flatten(summary.nested))
    return flattened


def _render_summary_field(
    lines: list[str],
    summary: FieldSummary,
    width: int,
    *,
    type_width: int = _TYPE_COLUMN_WIDTH,
    indent: int,
) -> None:
    parts = _summary_parts(summary)
    prefix = " " * indent + f"{summary.name:<{width}}"
    type_label = f"{summary.type:<{type_width}}"
    lines.append(f"{prefix}{type_label}{', '.join(parts)}")
    if summary.description is not None:
        lines.append(" " * (indent + width + type_width) + summary.description)
    for nested in summary.nested:
        _render_summary_field(lines, nested, width, type_width=type_width, indent=indent + 2)


def _summary_parts(summary: FieldSummary) -> list[str]:
    """Build one property's summary fragments, masking sensitive values."""

    parts: list[str] = ["required" if summary.required else "optional"]
    if summary.has_default:
        rendered = _REDACTED if summary.sensitive else _render_value(summary.default)
        parts.append(f"default {rendered}")
    if summary.has_const:
        rendered = _REDACTED if summary.sensitive else _render_value(summary.const)
        parts.append(f"const {rendered}")
    if summary.enum:
        parts.append(_rendered_list("one of", summary.enum, summary.sensitive))
    if summary.examples:
        parts.append(_rendered_list("examples", summary.examples, summary.sensitive))
    if summary.format is not None:
        parts.append(f"format {summary.format}")
    if summary.constraints:
        parts.append(", ".join(summary.constraints))
    if summary.sensitive:
        parts.append("sensitive")
    return parts


def _rendered_list(label: str, values: tuple[Any, ...], sensitive: bool) -> str:
    """Render one enum or examples fragment, masked when sensitive."""

    if sensitive:
        return f"{label}: {_REDACTED}"
    rendered = ", ".join(_render_plain(item) for item in values)
    return f"{label}: {rendered}"


_REDACTED = "***"


def _render_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render_plain(value: Any) -> str:
    """Render list members readably: plain strings, JSON for everything else."""

    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)
