"""Contract drift validation between declared schemas and typed Python steps."""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from fractions import Fraction
from typing import Any, Literal, TypeGuard

from pydantic import BaseModel, PydanticUserError
from syntara_plugin.contracts import canonical_json_digest

from .bindings import StepBinding, StepBindingError, StepBindings, StepTaxonomy
from .step import BaseStep

__all__ = [
    "DriftCode",
    "DriftIssue",
    "DriftReport",
    "DriftSeverity",
    "EntryPointResolver",
    "SchemaDriftError",
    "check_plugin_bindings",
    "check_step_contract",
    "check_target_binding",
    "compare_model_to_schema",
    "validate_plugin_bindings",
]

Contract = Literal["input", "output", "binding"]
Direction = Literal["input", "output"]
EntryPointResolver = Callable[[str], type[BaseStep[Any, Any]]]

_MAX_MESSAGE_LENGTH = 512
_MAX_ERROR_TEXT_LENGTH = 4_000
_MAX_PATH_LENGTH = 512
_MAX_COMPARISON_DEPTH = 64


class DriftSeverity(StrEnum):
    """Whether one drift issue fails validation on its own or only under strict mode."""

    ERROR = "error"
    WARNING = "warning"


class DriftCode(StrEnum):
    """Stable machine-readable codes for every contract drift this module reports."""

    BINDING_MISSING = "BINDING_MISSING"
    BINDING_UNDECLARED = "BINDING_UNDECLARED"
    TAXONOMY_MISMATCH = "TAXONOMY_MISMATCH"
    MODEL_UNRESOLVED = "MODEL_UNRESOLVED"
    ENTRY_POINT_NOT_DECLARED = "ENTRY_POINT_NOT_DECLARED"
    ENTRY_POINT_MISMATCH = "ENTRY_POINT_MISMATCH"
    ENTRY_POINT_UNRESOLVABLE = "ENTRY_POINT_UNRESOLVABLE"
    SCHEMA_UNAVAILABLE = "SCHEMA_UNAVAILABLE"
    FIELD_MISSING_FROM_MODEL = "FIELD_MISSING_FROM_MODEL"
    FIELD_MISSING_FROM_SCHEMA = "FIELD_MISSING_FROM_SCHEMA"
    REQUIRED_MISMATCH = "REQUIRED_MISMATCH"
    TYPE_INCOMPATIBLE = "TYPE_INCOMPATIBLE"
    ENUM_INCOMPATIBLE = "ENUM_INCOMPATIBLE"
    CONSTRAINT_INCOMPATIBLE = "CONSTRAINT_INCOMPATIBLE"
    ADDITIONAL_PROPERTIES_INCOMPATIBLE = "ADDITIONAL_PROPERTIES_INCOMPATIBLE"
    CONSTRAINT_UNVERIFIABLE = "CONSTRAINT_UNVERIFIABLE"
    SCHEMA_UNVERIFIABLE = "SCHEMA_UNVERIFIABLE"
    DEFAULT_MISMATCH = "DEFAULT_MISMATCH"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    IDENTITY_INVALID = "IDENTITY_INVALID"
    TAXONOMY_INVALID = "TAXONOMY_INVALID"


@dataclass(frozen=True)
class DriftIssue:
    """One bounded, actionable difference between a declared schema and a model.

    Messages and remediations describe the schema shapes involved; they never
    embed invocation input values and are length-capped so a failing report
    stays safe to transport and to read.
    """

    code: DriftCode
    severity: DriftSeverity
    contract: Contract
    path: str
    message: str
    remediation: str

    def __post_init__(self) -> None:
        """Normalize enum values and bound the path and human-readable text."""

        object.__setattr__(self, "path", self.path[:_MAX_PATH_LENGTH])
        object.__setattr__(self, "code", DriftCode(self.code))
        object.__setattr__(self, "severity", DriftSeverity(self.severity))
        message = self.message[:_MAX_MESSAGE_LENGTH]
        remediation = self.remediation[:_MAX_MESSAGE_LENGTH]
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "remediation", remediation)

    @property
    def sort_key(self) -> tuple[str, str, str, str, str]:
        """Return the deterministic ordering key for reports and error text."""

        return (self.contract, self.path, str(self.code), str(self.severity), self.message)


@dataclass(frozen=True)
class DriftReport:
    """The validation outcome for one step identity.

    The schema digests record which declared schemas were checked so a report
    can later be tied to an installed step revision without re-reading them.
    Issues are deduplicated and sorted deterministically at construction.
    """

    step_identity: str | None
    version: str | None
    input_schema_digest: str | None
    output_schema_digest: str | None
    issues: tuple[DriftIssue, ...] = ()

    def __post_init__(self) -> None:
        """Sort and deduplicate issues into a stable order."""

        unique: dict[tuple[str, ...], DriftIssue] = {}
        for issue in self.issues:
            unique.setdefault(
                (str(issue.code), str(issue.severity), issue.contract, issue.path, issue.message),
                issue,
            )
        ordered = tuple(sorted(unique.values(), key=lambda issue: issue.sort_key))
        object.__setattr__(self, "issues", ordered)

    @property
    def errors(self) -> tuple[DriftIssue, ...]:
        """Return the issues that fail validation regardless of strictness."""

        return tuple(issue for issue in self.issues if issue.severity is DriftSeverity.ERROR)

    @property
    def warnings(self) -> tuple[DriftIssue, ...]:
        """Return the issues that only fail validation under strict mode."""

        return tuple(issue for issue in self.issues if issue.severity is DriftSeverity.WARNING)

    def passed(self, strict: bool = False) -> bool:
        """Return whether the report succeeds; strict mode also rejects warnings."""

        if self.errors:
            return False
        return not strict or not self.warnings

    def raise_for_drift(self, strict: bool = False) -> None:
        """Raise :class:`SchemaDriftError` when the report does not pass."""

        if not self.passed(strict):
            raise SchemaDriftError(self)


class SchemaDriftError(Exception):
    """Raised when typed Python steps are incompatible with their declared schemas.

    ``report`` carries one :class:`DriftReport` or a tuple of them when several
    steps were checked together. The rendered message lists one bounded line
    per issue and never contains invocation values.
    """

    def __init__(self, report: DriftReport | tuple[DriftReport, ...]) -> None:
        self.report = report
        super().__init__(_format_reports(report))


def _format_reports(report: DriftReport | tuple[DriftReport, ...]) -> str:
    """Render one bounded block per report with severity and fix per issue."""

    reports = report if isinstance(report, tuple) else (report,)
    blocks: list[str] = []
    for single in reports:
        if not single.issues:
            continue
        counts = f"{len(single.errors)} error(s), {len(single.warnings)} warning(s)"
        lines = [f"contract drift in {single.step_identity or '<unknown step>'} ({counts}):"]
        for issue in single.issues:
            lines.append(
                f"  {issue.severity.value.upper():<7} {issue.contract:<6} "
                f"{issue.path or '<root>'}: {issue.message}"
            )
            lines.append(f"  {'':<7} {'':<6} fix: {issue.remediation}")
        blocks.append("\n".join(lines))
    text = "\n".join(blocks) if blocks else "contract validation failed"
    if len(text) > _MAX_ERROR_TEXT_LENGTH:
        text = text[: _MAX_ERROR_TEXT_LENGTH - 3].rstrip() + "..."
    return text


_ANNOTATION_KEYWORDS = frozenset(
    {
        "title",
        "description",
        "examples",
        "deprecated",
        "$comment",
        "readOnly",
        "writeOnly",
        "$id",
        "$schema",
        "$anchor",
        "$vocabulary",
        "definitions",
    }
)
_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "oneOf",
        "allOf",
        "not",
        "if",
        "then",
        "else",
        "anyOf",
        "patternProperties",
        "dependentSchemas",
        "dependentRequired",
        "propertyNames",
        "unevaluatedProperties",
        "unevaluatedItems",
        "$dynamicRef",
        "$dynamicAnchor",
        "contains",
        "minContains",
        "maxContains",
        "minProperties",
        "maxProperties",
    }
)
_LOWER_BOUND_KEYWORDS = ("minLength", "minItems")
_SCHEMA_VALUED_KEYWORDS = frozenset(
    {
        "items",
        "not",
        "if",
        "then",
        "else",
        "additionalProperties",
        "propertyNames",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
_SCHEMA_LIST_KEYWORDS = frozenset({"anyOf", "oneOf", "allOf", "prefixItems"})
_NAME_TO_SCHEMA_KEYWORDS = frozenset(
    {"properties", "patternProperties", "dependentSchemas", "$defs", "definitions"}
)
_UPPER_BOUND_KEYWORDS = ("maxLength", "maxItems")


def _normalize_schema(schema: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split one raw schema into a normalized body and its local definitions.

    Annotation-only keywords are dropped, ``const`` is folded into ``enum``,
    and ``anyOf: [X, {"type": "null"}]`` is collapsed into ``X`` plus a
    ``x-nullable`` marker, so the comparison engine only sees constraint
    keywords. Local ``$defs``/``definitions`` are returned separately because
    recursive models cannot be inlined.
    """

    defs: dict[str, Any] = {}
    body: dict[str, Any] = {}
    for key, value in schema.items():
        if key in ("$defs", "definitions") and isinstance(value, Mapping):
            defs.update({name: _normalize_node(item) for name, item in value.items()})
        else:
            body[key] = value
    return _normalize_node(body), defs


def _normalize_node(node: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one schema node without touching data-valued keywords.

    Annotation keywords are schema-node noise, but identical names such as a
    ``title`` property must survive inside ``properties`` and ``$defs``
    mappings, and ``enum``/``const`` members are data rather than schemas. The
    normalizer is therefore keyword-aware: only schema-valued keywords recurse.
    """

    result = {
        key: _normalize_keyword(key, value)
        for key, value in node.items()
        if key not in _ANNOTATION_KEYWORDS
    }
    normalized = _collapse_nullable(result)
    prefix = normalized.get("prefixItems")
    if (
        normalized.get("items") is False
        and isinstance(prefix, list)
        and not _bound_number(normalized.get("maxItems"))
    ):
        normalized["maxItems"] = len(prefix)
    return normalized


def _normalize_keyword(key: str, value: Any) -> Any:
    """Normalize one keyword's value, recursing only into schema-valued keywords."""

    if key in _NAME_TO_SCHEMA_KEYWORDS and isinstance(value, Mapping):
        return {
            name: _normalize_node(item) if isinstance(item, Mapping) else item
            for name, item in value.items()
        }
    if key in _SCHEMA_VALUED_KEYWORDS:
        return _normalize_node(value) if isinstance(value, Mapping) else value
    if key in _SCHEMA_LIST_KEYWORDS:
        return [_normalize_node(item) if isinstance(item, Mapping) else item for item in value]
    return value


def _is_null_schema(node: Any) -> bool:
    """Return whether one schema fragment accepts only the null value."""

    if not isinstance(node, Mapping):
        return False
    node_type = node.get("type")
    return node_type == "null" or node_type == ["null"]


def _fold_const(node: dict[str, Any]) -> dict[str, Any]:
    """Fold ``const`` into the enum: the intersection when both are present."""

    const_value = node["const"]
    folded = {key: value for key, value in node.items() if key != "const"}
    if isinstance(folded.get("enum"), list):
        const_digest = canonical_json_digest(const_value)
        folded["enum"] = [
            value for value in folded["enum"] if canonical_json_digest(value) == const_digest
        ]
    else:
        folded["enum"] = [const_value]
    return folded


def _collapse_nullable(node: dict[str, Any]) -> dict[str, Any]:
    """Fold ``const`` into a single-value enum and collapse ``anyOf: [X, null]``.

    When a node carries both ``const`` and ``enum``, JSON Schema requires both
    to hold, so the effective value set is their intersection: empty when the
    const is not among the enum members, the const alone otherwise.

    Merging follows the branch shape: ``type`` gains ``null``, ``enum`` gains
    ``None``, and a ``$ref`` branch keeps its reference and is marked
    ``x-nullable`` so the comparison resolves nullability after dereferencing.
    """

    if "const" in node:
        node = _fold_const(node)
    branches = node.get("anyOf")
    if not isinstance(branches, list) or len(branches) != 2:
        return node
    null_index = next(
        (index for index, branch in enumerate(branches) if _is_null_schema(branch)), None
    )
    if null_index is None:
        return node
    other = branches[1 - null_index]
    if not isinstance(other, Mapping):
        return node
    merged = dict(node)
    del merged["anyOf"]
    for key, value in other.items():
        if key == "type":
            types = value if isinstance(value, list) else [value]
            merged["type"] = sorted({*types, "null"})
        elif key == "enum":
            merged["enum"] = [*value, None]
        else:
            merged[key] = value
    if "$ref" in other:
        merged["x-nullable"] = True
    return merged


def _pointer_escape(name: str) -> str:
    """Escape one property name as an RFC 6901 JSON pointer segment."""

    return name.replace("~", "~0").replace("/", "~1")


def _local_ref_name(reference: str) -> str | None:
    """Return the definition name of a local ``#/$defs/...`` reference."""

    for prefix in ("#/$defs/", "#/definitions/"):
        if reference.startswith(prefix):
            return reference[len(prefix) :]
    return None


def _resolve(node: Any, defs: Mapping[str, Any]) -> tuple[Any, str | None, bool]:
    """Dereference one local ``$ref`` node against its definitions.

    Returns the node, its definition name when it resolved, and the ref-site
    nullability. Unresolvable references stay visible so the comparison
    reports them as unverifiable instead of guessing.
    """

    if not (isinstance(node, Mapping) and isinstance(node.get("$ref"), str)):
        return node, None, False
    name = _local_ref_name(node["$ref"])
    nullable = bool(node.get("x-nullable"))
    if name is None or name not in defs:
        return node, None, nullable
    body = defs[name]
    if nullable and isinstance(body, Mapping):
        body = {**body, "x-nullable": True}
    return body, name, nullable


def _types(node: Mapping[str, Any]) -> frozenset[str] | None:
    """Return the accepted type names, with ``None`` meaning unconstrained."""

    value = node.get("type")
    types: set[str] = set()
    if isinstance(value, str):
        types.add(value)
    elif isinstance(value, list):
        types.update(str(item) for item in value)
    if not types:
        return None
    if node.get("x-nullable"):
        types.add("null")
    return frozenset(types)


def _enum_values(node: Mapping[str, Any]) -> frozenset[str] | None:
    """Return digest identities for enum members, with ``None`` meaning absent."""

    value = node.get("enum")
    if not isinstance(value, list):
        return None
    members = list(value)
    if node.get("x-nullable"):
        members.append(None)
    return frozenset(canonical_json_digest(member) for member in members)


def _covers(source: frozenset[str] | None, target: frozenset[str] | None) -> bool:
    """Return whether every value of the source types is accepted by the target.

    An absent type list accepts everything, and JSON Schema ``integer`` values
    are a subset of ``number`` values, matching how validators treat them.
    """

    if target is None:
        return True
    if source is None:
        return False
    return all(
        any(name == other or (name == "integer" and other == "number") for other in target)
        for name in source
    )


@dataclass
class _Comparison:
    """Mutable state for one direction of a model-versus-schema comparison."""

    direction: Direction
    issues: list[DriftIssue] = field(default_factory=list)
    depth: int = 0
    visited_refs: set[tuple[object, ...]] = field(default_factory=set)
    declared_defs: Mapping[str, Any] = field(default_factory=dict)
    model_defs: Mapping[str, Any] = field(default_factory=dict)

    def add(
        self,
        code: DriftCode,
        severity: DriftSeverity,
        path: str,
        message: str,
        remediation: str,
    ) -> None:
        """Record one issue attributed to the direction being compared."""

        contract: Contract = self.direction
        self.issues.append(DriftIssue(code, severity, contract, path, message, remediation))


def _node_digest(node: Any) -> str:
    """Return a stable content identity for an inline (non-reference) node."""

    try:
        return str(canonical_json_digest(node))
    except Exception:  # pragma: no cover - defensive: node must stay JSON-like
        return str(id(node))


def _skips_false_schema(declared: Any, model: Any, path: str, comparison: _Comparison) -> bool:
    """Return whether a false schema ends the comparison, reporting it on output."""

    if declared is not False and model is not False:
        return False
    if comparison.direction == "input":
        return True
    comparison.add(
        DriftCode.SCHEMA_UNVERIFIABLE,
        DriftSeverity.WARNING,
        path,
        "a false schema cannot be compared against model output",
        "describe the value with a type instead of a false schema.",
    )
    return True


def _already_compared(
    declared: Any,
    model: Any,
    declared_identity: tuple[str | None, bool],
    model_identity: tuple[str | None, bool],
    comparison: _Comparison,
) -> bool:
    """Return whether this node pair was compared before, recording it otherwise.

    The memo keys on whichever side came from a reference; inline nodes use a
    content digest so recursion against an inline counterpart still ends.
    """

    declared_ref, declared_nullable = declared_identity
    model_ref, model_nullable = model_identity
    if declared_ref is None and model_ref is None:
        return False
    key = (
        declared_ref if declared_ref is not None else _node_digest(declared),
        model_ref if model_ref is not None else _node_digest(model),
        declared_nullable,
        model_nullable,
    )
    if key in comparison.visited_refs:
        return True
    comparison.visited_refs.add(key)
    return False


def _comparable_shape(declared: Any, model: Any, path: str, comparison: _Comparison) -> bool:
    """Return whether both nodes are comparable mappings, reporting why not."""

    if not isinstance(declared, Mapping) or not isinstance(model, Mapping):
        return False
    unsupported = ({*declared} & _UNSUPPORTED_KEYWORDS) | ({*model} & _UNSUPPORTED_KEYWORDS)
    if unsupported:
        comparison.add(
            DriftCode.SCHEMA_UNVERIFIABLE,
            DriftSeverity.WARNING,
            path,
            f"uses unsupported schema keywords: {', '.join(sorted(unsupported))}",
            "simplify both schemas to the comparable keyword subset or align them by hand.",
        )
        return False
    if "$ref" in declared or "$ref" in model:
        comparison.add(
            DriftCode.SCHEMA_UNVERIFIABLE,
            DriftSeverity.WARNING,
            path,
            "an external or unresolvable reference cannot be compared",
            "inline the referenced schema or keep both references local.",
        )
        return False
    return True


def _compare(declared: Any, model: Any, path: str, comparison: _Comparison) -> None:
    """Compare one declared schema fragment against its model counterpart.

    Input direction proves that every manifest-valid value builds the model
    (declared ⊆ model); output direction proves that every model-emittable
    value satisfies the declared schema (model ⊆ declared). Local references
    are dereferenced on the fly and a memo on reference pairs stops recursive
    models from looping.
    """

    declared, declared_ref, declared_nullable = _resolve(declared, comparison.declared_defs)
    model, model_ref, model_nullable = _resolve(model, comparison.model_defs)
    if declared is True:
        declared = {}
    if model is True:
        model = {}
    if _skips_false_schema(declared, model, path, comparison):
        return
    if _already_compared(
        declared,
        model,
        (declared_ref, declared_nullable),
        (model_ref, model_nullable),
        comparison,
    ):
        return
    if comparison.depth >= _MAX_COMPARISON_DEPTH:
        comparison.add(
            DriftCode.SCHEMA_UNVERIFIABLE,
            DriftSeverity.WARNING,
            path,
            "schema nesting is too deep to compare",
            "flatten the deeply nested schemas or compare them by hand.",
        )
        return
    if not _comparable_shape(declared, model, path, comparison):
        return
    if _accepts_nothing(declared):
        if comparison.direction == "output":
            _compare_enums(declared, model, path, comparison)
        return
    if _accepts_nothing(model):
        if comparison.direction == "input":
            _compare_enums(declared, model, path, comparison)
        return
    comparison.depth += 1
    try:
        _compare_types(declared, model, path, comparison)
        _compare_enums(declared, model, path, comparison)
        _compare_defaults(declared, model, path, comparison)
        _compare_constraints(declared, model, path, comparison)
        _compare_properties(declared, model, path, comparison)
        _compare_items(declared, model, path, comparison)
        _compare_additional(declared, model, path, comparison)
    finally:
        comparison.depth -= 1


def _compare_defaults(
    declared: Mapping[str, Any], model: Mapping[str, Any], path: str, comparison: _Comparison
) -> None:
    """Compare default values, which the runtime emits for unset fields.

    ``default`` is annotation-only in JSON Schema, so it never gates value
    validation, but the container adapter emits the model's default when a
    field is unset and the platform pre-fills the declared default. A default
    outside the other side's enum is therefore a real emission or build
    failure, and differing defaults are informational drift.
    """

    if "default" in model and comparison.direction == "output":
        _compare_emitted_default(model.get("default"), declared, path, comparison)
    if "default" in declared and comparison.direction == "input":
        _compare_declared_default(declared.get("default"), model, path, comparison)
    if "default" in declared and "default" in model:
        declared_default = canonical_json_digest(declared.get("default"))
        model_default = canonical_json_digest(model.get("default"))
        if declared_default != model_default:
            comparison.add(
                DriftCode.DEFAULT_MISMATCH,
                DriftSeverity.WARNING,
                path,
                "the manifest and the model declare different default values",
                "align the default in the manifest schema and the model field.",
            )


def _compare_emitted_default(
    model_default: Any, declared: Mapping[str, Any], path: str, comparison: _Comparison
) -> None:
    """Check the model's default against the declared output schema."""

    violation = _default_violation(model_default, declared)
    if violation is not None:
        comparison.add(
            DriftCode.CONSTRAINT_INCOMPATIBLE,
            DriftSeverity.ERROR,
            path,
            f"the model's default value violates the declared {violation}",
            "give the model a default the declared schema accepts.",
        )
    declared_enum = _enum_values(declared)
    if declared_enum is not None and canonical_json_digest(model_default) not in declared_enum:
        comparison.add(
            DriftCode.ENUM_INCOMPATIBLE,
            DriftSeverity.ERROR,
            path,
            "the model's default value is outside the declared enum",
            "give the model a default the declared schema accepts.",
        )


def _compare_declared_default(
    declared_default: Any, model: Mapping[str, Any], path: str, comparison: _Comparison
) -> None:
    """Check the manifest's default against the model's input schema."""

    violation = _default_violation(declared_default, model)
    if violation is not None:
        comparison.add(
            DriftCode.CONSTRAINT_INCOMPATIBLE,
            DriftSeverity.ERROR,
            path,
            f"the manifest's default value violates the model's {violation}",
            "declare a default the model accepts, or widen the model.",
        )
    model_enum = _enum_values(model)
    if model_enum is not None and canonical_json_digest(declared_default) not in model_enum:
        comparison.add(
            DriftCode.ENUM_INCOMPATIBLE,
            DriftSeverity.ERROR,
            path,
            "the manifest's default value cannot build the model",
            "declare a default the model accepts, or widen the model.",
        )


def _default_violation(default_value: Any, constraints: Mapping[str, Any]) -> str | None:
    """Return the constraint keyword a default value violates, if any."""

    if _bound_number(default_value):
        return _numeric_default_violation(default_value, constraints)
    if isinstance(default_value, str):
        return _length_default_violation(len(default_value), constraints, "minLength", "maxLength")
    if isinstance(default_value, list):
        return _length_default_violation(len(default_value), constraints, "minItems", "maxItems")
    return None


def _numeric_default_violation(value: float, constraints: Mapping[str, Any]) -> str | None:
    """Return the numeric constraint keyword a default value violates."""

    minimum = constraints.get("minimum")
    if _bound_number(minimum) and value < minimum:
        return "minimum"
    exclusive_minimum = constraints.get("exclusiveMinimum")
    if _bound_number(exclusive_minimum) and value <= exclusive_minimum:
        return "exclusiveMinimum"
    maximum = constraints.get("maximum")
    if _bound_number(maximum) and value > maximum:
        return "maximum"
    exclusive_maximum = constraints.get("exclusiveMaximum")
    if _bound_number(exclusive_maximum) and value >= exclusive_maximum:
        return "exclusiveMaximum"
    multiple = constraints.get("multipleOf")
    if _bound_number(multiple) and multiple != 0 and not _is_multiple(value, multiple):
        return "multipleOf"
    return None


def _length_default_violation(
    length: int, constraints: Mapping[str, Any], minimum_keyword: str, maximum_keyword: str
) -> str | None:
    """Return the length constraint keyword a default value violates."""

    minimum = constraints.get(minimum_keyword)
    if _bound_number(minimum) and length < minimum:
        return minimum_keyword
    maximum = constraints.get(maximum_keyword)
    if _bound_number(maximum) and length > maximum:
        return maximum_keyword
    return None


def _accepts_nothing(node: Mapping[str, Any]) -> bool:
    """Return whether the node's effective enum is empty, accepting no value."""

    values = _enum_values(node)
    return values is not None and not values


def _compare_types(
    declared: Mapping[str, Any], model: Mapping[str, Any], path: str, comparison: _Comparison
) -> None:
    """Check that the consuming side accepts every type the producing side allows."""

    declared_types = _types(declared) or _enum_type_names(declared)
    model_types = _types(model) or _enum_type_names(model)
    if comparison.direction == "input":
        compatible = _covers(declared_types, model_types)
        message = (
            f"manifest type {_render_types(declared_types)} is not accepted by the model type"
            f" {_render_types(model_types)}"
        )
    else:
        compatible = _covers(model_types, declared_types)
        message = (
            f"model type {_render_types(model_types)} can violate the manifest type"
            f" {_render_types(declared_types)}"
        )
    if not compatible:
        comparison.add(
            DriftCode.TYPE_INCOMPATIBLE,
            DriftSeverity.ERROR,
            path,
            message,
            "align the manifest schema and the Python model on the same JSON types.",
        )


def _enum_type_names(node: Mapping[str, Any]) -> frozenset[str] | None:
    """Infer type names from enum members when no ``type`` is declared."""

    value = node.get("enum")
    if not isinstance(value, list):
        return None
    names = {_json_type_name(member) for member in value}
    if node.get("x-nullable"):
        names.add("null")
    return frozenset(names) if names else None


def _json_type_name(value: Any) -> str:
    """Return the JSON Schema type name of one enum member."""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _render_types(types: frozenset[str] | None) -> str:
    """Render one bounded type set for messages."""

    if types is None:
        return "any"
    return "|".join(sorted(types))


def _compare_enums(
    declared: Mapping[str, Any], model: Mapping[str, Any], path: str, comparison: _Comparison
) -> None:
    """Check enum membership in the direction that guarantees compatibility."""

    declared_enum = _enum_values(declared)
    model_enum = _enum_values(model)
    if declared_enum is None and model_enum is None:
        return
    if comparison.direction == "input":
        message = _input_enum_drift(declared_enum, model_enum)
    else:
        message = _output_enum_drift(declared_enum, model_enum)
    if message is None:
        return
    comparison.add(
        DriftCode.ENUM_INCOMPATIBLE,
        DriftSeverity.ERROR,
        path,
        message,
        "use the same Literal values in the Pydantic model and the declared schema.",
    )


def _input_enum_drift(
    declared_enum: frozenset[str] | None, model_enum: frozenset[str] | None
) -> str | None:
    """Return the input-direction enum drift, or None when compatible."""

    if declared_enum is not None and model_enum is None:
        return None
    if model_enum is not None and declared_enum is None:
        return "the model restricts values that the declared schema does not restrict"
    if (
        declared_enum is not None
        and model_enum is not None
        and not declared_enum.issubset(model_enum)
    ):
        return "declared enum values are missing from the model"
    return None


def _output_enum_drift(
    declared_enum: frozenset[str] | None, model_enum: frozenset[str] | None
) -> str | None:
    """Return the output-direction enum drift, or None when compatible."""

    if model_enum is not None and declared_enum is None:
        return None
    if declared_enum is not None and model_enum is None:
        return "the declared schema restricts values that the model does not restrict"
    if (
        model_enum is not None
        and declared_enum is not None
        and not model_enum.issubset(declared_enum)
    ):
        return "the model can emit values outside the declared enum"
    return None


def _compare_constraints(
    declared: Mapping[str, Any], model: Mapping[str, Any], path: str, comparison: _Comparison
) -> None:
    """Check numeric, string, and array bounds in the direction of data flow.

    A violation exists when the consuming side enforces a bound the producing
    side does not guarantee: on input the model may not reject manifest-valid
    values, and on output the declared schema may not reject model-emittable
    values. ``pattern`` and ``format`` cannot be proven, so differences are
    warnings instead of errors.
    """

    if comparison.direction == "input":
        producer, consumer = declared, model
    else:
        producer, consumer = model, declared
    _compare_numeric_bound(producer, consumer, "minimum", "exclusiveMinimum", path, comparison)
    _compare_numeric_bound(producer, consumer, "maximum", "exclusiveMaximum", path, comparison)
    for keyword in _LOWER_BOUND_KEYWORDS:
        _compare_bound(producer, consumer, keyword, "minimum", path, comparison)
    for keyword in _UPPER_BOUND_KEYWORDS:
        _compare_bound(producer, consumer, keyword, "maximum", path, comparison)
    _compare_multiple_of(producer, consumer, path, comparison)
    if consumer.get("uniqueItems") is True and producer.get("uniqueItems") is not True:
        comparison.add(
            DriftCode.CONSTRAINT_INCOMPATIBLE,
            DriftSeverity.ERROR,
            path,
            "the consuming side requires unique array items the producing side may repeat",
            "require uniqueItems on both sides or allow duplicates on both sides.",
        )
    _compare_text_formats(declared, model, path, comparison)


def _compare_multiple_of(
    producer: Mapping[str, Any], consumer: Mapping[str, Any], path: str, comparison: _Comparison
) -> None:
    """Check ``multipleOf`` in the direction of data flow.

    Every multiple of the producer's value must also satisfy the consumer's.
    ``multipleOf: 1`` is special-cased: integers always satisfy it, so the
    producer only needs to be integer-typed or constrained to a whole multiple.
    """

    consumer_multiple = consumer.get("multipleOf")
    if not isinstance(consumer_multiple, (int, float)) or consumer_multiple == 0:
        return
    producer_enum = _producer_values(producer)
    if (
        producer_enum is not None
        and all(_bound_number(value) for value in producer_enum)
        and all(_is_multiple(value, consumer_multiple) for value in producer_enum)
    ):
        return
    raw_multiple = producer.get("multipleOf")
    producer_multiple: float | None = raw_multiple if _bound_number(raw_multiple) else None
    if consumer_multiple == 1:
        integers_only = producer.get("type") in ("integer", ["integer"])
        unproven = not integers_only and (
            producer_multiple is None or not _is_multiple(producer_multiple, 1)
        )
    else:
        unproven = producer_multiple is None or not _is_multiple(
            producer_multiple, consumer_multiple
        )
    if unproven:
        comparison.add(
            DriftCode.CONSTRAINT_INCOMPATIBLE,
            DriftSeverity.ERROR,
            path,
            "the consuming side enforces a multipleOf the producing side does not guarantee",
            "align the multipleOf values in both schemas.",
        )


def _compare_text_formats(
    declared: Mapping[str, Any], model: Mapping[str, Any], path: str, comparison: _Comparison
) -> None:
    """Warn when the consuming side's ``pattern``/``format`` is unprovable."""

    for keyword in ("pattern", "format"):
        if comparison.direction == "input":
            consumer_value, producer_value = model.get(keyword), declared.get(keyword)
        else:
            consumer_value, producer_value = declared.get(keyword), model.get(keyword)
        if consumer_value is not None and consumer_value != producer_value:
            comparison.add(
                DriftCode.CONSTRAINT_UNVERIFIABLE,
                DriftSeverity.WARNING,
                path,
                f"the consuming side's {keyword} cannot be verified against the producing side",
                f"use the same {keyword} on both sides.",
            )


def _is_multiple(value: float, divisor: float) -> bool:
    """Return whether every multiple of ``value`` is also a multiple of ``divisor``.

    Decimal string forms keep the comparison exact: binary floats make
    ``0.3 % 0.1`` non-zero even though every multiple of 0.3 is a multiple
    of 0.1 in the decimal sense JSON Schema intends.
    """

    try:
        return Fraction(str(value)) % Fraction(str(divisor)) == 0
    except (ValueError, ZeroDivisionError):
        return False


def _bound_number(value: Any) -> TypeGuard[float]:
    """Return whether one bound keyword holds a usable number."""

    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _effective_numeric_bound(
    node: Mapping[str, Any], inclusive: str, exclusive: str
) -> tuple[float, bool] | None:
    """Combine one side's inclusive and exclusive numeric bounds into the tighter one."""

    candidates: list[tuple[float, bool]] = []
    for keyword, is_exclusive in ((inclusive, False), (exclusive, True)):
        value = node.get(keyword)
        if _bound_number(value):
            candidates.append((value, is_exclusive))
    if not candidates:
        return None
    best = candidates[0]
    for candidate in candidates[1:]:
        value, is_exclusive = candidate
        best_value, best_exclusive = best
        tighter = (value > best_value if inclusive == "minimum" else value < best_value) or (
            value == best_value and is_exclusive and not best_exclusive
        )
        if tighter:
            best = candidate
    return best


def _producer_values(node: Mapping[str, Any]) -> list[Any] | None:
    """Return the node's finite value set when it is fully enumerated."""

    value = node.get("enum")
    if isinstance(value, list) and value:
        return value
    return None


def _enum_satisfies_numeric_bound(
    producer: Mapping[str, Any],
    consumer: Mapping[str, Any],
    inclusive: str,
    exclusive: str,
) -> bool:
    """Return whether every producer enum value satisfies the consumer bound.

    A fully enumerated producer needs no bound keyword of its own: when each
    possible value already meets the consumer's bound, the constraint holds.
    """

    values = _producer_values(producer)
    if values is None or not all(_bound_number(value) for value in values):
        return False
    consumer_bound = _effective_numeric_bound(consumer, inclusive, exclusive)
    if consumer_bound is None:
        return True
    bound, is_exclusive = consumer_bound
    for value in values:
        if inclusive == "minimum":
            if value < bound or (value == bound and is_exclusive):
                return False
        elif value > bound or (value == bound and is_exclusive):
            return False
    return True


def _enum_satisfies_length_bound(
    producer: Mapping[str, Any], consumer: Mapping[str, Any], keyword: str
) -> bool:
    """Return whether every producer enum value satisfies a length bound."""

    values = _producer_values(producer)
    if values is None:
        return False
    wants_string = keyword in ("minLength", "maxLength")
    if wants_string:
        if not all(isinstance(value, str) for value in values):
            return False
    elif not all(isinstance(value, list) for value in values):
        return False
    bound = consumer.get(keyword)
    if not _bound_number(bound):
        return True
    lengths = [len(value) for value in values]
    if keyword in ("minLength", "minItems"):
        return all(length >= bound for length in lengths)
    return all(length <= bound for length in lengths)


def _compare_numeric_bound(
    producer: Mapping[str, Any],
    consumer: Mapping[str, Any],
    inclusive: str,
    exclusive: str,
    path: str,
    comparison: _Comparison,
) -> None:
    """Compare effective numeric bounds, mixing inclusive and exclusive keywords.

    ``exclusiveMinimum: 0`` and ``minimum: 1`` accept overlapping value sets,
    so the keywords are combined into one (value, exclusivity) bound per side
    before asking whether the consumer is stricter than the producer.
    """

    consumer_bound = _effective_numeric_bound(consumer, inclusive, exclusive)
    if consumer_bound is None:
        return
    if _enum_satisfies_numeric_bound(producer, consumer, inclusive, exclusive):
        return
    producer_bound = _effective_numeric_bound(producer, inclusive, exclusive)
    if producer_bound is None:
        stricter = True
    else:
        consumer_value, consumer_exclusive = consumer_bound
        producer_value, producer_exclusive = producer_bound
        if consumer_value == producer_value:
            stricter = consumer_exclusive and not producer_exclusive
        elif inclusive == "minimum":
            stricter = consumer_value > producer_value
        else:
            stricter = consumer_value < producer_value
    if stricter:
        comparison.add(
            DriftCode.CONSTRAINT_INCOMPATIBLE,
            DriftSeverity.ERROR,
            path,
            f"the consuming side enforces a stricter {inclusive} than the producing side"
            " guarantees",
            f"loosen the {inclusive} on the consuming side or tighten it on the producing side.",
        )


def _compare_bound(
    producer: Mapping[str, Any],
    consumer: Mapping[str, Any],
    keyword: str,
    orientation: str,
    path: str,
    comparison: _Comparison,
) -> None:
    """Check one bound keyword where the consumer may be stricter than the producer."""

    if _enum_satisfies_length_bound(producer, consumer, keyword):
        return
    consumer_value = consumer.get(keyword)
    if not isinstance(consumer_value, (int, float)) or isinstance(consumer_value, bool):
        return
    producer_value = producer.get(keyword)
    if not isinstance(producer_value, (int, float)) or isinstance(producer_value, bool):
        stricter = True
    elif orientation == "minimum":
        stricter = consumer_value > producer_value
    else:
        stricter = consumer_value < producer_value
    if stricter:
        comparison.add(
            DriftCode.CONSTRAINT_INCOMPATIBLE,
            DriftSeverity.ERROR,
            path,
            f"the consuming side enforces a stricter {keyword} than the producing side guarantees",
            f"loosen the {keyword} on the consuming side or tighten it on the producing side.",
        )


def _compare_properties(
    declared: Mapping[str, Any], model: Mapping[str, Any], path: str, comparison: _Comparison
) -> None:
    """Compare every property present on either side, including required flags.

    On input, a manifest property the model lacks is an error even when
    optional: Pydantic's default ``extra="ignore"`` silently drops it, and a
    stricter model rejects it. On output the adapter's ``model_dump`` always
    emits every model field, so requiredness reduces to presence plus null
    typing, and a declared-required field backed by an optional model field
    only warns.
    """

    declared_properties = declared.get("properties")
    model_properties = model.get("properties")
    declared_properties = declared_properties if isinstance(declared_properties, Mapping) else {}
    model_properties = model_properties if isinstance(model_properties, Mapping) else {}
    declared_required = set(declared.get("required") or [])
    model_required = set(model.get("required") or [])
    for name in sorted(set(declared_properties) | set(model_properties)):
        child = f"{path}/properties/{_pointer_escape(name)}"
        if name in declared_properties and name not in model_properties:
            _report_missing_from_model(name, name in declared_required, child, comparison)
            continue
        if name in model_properties and name not in declared_properties:
            _report_missing_from_schema(
                name, name in model_required, declared, model_properties[name], child, comparison
            )
            continue
        _compare(declared_properties[name], model_properties[name], child, comparison)
        _compare_requiredness(
            name, name in declared_required, name in model_required, child, comparison
        )


def _report_missing_from_model(
    name: str, declared_required: bool, child: str, comparison: _Comparison
) -> None:
    """Report one manifest property the Python model does not define."""

    if comparison.direction == "input" or declared_required:
        severity = DriftSeverity.ERROR
    else:
        severity = DriftSeverity.WARNING
    if declared_required:
        message = f"the manifest requires `{name}` but the Python model does not define it"
    else:
        message = f"the manifest declares `{name}` but the Python model does not define it"
    comparison.add(
        DriftCode.FIELD_MISSING_FROM_MODEL,
        severity,
        child,
        message,
        f"add `{name}` to the model, or remove it from the manifest schema.",
    )


def _report_missing_from_schema(
    name: str,
    model_required: bool,
    declared: Mapping[str, Any],
    model_property: Any,
    child: str,
    comparison: _Comparison,
) -> None:
    """Report one model property the manifest does not declare.

    When the manifest constrains additional properties with a schema, the
    emitted property is also judged against it, because it arrives as an
    undeclared key at the platform boundary.
    """

    if comparison.direction == "input":
        severity = DriftSeverity.ERROR if model_required else DriftSeverity.WARNING
        if model_required:
            message = f"the model requires `{name}` but the manifest does not declare it"
        else:
            message = f"the model accepts `{name}` but the manifest does not declare it"
    elif declared.get("additionalProperties") is False:
        severity = DriftSeverity.ERROR
        message = (
            f"the model emits `{name}` but the output schema does not declare it"
            " and rejects additional properties"
        )
    else:
        severity = DriftSeverity.WARNING
        message = f"the model emits `{name}` but the output schema does not declare it"
    comparison.add(
        DriftCode.FIELD_MISSING_FROM_SCHEMA,
        severity,
        child,
        message,
        f"declare `{name}` in the manifest schema, or remove it from the model.",
    )
    declared_additional = declared.get("additionalProperties")
    if isinstance(declared_additional, Mapping):
        _compare(declared_additional, model_property, child, comparison)


def _compare_requiredness(
    name: str, declared_needs: bool, model_needs: bool, child: str, comparison: _Comparison
) -> None:
    """Compare the required flags of a property present on both sides."""

    if comparison.direction == "input":
        if model_needs and not declared_needs:
            comparison.add(
                DriftCode.REQUIRED_MISMATCH,
                DriftSeverity.ERROR,
                child,
                "the model requires a property the declared schema leaves optional",
                f"require `{name}` in the declared schema or give the model field a default.",
            )
        elif declared_needs and not model_needs:
            comparison.add(
                DriftCode.REQUIRED_MISMATCH,
                DriftSeverity.WARNING,
                child,
                "the declared schema requires a property the model accepts as optional",
                f"require `{name}` in the model to mirror the declared schema.",
            )
    elif declared_needs and not model_needs:
        comparison.add(
            DriftCode.REQUIRED_MISMATCH,
            DriftSeverity.WARNING,
            child,
            "the model only guarantees a default value for a declared-required property",
            f"make the `{name}` model field required or drop it from the declared schema.",
        )


def _compare_items(
    declared: Mapping[str, Any], model: Mapping[str, Any], path: str, comparison: _Comparison
) -> None:
    """Compare positional prefixItems, then array item schemas.

    prefixItems are compared first so a declared ``items: false`` cannot end
    the comparison before the positional schemas are checked. A declared
    ``items: false`` forbids items beyond ``prefixItems``: no manifest-valid
    value carries free items on input, so the model cannot reject one, while
    a model that can emit items violates it on output — unless its tuple is
    fixed-length. ``items: true`` stays unconstrained and compares normally.
    """

    declared_prefix = declared.get("prefixItems")
    model_prefix = model.get("prefixItems")
    if isinstance(declared_prefix, list) and isinstance(model_prefix, list):
        for index, (declared_item, model_item) in enumerate(
            zip(declared_prefix, model_prefix, strict=False)
        ):
            _compare(declared_item, model_item, f"{path}/prefixItems/{index}", comparison)
        if len(declared_prefix) != len(model_prefix):
            comparison.add(
                DriftCode.CONSTRAINT_UNVERIFIABLE,
                DriftSeverity.WARNING,
                f"{path}/prefixItems",
                "prefixItems lengths differ between the declared schema and the model",
                "use the same positional item schemas on both sides.",
            )
    declared_items = declared.get("items")
    if declared_items is False:
        if comparison.direction == "output" and _can_emit_extra_items(model):
            comparison.add(
                DriftCode.TYPE_INCOMPATIBLE,
                DriftSeverity.ERROR,
                f"{path}/items",
                "the model can emit array items the declared schema forbids",
                "declare the emitted item schema, or cap the array with maxItems.",
            )
        return
    model_items = model.get("items")
    declared_items = declared_items if isinstance(declared_items, Mapping) else {}
    model_items = model_items if isinstance(model_items, Mapping) else {}
    if declared_items or model_items:
        _compare(declared_items, model_items, f"{path}/items", comparison)


def _can_emit_extra_items(model: Mapping[str, Any]) -> bool:
    """Return whether the model schema allows items beyond its prefixItems.

    A fixed-length tuple schema caps itself either with ``items: false`` or
    with ``maxItems`` equal to its ``prefixItems`` length; neither can emit
    an item a declared ``items: false`` forbids.
    """

    if model.get("items") is False:
        return False
    prefix = model.get("prefixItems")
    max_items = model.get("maxItems")
    if isinstance(prefix, list) and _bound_number(max_items) and max_items <= len(prefix):
        return False
    return True


def _compare_additional(
    declared: Mapping[str, Any], model: Mapping[str, Any], path: str, comparison: _Comparison
) -> None:
    """Compare the additional-properties policies of both schemas.

    The model side mirrors Pydantic's ``model_config["extra"]``:
    ``"forbid"`` emits ``additionalProperties: false``, ``"allow"`` emits
    ``true``, and the default ``"ignore"`` emits nothing because unknown
    fields are accepted and dropped at validation time.
    """

    declared_additional = declared.get("additionalProperties")
    model_additional = model.get("additionalProperties")
    if comparison.direction == "input":
        if declared_additional is not False and model_additional is False:
            comparison.add(
                DriftCode.ADDITIONAL_PROPERTIES_INCOMPATIBLE,
                DriftSeverity.ERROR,
                path,
                "the declared schema allows additional properties the model forbids",
                "declare every property in the model or allow extras with extra='allow'.",
            )
    elif (
        declared_additional is False
        and model_additional is not None
        and model_additional is not False
    ):
        comparison.add(
            DriftCode.ADDITIONAL_PROPERTIES_INCOMPATIBLE,
            DriftSeverity.ERROR,
            path,
            "the model can emit additional properties the declared schema forbids",
            "declare the emitted properties or set additionalProperties: true in the manifest.",
        )
    declared_is_schema = isinstance(declared_additional, Mapping)
    model_is_schema = isinstance(model_additional, Mapping)
    if not (declared_is_schema or model_is_schema):
        return
    if comparison.direction == "input" and declared_additional is False:
        return
    if comparison.direction == "output" and model_additional is None:
        return
    _compare(
        declared_additional if declared_is_schema else {},
        model_additional if model_is_schema else {},
        f"{path}/additionalProperties",
        comparison,
    )


def _model_schema(model: type[BaseModel], direction: Direction) -> dict[str, Any]:
    """Return the model's effective JSON Schema for one direction of data flow.

    Input validation accepts alias keys by default, so the validation schema is
    generated ``by_alias=True``. Output emission mirrors the container adapter:
    ``model_dump(mode="json")`` writes field names unless the model sets
    ``serialize_by_alias``, so the serialization schema follows the same
    choice. Keeping this in one helper makes an adapter change easy to follow.
    """

    if direction == "input":
        return dict(model.model_json_schema(mode="validation", by_alias=True))
    by_alias = bool(model.model_config.get("serialize_by_alias", False))
    return dict(model.model_json_schema(mode="serialization", by_alias=by_alias))


def compare_model_to_schema(
    model: type[BaseModel],
    schema: Mapping[str, Any],
    *,
    direction: Direction,
) -> tuple[DriftIssue, ...]:
    """Compare one Pydantic model against one declared JSON Schema.

    ``direction="input"`` proves that every manifest-valid value builds the
    model; ``direction="output"`` proves that every model-emittable value
    satisfies the schema. The declared schema is authoritative: issues point
    at it with JSON pointer paths.
    """

    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        message = "model must be a Pydantic BaseModel subclass"
        raise TypeError(message)
    if not isinstance(schema, Mapping):
        message = "schema must be a JSON Schema mapping"
        raise TypeError(message)
    if direction not in ("input", "output"):
        message = "direction must be 'input' or 'output'"
        raise ValueError(message)
    declared_body, declared_defs = _normalize_schema(schema)
    try:
        model_body, model_defs = _normalize_schema(_model_schema(model, direction))
    except PydanticUserError as error:
        return (
            DriftIssue(
                DriftCode.MODEL_UNRESOLVED,
                DriftSeverity.ERROR,
                direction,
                "",
                f"the model cannot generate a JSON Schema ({type(error).__name__})",
                "annotate the model fields with JSON-representable types.",
            ),
        )
    comparison = _Comparison(
        direction=direction,
        declared_defs=declared_defs,
        model_defs=model_defs,
    )
    _compare(declared_body, model_body, "", comparison)
    return tuple(comparison.issues)


_TARGET_TAXONOMY: Mapping[str, StepTaxonomy] = {
    "Action": StepTaxonomy.ACTION,
    "Task": StepTaxonomy.TASK,
    "Workflow": StepTaxonomy.WORKFLOW,
    "Trigger": StepTaxonomy.TRIGGER,
}


def _target_taxonomy(target: Mapping[str, Any]) -> StepTaxonomy | None:
    """Return the step taxonomy a target declares, when it can be determined.

    The umbrella Step contract carries the taxonomy as target metadata; the
    current Action/Trigger manifests encode it in the kind name. An unknown
    encoding returns None so the taxonomy check is skipped rather than
    guessed.
    """

    declared = target.get("taxonomy")
    if declared is None:
        return _TARGET_TAXONOMY.get(str(target.get("kind")))
    if isinstance(declared, str):
        try:
            return StepTaxonomy(declared)
        except ValueError:
            return None
    return None


def _step_class(step: type[BaseStep[Any, Any]] | BaseStep[Any, Any]) -> type[BaseStep[Any, Any]]:
    """Return the class of a step argument given as a class or an instance."""

    if isinstance(step, type):
        if not issubclass(step, BaseStep):
            message = "step must be a BaseStep implementation or subclass"
            raise TypeError(message)
        return step
    if not isinstance(step, BaseStep):
        message = "step must be a BaseStep implementation or subclass"
        raise TypeError(message)
    return type(step)


def _model_unresolved_issue(step_class: type[BaseStep[Any, Any]], error: TypeError) -> DriftIssue:
    """Build the issue for a step whose generic models could not be resolved."""

    return DriftIssue(
        DriftCode.MODEL_UNRESOLVED,
        DriftSeverity.ERROR,
        "binding",
        "",
        f"{step_class.__qualname__} does not resolve concrete input/output models"
        f" ({str(error)[:120]})",
        "specialize BaseStep[TInput, TOutput] with concrete Pydantic models.",
    )


def check_step_contract(
    step: type[BaseStep[Any, Any]] | BaseStep[Any, Any],
    *,
    input_schema: Mapping[str, Any],
    output_schema: Mapping[str, Any],
    step_identity: str | None = None,
) -> DriftReport:
    """Validate one typed step directly against declared schemas.

    Task, Workflow, and Trigger owners that author their own declared schemas
    call this instead of going through a compiled descriptor. The report
    records the version (when known) and the canonical digests of the declared
    schemas that were checked.
    """

    step_class = _step_class(step)
    if not isinstance(input_schema, Mapping) or not isinstance(output_schema, Mapping):
        message = "input_schema and output_schema must be JSON Schema mappings"
        raise TypeError(message)
    try:
        input_model = step_class.input_model()
        output_model = step_class.output_model()
    except TypeError as error:
        return DriftReport(
            step_identity=step_identity,
            version=None,
            input_schema_digest=None,
            output_schema_digest=None,
            issues=(_model_unresolved_issue(step_class, error),),
        )
    input_issues = compare_model_to_schema(input_model, input_schema, direction="input")
    output_issues = compare_model_to_schema(output_model, output_schema, direction="output")
    return DriftReport(
        step_identity=step_identity,
        version=None,
        input_schema_digest=canonical_json_digest(input_schema),
        output_schema_digest=canonical_json_digest(output_schema),
        issues=(*input_issues, *output_issues),
    )


def _lookup_loaded_entry_point(entry_point: str) -> type[Any] | None:
    """Resolve one entry point against already-imported modules.

    Re-exported classes (``acmepkg:CreateIssue`` where the class lives in
    ``acmepkg.steps``) are the common packaging pattern, so the string
    comparison alone would reject valid entry points. Nothing is imported:
    a module that was never loaded falls back to the string comparison.
    """

    module_name, separator, qualname = entry_point.partition(":")
    if not separator or not qualname:
        return None
    module = sys.modules.get(module_name)
    if module is None:
        return None
    found: Any = module
    for part in qualname.split("."):
        found = getattr(found, part, None)
        if found is None:
            return None
    return found if isinstance(found, type) else None


def _entry_point_issues(
    target: Mapping[str, Any],
    binding: StepBinding,
    entry_point_resolver: EntryPointResolver | None,
) -> list[DriftIssue]:
    """Check the compiled target's entry point against the bound step class."""

    runtime = target.get("runtime")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    entry_point = runtime.get("entryPoint")
    step_class = type(binding.step)
    if not isinstance(entry_point, str) or not entry_point:
        return [
            DriftIssue(
                DriftCode.ENTRY_POINT_NOT_DECLARED,
                DriftSeverity.ERROR,
                "binding",
                "",
                "the compiled target declares no runtime entry point",
                "declare spec.runtime.entryPoint as 'module:ClassName' in the target manifest.",
            )
        ]
    if entry_point_resolver is None:
        actual = f"{step_class.__module__}:{step_class.__qualname__}"
        if entry_point == actual:
            return []
        loaded = _lookup_loaded_entry_point(entry_point)
        if loaded is step_class:
            return []
        if loaded is not None:
            return [
                DriftIssue(
                    DriftCode.ENTRY_POINT_MISMATCH,
                    DriftSeverity.ERROR,
                    "binding",
                    "",
                    f"manifest entry point {entry_point} resolves to"
                    f" {loaded.__qualname__}, not the bound class {step_class.__qualname__}",
                    f'set spec.runtime.entryPoint to "{actual}".',
                )
            ]
        return [
            DriftIssue(
                DriftCode.ENTRY_POINT_MISMATCH,
                DriftSeverity.ERROR,
                "binding",
                "",
                f"manifest entry point {entry_point} does not name the bound class {actual}",
                f'set spec.runtime.entryPoint to "{actual}".',
            )
        ]
    try:
        resolved = entry_point_resolver(entry_point)
    except Exception as error:
        return [
            DriftIssue(
                DriftCode.ENTRY_POINT_UNRESOLVABLE,
                DriftSeverity.ERROR,
                "binding",
                "",
                f"the entry point resolver failed for {entry_point} ({str(error)[:120]})",
                "fix the entry point or the resolver so it can load the declared class.",
            )
        ]
    if not (isinstance(resolved, type) and issubclass(resolved, BaseStep)):
        return [
            DriftIssue(
                DriftCode.ENTRY_POINT_UNRESOLVABLE,
                DriftSeverity.ERROR,
                "binding",
                "",
                f"the resolver for {entry_point} returned a non-step object",
                "make the resolver return the BaseStep subclass for the entry point.",
            )
        ]
    if resolved is not step_class:
        return [
            DriftIssue(
                DriftCode.ENTRY_POINT_MISMATCH,
                DriftSeverity.ERROR,
                "binding",
                "",
                f"entry point {entry_point} resolves to {resolved.__qualname__},"
                f" not the bound class {step_class.__qualname__}",
                "bind the class named by the entry point, or fix the declared entry point.",
            )
        ]
    return []


def _identity_issues(identity: str | None, binding: StepBinding) -> list[DriftIssue]:
    """Report a missing identity or one that disagrees with the binding."""

    if identity is None:
        return [
            DriftIssue(
                DriftCode.IDENTITY_INVALID,
                DriftSeverity.ERROR,
                "binding",
                "",
                "the compiled target declares no usable step identity",
                "compile the target so it carries its step identity.",
            )
        ]
    if binding.descriptor.step_identity != identity:
        return [
            DriftIssue(
                DriftCode.IDENTITY_MISMATCH,
                DriftSeverity.ERROR,
                "binding",
                "",
                f"target identity {identity} does not match the bound step identity"
                f" {binding.descriptor.step_identity}",
                "bind the step registered for this target identity.",
            )
        ]
    return []


def _taxonomy_issues(target: Mapping[str, Any]) -> list[DriftIssue]:
    """Report a declared taxonomy that is malformed or unknown."""

    declared = target.get("taxonomy")
    if declared is None or not isinstance(declared, str):
        if declared is None:
            return []
        return [
            DriftIssue(
                DriftCode.TAXONOMY_INVALID,
                DriftSeverity.ERROR,
                "binding",
                "",
                "the compiled target declares a malformed taxonomy",
                "declare the taxonomy as one of action, task, workflow, or trigger.",
            )
        ]
    if _target_taxonomy(target) is None:
        return [
            DriftIssue(
                DriftCode.TAXONOMY_INVALID,
                DriftSeverity.ERROR,
                "binding",
                "",
                f"the compiled target declares an unknown taxonomy {declared!r}",
                "declare the taxonomy as one of action, task, workflow, or trigger.",
            )
        ]
    return []


def check_target_binding(
    target: Mapping[str, Any],
    binding: StepBinding,
    *,
    entry_point_resolver: EntryPointResolver | None = None,
) -> DriftReport:
    """Validate one compiled target against its bound Python implementation.

    The target must be a Python-implemented (``custom-workload``) entry from a
    compiled plugin descriptor, matched to the binding by step identity. The
    report carries the target's version and the canonical digests of the
    declared schemas that were checked.
    """

    runtime = target.get("runtime")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    if runtime.get("kind") != "custom-workload":
        message = "only custom-workload targets can be checked against Python bindings"
        raise ValueError(message)
    raw_identity = target.get("identity")
    identity = raw_identity if isinstance(raw_identity, str) and raw_identity else None
    version = target.get("version")
    version = version if isinstance(version, str) else None
    issues: list[DriftIssue] = []
    issues.extend(_identity_issues(identity, binding))
    issues.extend(_taxonomy_issues(target))
    taxonomy = _target_taxonomy(target)
    if taxonomy is not None and taxonomy != binding.descriptor.taxonomy:
        issues.append(
            DriftIssue(
                DriftCode.TAXONOMY_MISMATCH,
                DriftSeverity.ERROR,
                "binding",
                "",
                f"target kind {target.get('kind')} does not match the bound"
                f" {binding.descriptor.taxonomy.value} step",
                "bind a step of the taxonomy the target manifest declares.",
            )
        )
    step_class = type(binding.step)
    try:
        input_model = step_class.input_model()
        output_model = step_class.output_model()
    except TypeError as error:
        issues.append(_model_unresolved_issue(step_class, error))
        return DriftReport(identity, version, None, None, tuple(issues))
    issues.extend(_entry_point_issues(target, binding, entry_point_resolver))
    input_schema = target.get("input")
    output_schema = target.get("output")
    if not (isinstance(input_schema, Mapping) and isinstance(output_schema, Mapping)):
        issues.append(
            DriftIssue(
                DriftCode.SCHEMA_UNAVAILABLE,
                DriftSeverity.WARNING,
                "binding",
                "",
                "the compiled target declares no input/output schema to compare",
                "declare spec.input and spec.output in the target manifest,"
                " or accept that only the binding checks ran.",
            )
        )
        return DriftReport(identity, version, None, None, tuple(issues))
    issues.extend(compare_model_to_schema(input_model, input_schema, direction="input"))
    issues.extend(compare_model_to_schema(output_model, output_schema, direction="output"))
    return DriftReport(
        step_identity=identity,
        version=version,
        input_schema_digest=canonical_json_digest(input_schema),
        output_schema_digest=canonical_json_digest(output_schema),
        issues=tuple(issues),
    )


def _collect_step_targets(
    descriptor: Mapping[str, Any],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    """Return every declared step target and its Python-implemented subset.

    Execution is classified by the target's runtime shape, never by its
    manifest kind name: any target whose runtime is ``custom-workload`` is a
    Python implementation regardless of kind, targets with another runtime
    kind are platform-run, and driver-based targets without a runtime (such
    as compiled triggers) keep their bindings for direct SDK execution.
    """

    spec = descriptor.get("spec")
    spec = spec if isinstance(spec, Mapping) else {}
    targets = spec.get("targets")
    step_targets: dict[str, Mapping[str, Any]] = {}
    python_targets: dict[str, Mapping[str, Any]] = {}
    for target in targets if isinstance(targets, list) else []:
        if not isinstance(target, Mapping):
            continue
        identity = target.get("identity")
        if not isinstance(identity, str):
            continue
        runtime = target.get("runtime")
        if not isinstance(runtime, Mapping):
            if str(target.get("kind")) not in _TARGET_TAXONOMY:
                continue
            step_targets[identity] = target
            continue
        step_targets[identity] = target
        if runtime.get("kind") == "custom-workload":
            python_targets[identity] = target
    return step_targets, python_targets


def _missing_binding_report(identity: str, target: Mapping[str, Any]) -> DriftReport:
    """Build the report for a Python-implemented target with no binding."""

    version = target.get("version")
    return DriftReport(
        step_identity=identity,
        version=version if isinstance(version, str) else None,
        input_schema_digest=None,
        output_schema_digest=None,
        issues=(
            DriftIssue(
                DriftCode.BINDING_MISSING,
                DriftSeverity.ERROR,
                "binding",
                "",
                "the descriptor declares a Python-implemented target with no binding",
                f"bind a BaseStep implementation for step identity {identity}.",
            ),
        ),
    )


def _undeclared_binding_reports(
    bindings: StepBindings,
    python_targets: Mapping[str, Mapping[str, Any]],
    step_targets: Mapping[str, Mapping[str, Any]],
) -> list[DriftReport]:
    """Report bindings that no Python-implemented target can ever dispatch."""

    reports: list[DriftReport] = []
    for identity in bindings.step_identities:
        if identity in python_targets:
            continue
        matched = step_targets.get(identity)
        if matched is not None and not isinstance(matched.get("runtime"), Mapping):
            # Driver-based targets such as compiled triggers carry no Python
            # runtime slot; their bindings serve direct SDK execution only.
            continue
        reports.append(
            DriftReport(
                step_identity=identity,
                version=None,
                input_schema_digest=None,
                output_schema_digest=None,
                issues=(
                    DriftIssue(
                        DriftCode.BINDING_UNDECLARED,
                        DriftSeverity.WARNING,
                        "binding",
                        "",
                        "the binding matches no Python-implemented target in the descriptor",
                        f"declare a custom-workload target for {identity} or remove the binding.",
                    ),
                ),
            )
        )
    return reports


def check_plugin_bindings(
    descriptor: Mapping[str, Any],
    bindings: StepBindings,
    *,
    entry_point_resolver: EntryPointResolver | None = None,
) -> tuple[DriftReport, ...]:
    """Validate every Python-implemented target of a compiled descriptor.

    Platform-runner targets and platform-managed triggers are skipped because
    they have no Python implementation to check. Bindings whose identity
    matches no Python-implemented target are reported as undeclared. Reports
    are returned in a deterministic order, one per checked or reported target.
    """

    if not isinstance(descriptor, Mapping):
        message = "descriptor must be a compiled plugin descriptor mapping"
        raise TypeError(message)
    step_targets, python_targets = _collect_step_targets(descriptor)
    reports: list[DriftReport] = []
    for identity, target in sorted(python_targets.items()):
        try:
            binding = bindings.binding(identity)
        except StepBindingError:
            reports.append(_missing_binding_report(identity, target))
            continue
        reports.append(
            check_target_binding(target, binding, entry_point_resolver=entry_point_resolver)
        )
    reports.extend(_undeclared_binding_reports(bindings, python_targets, step_targets))
    return tuple(reports)


def validate_plugin_bindings(
    descriptor: Mapping[str, Any],
    bindings: StepBindings,
    *,
    entry_point_resolver: EntryPointResolver | None = None,
    strict: bool = False,
) -> None:
    """Raise :class:`SchemaDriftError` when any checked target fails validation.

    With ``strict=True``, intended for CI, warnings fail as well. Reports for
    every failing step are attached to the raised error.
    """

    reports = check_plugin_bindings(descriptor, bindings, entry_point_resolver=entry_point_resolver)
    failed = tuple(report for report in reports if not report.passed(strict))
    if failed:
        raise SchemaDriftError(failed)
