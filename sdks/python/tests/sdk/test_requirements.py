from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from syntara_plugin.sdk.requirements import RequirementError, evaluate_requirements


def _runtime_option(
    *,
    probe_status: str = "matched",
    components: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "workload": "python",
        "option": "base",
        "language": "python",
        "version": "3.12.8",
        "probeStatus": probe_status,
        "components": components
        if components is not None
        else [{"name": "python", "version": "3.12.8", "probeStatus": "matched"}],
    }


def test_runtime_metadata_match_requires_all_declared_components_to_match() -> None:
    evidence = [
        _runtime_option(
            components=[
                {"name": "python", "version": "3.12.8", "probeStatus": "matched"},
                {"name": "bash", "version": "5.2.26", "probeStatus": "matched"},
            ]
        )
    ]

    assert evaluate_requirements(["runtime-metadata-match"], evidence, stage="build") == (
        {"name": "runtime-metadata-match", "stage": "build", "result": "passed"},
    )


@pytest.mark.parametrize(
    "evidence",
    [
        [],
        [_runtime_option(components=[])],
        [_runtime_option(components=[{"name": "python", "probeStatus": "matched"}])],
        [
            _runtime_option(
                components=[
                    {"name": "python", "version": "3.12.8", "probeStatus": "matched"},
                    {"name": "curl", "probeStatus": "unsupported"},
                ]
            )
        ],
        [
            _runtime_option(
                components=[{"name": "python", "version": "3.12.9", "probeStatus": "matched"}]
            )
        ],
        [_runtime_option(probe_status="unsupported")],
    ],
    ids=["no-options", "no-components", "missing-version", "unsupported-component", "version-mismatch", "unsupported-runtime"],
)
def test_runtime_metadata_match_rejects_incomplete_or_unsupported_evidence(
    evidence: list[Mapping[str, Any]],
) -> None:
    with pytest.raises(RequirementError, match="probe"):
        evaluate_requirements(["runtime-metadata-match"], evidence, stage="publish")


def test_default_policy_allows_unsupported_runtime_metadata() -> None:
    assert evaluate_requirements(
        [], [_runtime_option(probe_status="unsupported")], stage="build"
    ) == ()
