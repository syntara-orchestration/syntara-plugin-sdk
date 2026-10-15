"""Tests for model-versus-manifest contract drift validation."""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, TypeVar

import pytest
from pydantic import BaseModel, Field
from syntara_plugin.contracts import canonical_json_digest
from syntara_plugin.runtime import (
    ActionStep,
    BaseStep,
    DriftCode,
    DriftIssue,
    DriftReport,
    DriftSeverity,
    ExecutionContext,
    SchemaDriftError,
    StepBindings,
    StepDescriptor,
    StepTaxonomy,
    TaskStep,
    TriggerStep,
    WorkflowStep,
    check_plugin_bindings,
    check_step_contract,
    check_target_binding,
    compare_model_to_schema,
    validate_plugin_bindings,
)


class TitleInput(BaseModel):
    title: str


class TitleOutput(BaseModel):
    issue_url: str


def _make_step(
    base: type[BaseStep[Any, Any]],
    input_model: type[BaseModel],
    output_model: type[BaseModel],
) -> BaseStep[Any, Any]:
    class Step(base[input_model, output_model]):  # type: ignore[valid-type]
        def run(self, inputs: Any, context: ExecutionContext) -> Any:
            return output_model()

    return Step()


def _codes(issues: tuple[DriftIssue, ...] | tuple[DriftReport, ...]) -> set[str]:
    """Return the drift codes of an issue tuple or of every report's issues."""

    if issues and isinstance(issues[0], DriftReport):
        return {issue.code for report in issues for issue in report.issues}
    return {issue.code for issue in issues}  # type: ignore[misc]


def _paths(issues: tuple[DriftIssue, ...]) -> set[str]:
    return {issue.path for issue in issues}


def _make_binding(step: BaseStep[Any, Any], identity: str, taxonomy: StepTaxonomy) -> StepBindings:
    bindings = StepBindings()
    bindings.bind(StepDescriptor(identity, taxonomy, {"type": "object"}, {"type": "object"}), step)
    return bindings


@pytest.mark.parametrize(
    "base",
    [ActionStep, TaskStep, WorkflowStep, TriggerStep],
    ids=["action", "task", "workflow", "trigger"],
)
def test_matching_model_and_schema_pass_strict_for_every_taxonomy(
    base: type[BaseStep[Any, Any]],
) -> None:
    class PriorityInput(BaseModel):
        title: str
        priority: Literal["low", "high"] = "low"

    step = _make_step(base, PriorityInput, TitleOutput)
    report = check_step_contract(
        step,
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "high"]},
            },
            "required": ["title"],
        },
        output_schema={
            "type": "object",
            "properties": {"issue_url": {"type": "string"}},
            "required": ["issue_url"],
        },
        step_identity="acme.plugin.step",
    )

    assert report.passed(strict=True)
    assert report.step_identity == "acme.plugin.step"
    assert report.input_schema_digest == canonical_json_digest(
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "high"]},
            },
            "required": ["title"],
        }
    )
    assert report.output_schema_digest is not None


def test_input_manifest_property_missing_from_model_is_an_error() -> None:
    issues = compare_model_to_schema(
        TitleInput,
        {
            "type": "object",
            "properties": {"title": {"type": "string"}, "owner": {"type": "string"}},
            "required": ["title"],
        },
        direction="input",
    )

    assert _codes(issues) == {DriftCode.FIELD_MISSING_FROM_MODEL}
    assert _paths(issues) == {"/properties/owner"}
    assert issues[0].severity is DriftSeverity.ERROR


def test_input_model_required_field_missing_from_manifest_is_an_error() -> None:
    issues = compare_model_to_schema(
        TitleInput, {"type": "object", "properties": {}}, direction="input"
    )

    assert _codes(issues) == {DriftCode.FIELD_MISSING_FROM_SCHEMA}
    assert _paths(issues) == {"/properties/title"}
    assert issues[0].severity is DriftSeverity.ERROR


def test_input_model_optional_field_missing_from_manifest_warns() -> None:
    class OptionalInput(BaseModel):
        title: str
        assignee: str | None = None

    issues = compare_model_to_schema(
        OptionalInput,
        {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
        direction="input",
    )

    assert _codes(issues) == {DriftCode.FIELD_MISSING_FROM_SCHEMA}
    assert issues[0].severity is DriftSeverity.WARNING


def test_output_manifest_required_property_missing_from_model_is_an_error() -> None:
    issues = compare_model_to_schema(
        TitleOutput,
        {
            "type": "object",
            "properties": {"issue_url": {"type": "string"}, "issue_id": {"type": "string"}},
            "required": ["issue_url", "issue_id"],
        },
        direction="output",
    )

    codes = _codes(issues)
    assert DriftCode.FIELD_MISSING_FROM_MODEL in codes
    missing = [issue for issue in issues if issue.code is DriftCode.FIELD_MISSING_FROM_MODEL]
    assert {issue.path for issue in missing} == {"/properties/issue_id"}
    assert all(issue.severity is DriftSeverity.ERROR for issue in missing)


def test_output_manifest_optional_property_missing_from_model_warns() -> None:
    issues = compare_model_to_schema(
        TitleOutput,
        {
            "type": "object",
            "properties": {"issue_url": {"type": "string"}, "issue_id": {"type": "string"}},
            "required": ["issue_url"],
        },
        direction="output",
    )

    assert _codes(issues) == {DriftCode.FIELD_MISSING_FROM_MODEL}
    assert issues[0].severity is DriftSeverity.WARNING


def test_output_model_property_forbidden_by_manifest_is_an_error() -> None:
    class AssigneeOutput(BaseModel):
        issue_url: str
        assignee: str | None = None

    issues = compare_model_to_schema(
        AssigneeOutput,
        {
            "type": "object",
            "properties": {"issue_url": {"type": "string"}},
            "required": ["issue_url"],
            "additionalProperties": False,
        },
        direction="output",
    )

    assert DriftCode.FIELD_MISSING_FROM_SCHEMA in _codes(issues)
    blocked = [issue for issue in issues if issue.code is DriftCode.FIELD_MISSING_FROM_SCHEMA]
    assert blocked[0].severity is DriftSeverity.ERROR
    assert blocked[0].path == "/properties/assignee"


def test_output_model_property_unknown_to_manifest_warns() -> None:
    class AssigneeOutput(BaseModel):
        issue_url: str
        assignee: str | None = None

    issues = compare_model_to_schema(
        AssigneeOutput,
        {"type": "object", "properties": {"issue_url": {"type": "string"}}},
        direction="output",
    )

    assert DriftCode.FIELD_MISSING_FROM_SCHEMA in _codes(issues)
    unknown = [issue for issue in issues if issue.code is DriftCode.FIELD_MISSING_FROM_SCHEMA]
    assert unknown[0].severity is DriftSeverity.WARNING


def test_input_model_required_but_manifest_optional_is_an_error() -> None:
    issues = compare_model_to_schema(
        TitleInput,
        {"type": "object", "properties": {"title": {"type": "string"}}},
        direction="input",
    )

    assert _codes(issues) == {DriftCode.REQUIRED_MISMATCH}
    assert issues[0].severity is DriftSeverity.ERROR
    assert issues[0].path == "/properties/title"


def test_input_manifest_required_but_model_optional_warns() -> None:
    class OptionalInput(BaseModel):
        title: str = "untitled"

    issues = compare_model_to_schema(
        OptionalInput,
        {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
        direction="input",
    )

    assert _codes(issues) == {DriftCode.REQUIRED_MISMATCH}
    assert issues[0].severity is DriftSeverity.WARNING


def test_output_manifest_required_but_model_optional_warns() -> None:
    class DefaultedOutput(BaseModel):
        issue_url: str = "about:blank"

    issues = compare_model_to_schema(
        DefaultedOutput,
        {
            "type": "object",
            "properties": {"issue_url": {"type": "string"}},
            "required": ["issue_url"],
        },
        direction="output",
    )

    assert _codes(issues) == {DriftCode.REQUIRED_MISMATCH}
    assert issues[0].severity is DriftSeverity.WARNING


def test_input_rejects_integer_model_for_number_manifest() -> None:
    class CountInput(BaseModel):
        count: int

    issues = compare_model_to_schema(
        CountInput,
        {"type": "object", "properties": {"count": {"type": "number"}}, "required": ["count"]},
        direction="input",
    )

    assert _codes(issues) == {DriftCode.TYPE_INCOMPATIBLE}
    assert issues[0].severity is DriftSeverity.ERROR
    assert issues[0].path == "/properties/count"


def test_input_accepts_number_model_for_integer_manifest() -> None:
    class NumberInput(BaseModel):
        count: float

    issues = compare_model_to_schema(
        NumberInput,
        {"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]},
        direction="input",
    )

    assert issues == ()


def test_input_rejects_nullable_manifest_against_required_model() -> None:
    issues = compare_model_to_schema(
        TitleInput,
        {
            "type": "object",
            "properties": {"title": {"type": ["string", "null"]}},
            "required": ["title"],
        },
        direction="input",
    )

    assert _codes(issues) == {DriftCode.TYPE_INCOMPATIBLE}


def test_output_rejects_nullable_model_against_required_manifest() -> None:
    class NullableOutput(BaseModel):
        issue_url: str | None

    issues = compare_model_to_schema(
        NullableOutput,
        {
            "type": "object",
            "properties": {"issue_url": {"type": "string"}},
            "required": ["issue_url"],
        },
        direction="output",
    )

    assert _codes(issues) == {DriftCode.TYPE_INCOMPATIBLE}
    assert issues[0].path == "/properties/issue_url"


def test_input_enum_subset_passes_and_missing_value_fails() -> None:
    class NarrowInput(BaseModel):
        priority: Literal["low", "high"]

    class WideInput(BaseModel):
        priority: Literal["low", "high", "critical"]

    schema = {
        "type": "object",
        "properties": {"priority": {"type": "string", "enum": ["low", "high"]}},
        "required": ["priority"],
    }
    assert compare_model_to_schema(WideInput, schema, direction="input") == ()

    wider = {
        "type": "object",
        "properties": {"priority": {"type": "string", "enum": ["low", "high", "urgent"]}},
        "required": ["priority"],
    }
    issues = compare_model_to_schema(NarrowInput, wider, direction="input")
    assert _codes(issues) == {DriftCode.ENUM_INCOMPATIBLE}
    assert issues[0].severity is DriftSeverity.ERROR


def test_input_model_only_enum_is_an_error() -> None:
    class RestrictedInput(BaseModel):
        priority: Literal["low", "high"]

    issues = compare_model_to_schema(
        RestrictedInput,
        {
            "type": "object",
            "properties": {"priority": {"type": "string"}},
            "required": ["priority"],
        },
        direction="input",
    )

    assert _codes(issues) == {DriftCode.ENUM_INCOMPATIBLE}


def test_output_enum_drift_in_both_shapes() -> None:
    class WideOutput(BaseModel):
        priority: Literal["low", "high", "critical"]

    class FreeOutput(BaseModel):
        priority: str

    schema = {
        "type": "object",
        "properties": {"priority": {"type": "string", "enum": ["low", "high"]}},
        "required": ["priority"],
    }
    issues = compare_model_to_schema(WideOutput, schema, direction="output")
    assert _codes(issues) == {DriftCode.ENUM_INCOMPATIBLE}

    issues = compare_model_to_schema(FreeOutput, schema, direction="output")
    assert _codes(issues) == {DriftCode.ENUM_INCOMPATIBLE}


def test_str_enum_matches_declared_enum_values() -> None:
    class Priority(StrEnum):
        LOW = "low"
        HIGH = "high"

    class PriorityInput(BaseModel):
        priority: Priority

    schema = {
        "type": "object",
        "properties": {"priority": {"type": "string", "enum": ["low", "high"]}},
        "required": ["priority"],
    }

    assert compare_model_to_schema(PriorityInput, schema, direction="input") == ()
    assert compare_model_to_schema(PriorityInput, schema, direction="output") == ()


def test_input_model_stricter_bound_is_an_error() -> None:
    class BoundedInput(BaseModel):
        title: str = Field(min_length=3)

    issues = compare_model_to_schema(
        BoundedInput,
        {
            "type": "object",
            "properties": {"title": {"type": "string", "maxLength": 10}},
            "required": ["title"],
        },
        direction="input",
    )

    assert _codes(issues) == {DriftCode.CONSTRAINT_INCOMPATIBLE}
    assert issues[0].severity is DriftSeverity.ERROR
    assert issues[0].path == "/properties/title"


def test_input_model_looser_bound_passes() -> None:
    class LooseInput(BaseModel):
        title: str = Field(max_length=100)

    issues = compare_model_to_schema(
        LooseInput,
        {
            "type": "object",
            "properties": {"title": {"type": "string", "maxLength": 10}},
            "required": ["title"],
        },
        direction="input",
    )

    assert issues == ()


def test_input_model_only_bound_is_an_error() -> None:
    class MinInput(BaseModel):
        title: str = Field(min_length=1)

    issues = compare_model_to_schema(
        MinInput,
        {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
        direction="input",
    )

    assert _codes(issues) == {DriftCode.CONSTRAINT_INCOMPATIBLE}


def test_output_manifest_stricter_bound_is_an_error() -> None:
    class LooseOutput(BaseModel):
        issue_url: str = Field(max_length=100)

    issues = compare_model_to_schema(
        LooseOutput,
        {
            "type": "object",
            "properties": {"issue_url": {"type": "string", "maxLength": 10}},
            "required": ["issue_url"],
        },
        direction="output",
    )

    assert _codes(issues) == {DriftCode.CONSTRAINT_INCOMPATIBLE}


def test_input_multiple_of_alignment() -> None:
    class MultipleInput(BaseModel):
        count: int = Field(multiple_of=3)

    compatible = {
        "type": "object",
        "properties": {"count": {"type": "integer", "multipleOf": 6}},
        "required": ["count"],
    }
    incompatible = {
        "type": "object",
        "properties": {"count": {"type": "integer", "multipleOf": 2}},
        "required": ["count"],
    }

    assert compare_model_to_schema(MultipleInput, compatible, direction="input") == ()
    assert _codes(compare_model_to_schema(MultipleInput, incompatible, direction="input")) == {
        DriftCode.CONSTRAINT_INCOMPATIBLE
    }


def test_unique_items_alignment_per_direction() -> None:
    class TagsInput(BaseModel):
        tags: list[str]

    declared_allows_duplicates = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
        "required": ["tags"],
    }
    issues = compare_model_to_schema(TagsInput, declared_allows_duplicates, direction="output")
    assert issues == ()

    declared_requires_unique = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"type": "string"}, "uniqueItems": True}},
        "required": ["tags"],
    }
    issues = compare_model_to_schema(TagsInput, declared_requires_unique, direction="output")
    assert _codes(issues) == {DriftCode.CONSTRAINT_INCOMPATIBLE}


def test_pattern_identical_passes_and_difference_warns() -> None:
    class PatternedInput(BaseModel):
        title: str = Field(pattern="^[a-z]+$")

    matching = {
        "type": "object",
        "properties": {"title": {"type": "string", "pattern": "^[a-z]+$"}},
        "required": ["title"],
    }
    differing = {
        "type": "object",
        "properties": {"title": {"type": "string", "pattern": "^[A-Z]+$"}},
        "required": ["title"],
    }

    assert compare_model_to_schema(PatternedInput, matching, direction="input") == ()
    issues = compare_model_to_schema(PatternedInput, differing, direction="input")
    assert _codes(issues) == {DriftCode.CONSTRAINT_UNVERIFIABLE}
    assert issues[0].severity is DriftSeverity.WARNING


def test_format_difference_warns() -> None:
    class UriOutput(BaseModel):
        issue_url: str

    issues = compare_model_to_schema(
        UriOutput,
        {
            "type": "object",
            "properties": {"issue_url": {"type": "string", "format": "date-time"}},
            "required": ["issue_url"],
        },
        direction="output",
    )

    assert _codes(issues) == {DriftCode.CONSTRAINT_UNVERIFIABLE}


def test_nested_models_recurse_and_report_nested_paths() -> None:
    class Address(BaseModel):
        city: str

    class PersonInput(BaseModel):
        address: Address

    matching = {
        "type": "object",
        "properties": {
            "address": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            }
        },
        "required": ["address"],
    }
    drifted = {
        "type": "object",
        "properties": {
            "address": {
                "type": "object",
                "properties": {"city": {"type": "string"}, "zip": {"type": "string"}},
                "required": ["city"],
            }
        },
        "required": ["address"],
    }

    assert compare_model_to_schema(PersonInput, matching, direction="input") == ()
    issues = compare_model_to_schema(PersonInput, drifted, direction="input")
    assert _codes(issues) == {DriftCode.FIELD_MISSING_FROM_MODEL}
    assert issues[0].path == "/properties/address/properties/zip"


def test_arrays_of_models_recurse_through_items() -> None:
    class Address(BaseModel):
        city: str

    class TeamInput(BaseModel):
        addresses: list[Address]

    drifted = {
        "type": "object",
        "properties": {
            "addresses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"city": {"type": "integer"}},
                    "required": ["city"],
                },
            }
        },
        "required": ["addresses"],
    }

    issues = compare_model_to_schema(TeamInput, drifted, direction="input")
    assert _codes(issues) == {DriftCode.TYPE_INCOMPATIBLE}
    assert issues[0].path == "/properties/addresses/items/properties/city"


def test_recursive_model_comparison_terminates() -> None:
    class Node(BaseModel):
        name: str
        children: list[Node] = []

    declared = {
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "children": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/Node"},
                    },
                },
                "required": ["name"],
            }
        },
        "$ref": "#/$defs/Node",
    }

    assert compare_model_to_schema(Node, declared, direction="input") == ()
    drifted = {
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {
                    "name": {"type": "integer"},
                    "children": {"type": "array", "items": {"$ref": "#/$defs/Node"}},
                },
                "required": ["name"],
            }
        },
        "$ref": "#/$defs/Node",
    }
    issues = compare_model_to_schema(Node, drifted, direction="input")
    assert _codes(issues) == {DriftCode.TYPE_INCOMPATIBLE}


def test_input_manifest_extras_against_model_forbid_is_an_error() -> None:
    class StrictInput(BaseModel):
        model_config = {"extra": "forbid"}

        title: str

    issues = compare_model_to_schema(
        StrictInput,
        {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
        direction="input",
    )

    assert _codes(issues) == {DriftCode.ADDITIONAL_PROPERTIES_INCOMPATIBLE}


def test_input_manifest_forbidding_extras_against_ignoring_model_passes() -> None:
    issues = compare_model_to_schema(
        TitleInput,
        {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            "additionalProperties": False,
        },
        direction="input",
    )

    assert issues == ()


def test_output_model_allowing_extras_against_forbidding_manifest_is_an_error() -> None:
    class OpenOutput(BaseModel):
        model_config = {"extra": "allow"}

        issue_url: str

    issues = compare_model_to_schema(
        OpenOutput,
        {
            "type": "object",
            "properties": {"issue_url": {"type": "string"}},
            "required": ["issue_url"],
            "additionalProperties": False,
        },
        direction="output",
    )

    assert _codes(issues) == {DriftCode.ADDITIONAL_PROPERTIES_INCOMPATIBLE}


def test_input_alias_is_accepted_on_the_model_side() -> None:
    class AliasedInput(BaseModel):
        full_name: str = Field(alias="fullName")

    issues = compare_model_to_schema(
        AliasedInput,
        {
            "type": "object",
            "properties": {"fullName": {"type": "string"}},
            "required": ["fullName"],
        },
        direction="input",
    )

    assert issues == ()


def test_output_emits_field_names_and_pins_adapter_behavior() -> None:
    """The adapter dumps by field name, so aliased output drifts until Ana's decision lands."""

    class AliasedOutput(BaseModel):
        full_name: str = Field(alias="fullName")

    issues = compare_model_to_schema(
        AliasedOutput,
        {
            "type": "object",
            "properties": {"fullName": {"type": "string"}},
            "required": ["fullName"],
        },
        direction="output",
    )

    assert _codes(issues) == {
        DriftCode.FIELD_MISSING_FROM_MODEL,
        DriftCode.FIELD_MISSING_FROM_SCHEMA,
    }


def test_output_serialized_by_alias_when_model_uses_serialize_by_alias() -> None:
    class AliasedOutput(BaseModel):
        model_config = {"serialize_by_alias": True}

        full_name: str = Field(alias="fullName")

    issues = compare_model_to_schema(
        AliasedOutput,
        {
            "type": "object",
            "properties": {"fullName": {"type": "string"}},
            "required": ["fullName"],
        },
        direction="output",
    )

    assert issues == ()


def test_unsupported_keyword_warns_and_stops_recursion() -> None:
    issues = compare_model_to_schema(
        TitleInput,
        {
            "type": "object",
            "properties": {"title": {"type": "string"}, "extra": {"type": "string"}},
            "required": ["title"],
            "oneOf": [{"type": "object"}, {"type": "array"}],
        },
        direction="input",
    )

    assert _codes(issues) == {DriftCode.SCHEMA_UNVERIFIABLE}
    assert issues[0].path == ""
    assert issues[0].severity is DriftSeverity.WARNING


def test_non_nullable_any_of_warns() -> None:
    issues = compare_model_to_schema(
        TitleInput,
        {
            "type": "object",
            "properties": {"title": {"type": "string"}, "either": {"type": "string"}},
            "required": ["title"],
            "anyOf": [{"required": ["title"]}, {"required": ["either"]}],
        },
        direction="input",
    )

    assert _codes(issues) == {DriftCode.SCHEMA_UNVERIFIABLE}


def test_boolean_schemas_do_not_crash() -> None:
    issues = compare_model_to_schema(
        TitleInput,
        {
            "type": "object",
            "properties": {"title": {"type": "string"}, "anything": True},
            "required": ["title"],
        },
        direction="input",
    )

    assert _codes(issues) == {DriftCode.FIELD_MISSING_FROM_MODEL}


def test_bad_arguments_raise_type_errors() -> None:
    with pytest.raises(TypeError):
        compare_model_to_schema("not a model", {"type": "object"}, direction="input")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compare_model_to_schema(TitleInput, "not a schema", direction="input")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        compare_model_to_schema(TitleInput, {"type": "object"}, direction="sideways")  # type: ignore[arg-type]


def test_issue_messages_are_bounded() -> None:
    issue = DriftIssue(
        DriftCode.TYPE_INCOMPATIBLE,
        DriftSeverity.ERROR,
        "input",
        "/properties/x",
        "x" * 2_000,
        "y" * 2_000,
    )

    assert len(issue.message) == 512
    assert len(issue.remediation) == 512


def test_report_issues_are_deduplicated_and_sorted() -> None:
    first = DriftIssue(
        DriftCode.FIELD_MISSING_FROM_MODEL, DriftSeverity.ERROR, "input", "/properties/b", "m", "r"
    )
    duplicate = DriftIssue(
        DriftCode.FIELD_MISSING_FROM_MODEL, DriftSeverity.ERROR, "input", "/properties/b", "m", "r"
    )
    second = DriftIssue(
        DriftCode.TYPE_INCOMPATIBLE, DriftSeverity.ERROR, "input", "/properties/a", "m", "r"
    )
    binding = DriftIssue(DriftCode.BINDING_MISSING, DriftSeverity.ERROR, "binding", "", "m", "r")

    report = DriftReport(
        "acme.plugin.step", "1.0.0", None, None, (first, duplicate, second, binding)
    )

    assert report.issues == (binding, second, first)
    assert report.errors == report.issues
    assert report.warnings == ()
    assert report.passed() is False


def _matching_target(step_class: type[BaseStep[Any, Any]], **overrides: Any) -> dict[str, Any]:
    """Build one compiled custom-workload Action target matching the step's models."""

    target: dict[str, Any] = {
        "kind": "Action",
        "identity": "acme.issue-tools.create-issue",
        "version": "0.1.0",
        "input": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
        "output": {
            "type": "object",
            "properties": {"issue_url": {"type": "string"}},
            "required": ["issue_url"],
        },
        "runtime": {
            "kind": "custom-workload",
            "entryPoint": f"{step_class.__module__}:{step_class.__qualname__}",
            "workload": "plugin-workload",
            "abi": "syntara.container/v1alpha1",
        },
    }
    target.update(overrides)
    return target


class _IssueInput(BaseModel):
    title: str


class _IssueOutput(BaseModel):
    issue_url: str


class CreateIssueStep(ActionStep[_IssueInput, _IssueOutput]):
    def run(self, inputs: _IssueInput, context: ExecutionContext) -> _IssueOutput:
        return _IssueOutput(issue_url=inputs.title)


def _issue_bindings(step: BaseStep[Any, Any] | None = None) -> StepBindings:
    bindings = StepBindings()
    bindings.bind(
        StepDescriptor(
            "acme.issue-tools.create-issue",
            StepTaxonomy.ACTION,
            _IssueInput.model_json_schema(),
            _IssueOutput.model_json_schema(),
        ),
        step if step is not None else CreateIssueStep(),
    )
    return bindings


def test_check_target_binding_passes_matching_step() -> None:
    bindings = _issue_bindings()
    report = check_target_binding(
        _matching_target(type(bindings.resolve("acme.issue-tools.create-issue"))),  # type: ignore[arg-type]
        bindings.binding("acme.issue-tools.create-issue"),
    )

    assert report.passed(strict=True)
    assert report.step_identity == "acme.issue-tools.create-issue"
    assert report.version == "0.1.0"
    assert report.input_schema_digest is not None
    assert report.output_schema_digest is not None


def test_check_target_binding_rejects_platform_runner_targets() -> None:
    bindings = _issue_bindings()
    target = _matching_target(CreateIssueStep)
    target["runtime"] = {"kind": "platform-runner", "driver": "http.v1"}

    binding = bindings.binding("acme.issue-tools.create-issue")
    with pytest.raises(ValueError, match="custom-workload"):
        check_target_binding(target, binding)


def test_check_target_binding_reports_taxonomy_mismatch() -> None:
    class CreateIssueTask(TaskStep[_IssueInput, _IssueOutput]):
        def run(self, inputs: _IssueInput, context: ExecutionContext) -> _IssueOutput:
            return _IssueOutput(issue_url=inputs.title)

    bindings = StepBindings()
    bindings.bind(
        StepDescriptor(
            "acme.issue-tools.create-issue",
            StepTaxonomy.TASK,
            {"type": "object"},
            {"type": "object"},
        ),
        CreateIssueTask(),
    )
    target = _matching_target(CreateIssueTask)
    target["kind"] = "Action"

    report = check_target_binding(target, bindings.binding("acme.issue-tools.create-issue"))

    assert DriftCode.TAXONOMY_MISMATCH in _codes(report.issues)
    assert not report.passed()


def test_check_target_binding_reports_unresolved_models() -> None:
    TIn = TypeVar("TIn", bound=BaseModel)
    TOut = TypeVar("TOut", bound=BaseModel)

    class GenericStep(ActionStep[TIn, TOut]):
        def run(self, inputs: TIn, context: ExecutionContext) -> TOut:
            raise NotImplementedError

    bindings = StepBindings()
    bindings.bind(
        StepDescriptor(
            "acme.issue-tools.create-issue",
            StepTaxonomy.ACTION,
            {"type": "object"},
            {"type": "object"},
        ),
        GenericStep(),  # type: ignore[arg-type]
    )
    report = check_target_binding(
        _matching_target(GenericStep), bindings.binding("acme.issue-tools.create-issue")
    )

    assert _codes(report.issues) == {DriftCode.MODEL_UNRESOLVED}
    assert not report.passed()


def test_check_target_binding_entry_point_not_declared_is_an_error() -> None:
    bindings = _issue_bindings()
    target = _matching_target(CreateIssueStep)
    target["runtime"] = {"kind": "custom-workload", "workload": "plugin-workload"}

    report = check_target_binding(target, bindings.binding("acme.issue-tools.create-issue"))

    assert DriftCode.ENTRY_POINT_NOT_DECLARED in _codes(report.issues)
    assert not report.passed()


def test_check_target_binding_entry_point_string_mismatch_is_an_error() -> None:
    bindings = _issue_bindings()
    target = _matching_target(CreateIssueStep)
    target["runtime"] = dict(target["runtime"], entryPoint="somewhere.else:WrongClass")

    report = check_target_binding(target, bindings.binding("acme.issue-tools.create-issue"))

    assert DriftCode.ENTRY_POINT_MISMATCH in _codes(report.issues)
    assert not report.passed()


def test_check_target_binding_entry_point_resolver_agreement() -> None:
    bindings = _issue_bindings()
    target = _matching_target(CreateIssueStep)
    entry_point = target["runtime"]["entryPoint"]
    assert isinstance(entry_point, str)

    report = check_target_binding(
        target,
        bindings.binding("acme.issue-tools.create-issue"),
        entry_point_resolver=lambda name: CreateIssueStep if name == entry_point else OtherStep,
    )
    assert report.passed(strict=True)

    mismatched = check_target_binding(
        target,
        bindings.binding("acme.issue-tools.create-issue"),
        entry_point_resolver=lambda name: OtherStep,
    )
    assert DriftCode.ENTRY_POINT_MISMATCH in _codes(mismatched.issues)


class OtherStep(ActionStep[_IssueInput, _IssueOutput]):
    def run(self, inputs: _IssueInput, context: ExecutionContext) -> _IssueOutput:
        return _IssueOutput(issue_url=inputs.title)


def test_check_target_binding_resolver_failures_are_errors() -> None:
    bindings = _issue_bindings()
    target = _matching_target(CreateIssueStep)

    def raising(name: str) -> type[BaseStep[Any, Any]]:
        message = "cannot import"
        raise ImportError(name + ": " + message)

    report = check_target_binding(
        target, bindings.binding("acme.issue-tools.create-issue"), entry_point_resolver=raising
    )
    assert DriftCode.ENTRY_POINT_UNRESOLVABLE in _codes(report.issues)

    report = check_target_binding(
        target,
        bindings.binding("acme.issue-tools.create-issue"),
        entry_point_resolver=lambda name: dict,  # type: ignore[arg-type,return-value]
    )
    assert DriftCode.ENTRY_POINT_UNRESOLVABLE in _codes(report.issues)


def test_check_target_binding_without_schemas_reports_unavailable() -> None:
    bindings = _issue_bindings()
    target = _matching_target(CreateIssueStep)
    del target["input"]
    del target["output"]

    report = check_target_binding(target, bindings.binding("acme.issue-tools.create-issue"))

    assert DriftCode.SCHEMA_UNAVAILABLE in _codes(report.issues)
    assert report.passed()
    assert not report.passed(strict=True)
    assert report.input_schema_digest is None


def test_check_plugin_bindings_reports_missing_and_undeclared() -> None:
    target = _matching_target(CreateIssueStep)
    descriptor = {"spec": {"targets": [target, {"kind": "IntegrationType"}]}}

    reports = check_plugin_bindings(descriptor, StepBindings())
    assert _codes(reports) == {DriftCode.BINDING_MISSING}
    assert not reports[0].passed()

    reports = check_plugin_bindings(descriptor, _issue_bindings())
    assert reports[0].passed(strict=True)

    stray = _issue_bindings()
    stray.bind(
        StepDescriptor(
            "acme.issue-tools.close-issue",
            StepTaxonomy.ACTION,
            {"type": "object"},
            {"type": "object"},
        ),
        CreateIssueStep(),
    )
    reports = check_plugin_bindings(descriptor, stray)
    assert DriftCode.BINDING_UNDECLARED in _codes(reports)


def test_check_plugin_bindings_skips_platform_runner_targets() -> None:
    target = _matching_target(CreateIssueStep)
    target["runtime"] = {"kind": "platform-runner", "driver": "http.v1"}
    descriptor = {"spec": {"targets": [target]}}

    reports = check_plugin_bindings(descriptor, _issue_bindings())

    assert DriftCode.BINDING_UNDECLARED in _codes(reports)
    assert all(DriftCode.BINDING_MISSING not in _codes((report,)) for report in reports)


class IssueOpenedTrigger(TriggerStep[_IssueInput, _IssueOutput]):
    def run(self, inputs: _IssueInput, context: ExecutionContext) -> _IssueOutput:
        return _IssueOutput(issue_url=inputs.title)


def test_check_plugin_bindings_reports_trigger_without_schemas() -> None:
    trigger = {
        "kind": "Trigger",
        "identity": "acme.issue-tools.issue-opened",
        "version": "0.1.0",
        "runtime": {
            "kind": "custom-workload",
            "entryPoint": f"{IssueOpenedTrigger.__module__}:{IssueOpenedTrigger.__qualname__}",
        },
        "configuration": {"type": "object"},
    }
    descriptor = {"spec": {"targets": [trigger]}}
    bindings = StepBindings()
    bindings.bind(
        StepDescriptor(
            "acme.issue-tools.issue-opened",
            StepTaxonomy.TRIGGER,
            {"type": "object"},
            {"type": "object"},
        ),
        IssueOpenedTrigger(),
    )

    reports = check_plugin_bindings(descriptor, bindings)

    assert _codes(reports) == {DriftCode.SCHEMA_UNAVAILABLE}
    assert reports[0].passed()
    assert not reports[0].passed(strict=True)


def test_check_plugin_bindings_rejects_bad_descriptor_argument() -> None:
    empty_bindings = StepBindings()
    with pytest.raises(TypeError):
        check_plugin_bindings("not a descriptor", empty_bindings)  # type: ignore[arg-type]


def test_validate_plugin_bindings_raises_with_drift_report() -> None:
    target = _matching_target(CreateIssueStep)
    target["input"] = {
        "type": "object",
        "properties": {"title": {"type": "string"}, "owner": {"type": "string"}},
        "required": ["title"],
    }
    descriptor = {"spec": {"targets": [target]}}

    bindings = _issue_bindings()
    with pytest.raises(SchemaDriftError) as raised:
        validate_plugin_bindings(descriptor, bindings, strict=True)

    reports = raised.value.report
    assert isinstance(reports, tuple)
    assert DriftCode.FIELD_MISSING_FROM_MODEL in _codes(reports)
    assert "/properties/owner" in str(raised.value)


def test_validate_plugin_bindings_passes_on_clean_contracts() -> None:
    descriptor = {"spec": {"targets": [_matching_target(CreateIssueStep)]}}

    validate_plugin_bindings(descriptor, _issue_bindings(), strict=True)


def test_validate_plugin_bindings_strict_escalates_warnings() -> None:
    descriptor = {"spec": {"targets": [_matching_target(CreateIssueStep)]}}

    bindings = _issue_bindings()
    bindings.bind(
        StepDescriptor(
            "acme.issue-tools.close-issue",
            StepTaxonomy.ACTION,
            {"type": "object"},
            {"type": "object"},
        ),
        CreateIssueStep(),
    )
    validate_plugin_bindings(descriptor, bindings)

    with pytest.raises(SchemaDriftError):
        validate_plugin_bindings(descriptor, bindings, strict=True)


def test_drift_report_raise_for_drift_and_error_rendering() -> None:
    report = DriftReport(
        "acme.plugin.step",
        "1.0.0",
        None,
        None,
        (
            DriftIssue(
                DriftCode.TYPE_INCOMPATIBLE,
                DriftSeverity.ERROR,
                "input",
                "/properties/title",
                "declared types string are not all accepted by the model (integer)",
                "align the types.",
            ),
        ),
    )

    with pytest.raises(SchemaDriftError) as raised:
        report.raise_for_drift()

    assert raised.value.report is report
    text = str(raised.value)
    assert "ERROR   input  /properties/title" in text
    assert "fix: align the types." in text
    assert "1 error(s), 0 warning(s)" in text


def test_check_step_contract_reports_unresolved_models() -> None:
    TIn = TypeVar("TIn", bound=BaseModel)
    TOut = TypeVar("TOut", bound=BaseModel)

    class GenericStep(ActionStep[TIn, TOut]):
        def run(self, inputs: TIn, context: ExecutionContext) -> TOut:
            raise NotImplementedError

    report = check_step_contract(
        GenericStep,  # type: ignore[arg-type]
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )

    assert _codes(report.issues) == {DriftCode.MODEL_UNRESOLVED}
    assert report.input_schema_digest is None


class _Sub(BaseModel):
    value: str


def test_one_sided_recursion_terminates() -> None:
    """A recursive manifest schema against a plain model must not recurse forever."""

    class MetaInput(BaseModel):
        meta: dict[str, Any]

    self_referential = {
        "$defs": {
            "J": {
                "type": "object",
                "additionalProperties": {"$ref": "#/$defs/J"},
            }
        },
        "type": "object",
        "properties": {"meta": {"$ref": "#/$defs/J"}},
        "required": ["meta"],
    }

    issues = compare_model_to_schema(MetaInput, self_referential, direction="input")

    assert all(issue.code is not DriftCode.FIELD_MISSING_FROM_MODEL for issue in issues)

    class ListInput(BaseModel):
        xs: list[Any]

    nested_arrays = {
        "$defs": {"A": {"type": "array", "items": {"$ref": "#/$defs/A"}}},
        "type": "object",
        "properties": {"xs": {"$ref": "#/$defs/A"}},
        "required": ["xs"],
    }

    assert compare_model_to_schema(ListInput, nested_arrays, direction="input") == ()
    output_issues = compare_model_to_schema(ListInput, nested_arrays, direction="output")
    assert _codes(output_issues) == {DriftCode.TYPE_INCOMPATIBLE}


def test_nullable_ref_memo_still_reports_nullability_drift() -> None:
    class HolderOutput(BaseModel):
        a: _Sub
        b: _Sub | None

    declared = {
        "$defs": {
            "Sub": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            }
        },
        "type": "object",
        "properties": {
            "a": {"$ref": "#/$defs/Sub"},
            "b": {"$ref": "#/$defs/Sub"},
        },
        "required": ["a", "b"],
    }

    issues = compare_model_to_schema(HolderOutput, declared, direction="output")

    assert _codes(issues) == {DriftCode.TYPE_INCOMPATIBLE}
    assert issues[0].path == "/properties/b"


def test_enum_without_declared_type_passes() -> None:
    class PriorityInput(BaseModel):
        priority: Literal["low", "high"]

    issues = compare_model_to_schema(
        PriorityInput,
        {
            "type": "object",
            "properties": {"priority": {"enum": ["low", "high"]}},
            "required": ["priority"],
        },
        direction="input",
    )

    assert issues == ()


def test_nullable_typeless_enum_reference_passes() -> None:
    class Color(StrEnum):
        RED = "red"
        BLUE = "blue"

    class ColorOutput(BaseModel):
        color: Color | None

    declared = {
        "$defs": {"Color": {"enum": ["red", "blue"]}},
        "type": "object",
        "properties": {"color": {"anyOf": [{"$ref": "#/$defs/Color"}, {"type": "null"}]}},
        "required": ["color"],
    }

    assert compare_model_to_schema(ColorOutput, declared, direction="output") == ()


def test_exclusive_and_inclusive_bounds_cross_compare() -> None:
    class PositiveInput(BaseModel):
        count: int = Field(gt=0)

    overlapping = {
        "type": "object",
        "properties": {"count": {"type": "integer", "minimum": 1}},
        "required": ["count"],
    }

    assert compare_model_to_schema(PositiveInput, overlapping, direction="input") == ()

    class GeOutput(BaseModel):
        count: int = Field(ge=1)

    overlapping_output = {
        "type": "object",
        "properties": {"count": {"type": "integer", "exclusiveMinimum": 0}},
        "required": ["count"],
    }

    assert compare_model_to_schema(GeOutput, overlapping_output, direction="output") == ()

    class ExclusiveInput(BaseModel):
        count: int = Field(gt=5)

    stricter = {
        "type": "object",
        "properties": {"count": {"type": "integer", "minimum": 1}},
        "required": ["count"],
    }

    issues = compare_model_to_schema(ExclusiveInput, stricter, direction="input")
    assert _codes(issues) == {DriftCode.CONSTRAINT_INCOMPATIBLE}


def test_model_dict_field_against_forbidding_manifest_output() -> None:
    class LabelsOutput(BaseModel):
        labels: dict[str, str]

    issues = compare_model_to_schema(
        LabelsOutput,
        {
            "type": "object",
            "properties": {"labels": {"type": "object", "additionalProperties": False}},
            "required": ["labels"],
        },
        direction="output",
    )

    assert _codes(issues) == {DriftCode.ADDITIONAL_PROPERTIES_INCOMPATIBLE}


def test_model_only_property_judged_by_additional_properties_schema() -> None:
    class CountOutput(BaseModel):
        count: int

    issues = compare_model_to_schema(
        CountOutput,
        {
            "type": "object",
            "properties": {},
            "additionalProperties": {"type": "string"},
        },
        direction="output",
    )

    assert DriftCode.TYPE_INCOMPATIBLE in _codes(issues)
    assert issues[0].path == "/properties/count"


def test_input_manifest_forbidding_extras_skips_model_dict_comparison() -> None:
    class LabelsInput(BaseModel):
        labels: dict[str, str]

    issues = compare_model_to_schema(
        LabelsInput,
        {
            "type": "object",
            "properties": {"labels": {"type": "object", "additionalProperties": False}},
            "required": ["labels"],
        },
        direction="input",
    )

    assert issues == ()


def test_true_schema_is_compared_not_skipped() -> None:
    class CountInput(BaseModel):
        count: int

    issues = compare_model_to_schema(
        CountInput,
        {"type": "object", "properties": {"count": True}, "required": ["count"]},
        direction="input",
    )

    assert _codes(issues) == {DriftCode.TYPE_INCOMPATIBLE}


def test_contains_and_property_count_keywords_are_flagged() -> None:
    issues = compare_model_to_schema(
        TitleInput,
        {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            "minProperties": 5,
        },
        direction="input",
    )

    assert _codes(issues) == {DriftCode.SCHEMA_UNVERIFIABLE}


def test_model_that_cannot_generate_a_schema_becomes_an_issue() -> None:
    class CallableInput(BaseModel):
        callback: Callable[..., Any]

    issues = compare_model_to_schema(CallableInput, {"type": "object"}, direction="input")

    assert _codes(issues) == {DriftCode.MODEL_UNRESOLVED}


def test_check_step_contract_rejects_non_mapping_schemas_up_front() -> None:
    with pytest.raises(TypeError, match="JSON Schema mappings"):
        check_step_contract(
            CreateIssueStep,
            input_schema="not a schema",  # type: ignore[arg-type]
            output_schema={"type": "object"},
        )


def test_multiple_of_decimal_values_stay_exact() -> None:
    class TenthsInput(BaseModel):
        count: float = Field(multiple_of=0.1)

    class ThirdsOutput(BaseModel):
        count: float = Field(multiple_of=0.3)

    schema = {
        "type": "object",
        "properties": {"count": {"type": "number", "multipleOf": 0.3}},
        "required": ["count"],
    }

    assert compare_model_to_schema(TenthsInput, schema, direction="input") == ()

    tenths = {
        "type": "object",
        "properties": {"count": {"type": "number", "multipleOf": 0.1}},
        "required": ["count"],
    }

    assert compare_model_to_schema(ThirdsOutput, tenths, direction="output") == ()


def test_integer_producer_satisfies_multiple_of_one() -> None:
    class CountInput(BaseModel):
        count: int

    issues = compare_model_to_schema(
        CountInput,
        {
            "type": "object",
            "properties": {"count": {"type": "integer", "multipleOf": 1}},
            "required": ["count"],
        },
        direction="output",
    )

    assert issues == ()


def test_producer_only_pattern_does_not_warn() -> None:
    class ScheduledOutput(BaseModel):
        when: datetime

    issues = compare_model_to_schema(
        ScheduledOutput,
        {
            "type": "object",
            "properties": {"when": {"type": "string"}},
            "required": ["when"],
        },
        direction="output",
    )

    assert issues == ()


def test_property_names_are_pointer_escaped() -> None:
    class SlashInput(BaseModel):
        a_b: int

    issues = compare_model_to_schema(
        SlashInput,
        {
            "type": "object",
            "properties": {"a/b": {"type": "string"}},
            "required": ["a/b"],
        },
        direction="input",
    )

    assert _paths(issues) == {"/properties/a~1b", "/properties/a_b"}


def test_issue_paths_are_length_capped() -> None:
    issue = DriftIssue(
        DriftCode.TYPE_INCOMPATIBLE,
        DriftSeverity.ERROR,
        "input",
        "/" + "x" * 10_000,
        "m",
        "r",
    )

    assert len(issue.path) == 512


def test_reexported_entry_point_resolves_through_loaded_modules() -> None:
    import types

    module = types.ModuleType("acme_reexport")
    module.CreateIssueStep = CreateIssueStep
    sys.modules["acme_reexport"] = module
    try:
        bindings = _issue_bindings()
        target = _matching_target(CreateIssueStep)
        target["runtime"] = dict(target["runtime"], entryPoint="acme_reexport:CreateIssueStep")

        report = check_target_binding(target, bindings.binding("acme.issue-tools.create-issue"))
        assert all(issue.code is not DriftCode.ENTRY_POINT_MISMATCH for issue in report.issues)

        module.WrongStep = OtherStep
        target["runtime"] = dict(target["runtime"], entryPoint="acme_reexport:WrongStep")
        report = check_target_binding(target, bindings.binding("acme.issue-tools.create-issue"))
        assert DriftCode.ENTRY_POINT_MISMATCH in _codes(report.issues)
    finally:
        del sys.modules["acme_reexport"]


def test_declared_false_items_do_not_false_positive_on_input() -> None:
    class TagsInput(BaseModel):
        tags: list[str]

    prefix_only = {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "prefixItems": [{"type": "string"}], "items": False}
        },
        "required": ["tags"],
    }

    assert compare_model_to_schema(TagsInput, prefix_only, direction="input") == ()


def test_declared_false_items_flag_model_emission_on_output() -> None:
    class TagsOutput(BaseModel):
        tags: list[str]

    no_items = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": False}},
        "required": ["tags"],
    }

    issues = compare_model_to_schema(TagsOutput, no_items, direction="output")

    assert _codes(issues) == {DriftCode.TYPE_INCOMPATIBLE}
    assert issues[0].path == "/properties/tags/items"


def test_declared_true_items_still_compare_against_typed_models() -> None:
    class TagsInput(BaseModel):
        tags: list[str]

    any_items = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": True}},
        "required": ["tags"],
    }

    issues = compare_model_to_schema(TagsInput, any_items, direction="input")

    assert _codes(issues) == {DriftCode.TYPE_INCOMPATIBLE}


def test_model_default_outside_declared_enum_is_an_error() -> None:
    class Priority(BaseModel):
        priority: Literal["low", "high"] = "urgent"

    issues = compare_model_to_schema(
        Priority,
        {
            "type": "object",
            "properties": {"priority": {"type": "string", "enum": ["low", "high"]}},
            "required": ["priority"],
        },
        direction="output",
    )

    assert DriftCode.ENUM_INCOMPATIBLE in _codes(issues)
    default_issue = [i for i in issues if "default value" in i.message]
    assert default_issue
    assert default_issue[0].path == "/properties/priority"


def test_manifest_default_outside_model_enum_is_an_error() -> None:
    class Priority(BaseModel):
        priority: Literal["low", "high"]

    issues = compare_model_to_schema(
        Priority,
        {
            "type": "object",
            "properties": {
                "priority": {"type": "string", "enum": ["low", "high"], "default": "urgent"}
            },
            "required": ["priority"],
        },
        direction="input",
    )

    assert DriftCode.ENUM_INCOMPATIBLE in _codes(issues)


def test_differing_defaults_warn_and_equal_defaults_pass() -> None:
    class Priority(BaseModel):
        priority: Literal["low", "high"] = "low"

    equal = {
        "type": "object",
        "properties": {"priority": {"type": "string", "enum": ["low", "high"], "default": "low"}},
    }
    assert compare_model_to_schema(Priority, equal, direction="output") == ()

    differing = {
        "type": "object",
        "properties": {"priority": {"type": "string", "enum": ["low", "high"], "default": "high"}},
    }
    issues = compare_model_to_schema(Priority, differing, direction="output")
    assert _codes(issues) == {DriftCode.DEFAULT_MISMATCH}
    assert issues[0].severity is DriftSeverity.WARNING


def test_check_target_binding_reports_identity_mismatch() -> None:
    bindings = StepBindings()
    bindings.bind(
        StepDescriptor(
            "acme.issue-tools.close-issue",
            StepTaxonomy.ACTION,
            _IssueInput.model_json_schema(),
            _IssueOutput.model_json_schema(),
        ),
        CreateIssueStep(),
    )
    target = _matching_target(CreateIssueStep)

    report = check_target_binding(target, bindings.binding("acme.issue-tools.close-issue"))

    assert DriftCode.IDENTITY_MISMATCH in _codes(report.issues)
    assert not report.passed()


def test_unmapped_kind_with_python_runtime_is_validated() -> None:
    """The umbrella Step kind must not gate validation: runtime shape decides."""

    bindings = _issue_bindings()
    target = _matching_target(CreateIssueStep)
    target["kind"] = "Step"

    report = check_target_binding(target, bindings.binding("acme.issue-tools.create-issue"))
    assert report.passed(strict=True)

    target["input"] = {
        "type": "object",
        "properties": {"title": {"type": "string"}, "owner": {"type": "string"}},
        "required": ["title", "owner"],
    }
    reports = check_plugin_bindings({"spec": {"targets": [target]}}, bindings)
    assert _codes(reports) == {DriftCode.FIELD_MISSING_FROM_MODEL}


def test_unmapped_kind_with_python_runtime_is_not_reported_undeclared() -> None:
    bindings = _issue_bindings()
    target = _matching_target(CreateIssueStep)
    target["kind"] = "Step"

    reports = check_plugin_bindings({"spec": {"targets": [target]}}, bindings)

    assert reports[0].passed(strict=True)
    assert DriftCode.BINDING_UNDECLARED not in _codes(reports)


def test_taxonomy_metadata_is_verified_for_unmapped_kinds() -> None:
    class CreateIssueTask(TaskStep[_IssueInput, _IssueOutput]):
        def run(self, inputs: _IssueInput, context: ExecutionContext) -> _IssueOutput:
            return _IssueOutput(issue_url=inputs.title)

    bindings = StepBindings()
    bindings.bind(
        StepDescriptor(
            "acme.issue-tools.create-issue",
            StepTaxonomy.TASK,
            {"type": "object"},
            {"type": "object"},
        ),
        CreateIssueTask(),
    )
    target = _matching_target(CreateIssueTask)
    target["kind"] = "Step"
    target["taxonomy"] = "action"

    report = check_target_binding(target, bindings.binding("acme.issue-tools.create-issue"))

    assert DriftCode.TAXONOMY_MISMATCH in _codes(report.issues)


def test_binding_for_non_step_target_is_undeclared() -> None:
    descriptor = {"spec": {"targets": [{"kind": "IntegrationType", "identity": "acme.conn"}]}}
    bindings = _issue_bindings()
    bindings.bind(
        StepDescriptor("acme.conn", StepTaxonomy.ACTION, {"type": "object"}, {"type": "object"}),
        CreateIssueStep(),
    )

    reports = check_plugin_bindings(descriptor, bindings)

    assert DriftCode.BINDING_UNDECLARED in _codes(reports)
