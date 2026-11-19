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
    "runtime-metadata-match": Requirement("runtime-metadata-match", frozenset({"build", "publish"}))
}


class RequirementError(ValueError):
    """Raised when requested policy cannot be satisfied by runtime probe evidence."""


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
        if name == "runtime-metadata-match":
            _require_runtime_metadata_match(runtime_options, stage)
        results.append({"name": name, "stage": stage, "result": "passed"})
    return tuple(results)


def _require_runtime_metadata_match(
    runtime_options: Sequence[Mapping[str, Any]], stage: RequirementStage
) -> None:
    if not runtime_options:
        raise RequirementError(f"{stage} has no runtime image options to probe")
    unmatched: list[str] = []
    for option in runtime_options:
        workload = str(option.get("workload", "unknown"))
        option_name = str(option.get("option", "unknown"))
        runtime = option.get("runtime")
        if isinstance(runtime, Mapping):
            language = runtime.get("language")
            version = runtime.get("version")
            probe_status = runtime.get("probeStatus")
            components = runtime.get("components")
        else:
            language = option.get("language")
            version = option.get("version")
            probe_status = option.get("probeStatus")
            components = option.get("components")

        component_list = components if isinstance(components, Sequence) else ()
        matching = [
            component
            for component in component_list
            if isinstance(component, Mapping)
            and isinstance(language, str)
            and isinstance(component.get("name"), str)
            and component.get("name", "").casefold() == language.casefold()
        ]
        components_match = bool(component_list) and all(
            isinstance(component, Mapping)
            and component.get("probeStatus") == "matched"
            and isinstance(component.get("version"), str)
            and bool(component["version"].strip())
            for component in component_list
        )
        annotated_runtime_matches = (
            len(matching) == 1
            and matching[0].get("version") == version
            and isinstance(version, str)
            and bool(version.strip())
        )
        if (
            probe_status != "matched"
            or not components_match
            or not annotated_runtime_matches
        ):
            unmatched.append(f"{workload}/{option_name}")
    if unmatched:
        labels = ", ".join(sorted(set(unmatched)))
        raise RequirementError(
            f"{stage} requires every custom-runner image option to have a matching "
            f"runtime probe for every declared component; unsupported or incomplete "
            f"image option(s): {labels}"
        )
