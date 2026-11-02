"""Field-level comparison of two declared schemas that must agree."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .manifests import schema_without_descriptions


class SchemaAgreementError(ValueError):
    """Raised when two declared schemas that must agree diverge."""

    def __init__(self, issues: Sequence[str]) -> None:
        self.issues = tuple(issues)
        details = "; ".join(self.issues) or "the schemas differ"
        super().__init__(f"declared schemas do not agree: {details}")


def _format_location(path: Sequence[str]) -> str:
    return "<root>" if not path else "".join(f".{segment}" for segment in path)


def _format_value(value: Any) -> str:
    """Render a schema fragment compactly, preferring its keyword name."""

    if isinstance(value, str):
        return f"'{value}'"
    if isinstance(value, Mapping):
        return f"{{{', '.join(sorted(value))}}}" if value else "{}"
    return str(value)


def _agreement_issues(
    source: Any, target: Any, path: tuple[str, ...]
) -> tuple[str, ...]:
    if isinstance(source, Mapping) and isinstance(target, Mapping):
        issues: list[str] = []
        for key in sorted(set(source) | set(target)):
            if key == "description":
                continue
            location = _format_location((*path, key))
            if key not in source:
                issues.append(f"{location}: missing from the source schema")
            elif key not in target:
                issues.append(f"{location}: missing from the target schema")
            else:
                issues.extend(
                    _agreement_issues(source[key], target[key], (*path, key))
                )
        return tuple(issues)
    if isinstance(source, list) and isinstance(target, list):
        if len(source) != len(target):
            return (
                f"{_format_location(path)}: {len(source)} item(s) in the source "
                f"schema vs {len(target)} in the target schema",
            )
        issues = []
        for index, (source_item, target_item) in enumerate(
            zip(source, target, strict=True)
        ):
            issues.extend(
                _agreement_issues(source_item, target_item, (*path, f"[{index}]"))
            )
        return tuple(issues)
    if source != target:
        return (
            f"{_format_location(path)}: {_format_value(source)} in the source "
            f"schema vs {_format_value(target)} in the target schema",
        )
    return ()


def schema_agreement_issues(source: Any, target: Any) -> tuple[str, ...]:
    """Report every field-level divergence between two schemas that must agree.

    ``description`` keys are ignored (they differ by perspective); everything
    else — types, formats, nested schemas, defaults, enums, ``required`` —
    must match. Each issue names the location and the diverging values, so a
    failure reads as a repair list rather than a bare inequality.
    """

    return _agreement_issues(
        schema_without_descriptions(source), schema_without_descriptions(target), ()
    )


def assert_schemas_agree(
    source: Any,
    target: Any,
    *,
    source_label: str = "source schema",
    target_label: str = "target schema",
) -> None:
    """Assert two declared schemas agree, naming every diverging field.

    An opt-in authoring check for composition seams the contracts do not
    enforce — a parent Control's declared input against a child Trigger's
    event payload, or a child workflow's terminal result against the parent's
    declared output. Raises :class:`SchemaAgreementError` whose message lists
    each divergence with its location and both values.
    """

    issues = schema_agreement_issues(source, target)
    if issues:
        message = "; ".join(issues)
        raise SchemaAgreementError(
            (f"{source_label} and {target_label} do not agree: {message}",)
        )
