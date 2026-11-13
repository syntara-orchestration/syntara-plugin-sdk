"""Shared requirement expansion and evidence checks for build and publish."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

RequirementStage = Literal["build", "publish"]


@dataclass(frozen=True)
class Requirement:
    """One versioned SDK policy check and the stages that can evaluate it."""

    name: str
    stages: frozenset[RequirementStage]


REQUIREMENTS = {
    "verified-runtime": Requirement("verified-runtime", frozenset({"build", "publish"}))
}


class RequirementError(ValueError):
    """Raised when requested policy cannot be satisfied by verified evidence."""


def resolve_requirements(
    configured: Sequence[str], command_line: Sequence[str] | None
) -> tuple[str, ...]:
    """Union settings and CLI checks, expanding `all` against this SDK release."""

    requested = [*configured, *(command_line or ())]
    expanded: set[str] = set()
    for value in requested:
        if value == "all":
            expanded.update(REQUIREMENTS)
        elif value in REQUIREMENTS:
            expanded.add(value)
        else:
            raise RequirementError(f"unknown requirement {value!r}")
    return tuple(sorted(expanded))


def evaluate_requirements(
    requirements: Iterable[str],
    runtime_options: Sequence[Mapping[str, Any]],
    *,
    stage: RequirementStage,
) -> tuple[dict[str, str], ...]:
    """Evaluate checks against runtime metadata, never a stored pass flag."""

    results: list[dict[str, str]] = []
    for name in requirements:
        requirement = REQUIREMENTS.get(name)
        if requirement is None:
            raise RequirementError(f"unknown requirement {name!r}")
        if stage not in requirement.stages:
            continue
        if name == "verified-runtime":
            _require_verified_runtimes(runtime_options, stage)
        results.append({"name": name, "stage": stage, "result": "passed"})
    return tuple(results)


def _require_verified_runtimes(
    runtime_options: Sequence[Mapping[str, Any]], stage: RequirementStage
) -> None:
    unverified: list[str] = []
    for option in runtime_options:
        workload = str(option.get("workload", "unknown"))
        option_name = str(option.get("option", "unknown"))
        components = option.get("components", ())
        if not isinstance(components, Sequence) or isinstance(components, str):
            raise RequirementError("runtime evidence has an invalid components list")
        for component in components:
            if not isinstance(component, Mapping) or component.get("verification") != "verified":
                unverified.append(f"{workload}/{option_name}")
    if unverified:
        labels = ", ".join(sorted(set(unverified)))
        raise RequirementError(
            f"{stage} requires every custom-runner image runtime to be verified; "
            f"unverified image option(s): {labels}"
        )
