"""Field-level comparison of two declared schemas that must agree."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from .manifests import schema_without_descriptions


class SchemaAgreementError(ValueError):
    """Raised when two declared schemas that must agree diverge."""

    def __init__(
        self, issues: Sequence[str], *, header: str = "declared schemas do not agree"
    ) -> None:
        self.issues = tuple(issues)
        details = "; ".join(self.issues) or "the schemas differ"
        super().__init__(f"{header}: {details}")


def _format_location(path: Sequence[str]) -> str:
    return "<root>" if not path else "".join(f".{segment}" for segment in path)


def _format_value(value: Any) -> str:
    """Render a schema fragment compactly, preferring its keyword name."""

    if isinstance(value, str):
        return f"'{value}'"
    if isinstance(value, Mapping):
        return _canonical(value)
    return str(value)


def _canonical(value: Any) -> str:
    """Render a value so equal JSON content renders identically.

    Mapping keys are sorted by ``repr`` (PyYAML may produce non-string
    keys), list order is preserved (it is meaningful inside members of
    set-valued keywords), and booleans render distinctly from numbers.
    """

    if isinstance(value, Mapping):
        items = sorted(value.items(), key=lambda item: repr(item[0]))
        return (
            "{"
            + ", ".join(f"{_canonical(key)}: {_canonical(item)}" for key, item in items)
            + "}"
        )
    if isinstance(value, list):
        return "[" + ", ".join(_canonical(item) for item in value) + "]"
    return _format_value(value)


def _values_differ(source: Any, target: Any) -> bool:
    """Compare two scalar values so booleans never equal numbers.

    ``True == 1`` in Python, yet ``const: true`` and ``const: 1`` accept
    different values, so a bool/non-bool mismatch counts as divergence
    even when ``==`` says otherwise.
    """

    if isinstance(source, bool) != isinstance(target, bool):
        return True
    return source != target


_SET_VALUED_KEYWORDS = frozenset({"required", "enum", "type"})


def _multiset_issues(
    source: list[Any], target: list[Any], path: tuple[str, ...]
) -> tuple[str, ...]:
    """Compare set-valued keyword members without regard to order."""

    issues: list[str] = []
    location = _format_location(path)
    source_counts = Counter(_canonical(item) for item in source)
    target_counts = Counter(_canonical(item) for item in target)
    for member in sorted(source_counts - target_counts):
        issues.append(
            f"{location}: {member} in the source schema "
            "but not the target schema"
        )
    for member in sorted(target_counts - source_counts):
        issues.append(
            f"{location}: {member} in the target schema "
            "but not the source schema"
        )
    return tuple(issues)


def _agreement_issues(
    source: Any, target: Any, path: tuple[str, ...]
) -> tuple[str, ...]:
    if isinstance(source, Mapping) and isinstance(target, Mapping):
        issues: list[str] = []
        for key in sorted(set(source) | set(target), key=repr):
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
        if path and path[-1] in _SET_VALUED_KEYWORDS:
            return _multiset_issues(source, target, path)
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
    if _values_differ(source, target):
        return (
            f"{_format_location(path)}: {_format_value(source)} in the source "
            f"schema vs {_format_value(target)} in the target schema",
        )
    return ()


def schema_agreement_issues(source: Any, target: Any) -> tuple[str, ...]:
    """Report every field-level divergence between two schemas that must agree.

    ``description`` annotations are ignored (they differ by perspective); a
    property *named* ``description`` is compared like any other. Everything
    else — types, formats, nested schemas, defaults, enums, ``required`` —
    must match, with booleans distinguished from numbers. Set-valued
    keywords (``required``, ``enum``, list-form ``type``) compare members
    without regard to order. Each issue names the location and the diverging
    values, so a failure reads as a repair list rather than a bare
    inequality.
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
        raise SchemaAgreementError(
            issues,
            header=f"{source_label} and {target_label} do not agree",
        )
