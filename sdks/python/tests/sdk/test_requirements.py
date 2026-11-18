from __future__ import annotations

import pytest
from syntara_plugin.sdk.requirements import RequirementError, evaluate_requirements


def test_verified_runtime_requires_annotated_metadata_verification() -> None:
    verified = [
        {
            "workload": "python",
            "option": "base",
            "verification": "verified",
            "components": [
                {"name": "python", "verification": "verified"},
                {"name": "curl", "verification": "unverified"},
            ],
        }
    ]

    assert evaluate_requirements(["verified-runtime"], verified, stage="build") == (
        {"name": "verified-runtime", "stage": "build", "result": "passed"},
    )


def test_verified_runtime_rejects_an_unverified_annotation() -> None:
    with pytest.raises(RequirementError, match="python/base"):
        evaluate_requirements(
            ["verified-runtime"],
            [
                {
                    "workload": "python",
                    "option": "base",
                    "verification": "unverified",
                    "components": [],
                }
            ],
            stage="publish",
        )


def test_default_policy_allows_an_unverified_runtime() -> None:
    assert evaluate_requirements(
        [],
        [{"workload": "ruby", "option": "base", "verification": "unverified"}],
        stage="build",
    ) == ()
