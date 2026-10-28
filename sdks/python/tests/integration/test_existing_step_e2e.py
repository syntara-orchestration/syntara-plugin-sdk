"""End-to-end contract tests for the checked-in Existing Step example.

Executes the example's steps directly and through the in-process container
adapter, and verifies reproducible artifact and catalog compilation. The
example workspace is fixture data here, mirroring the Podman integration
test in ``test_runtime_image.py``; the example itself stays copyable without
these tests.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from syntara_plugin.sdk import (
    BuildRequest,
    build_catalog_index_artifact,
    build_plugin_artifact,
    compile_workspace,
)
from syntara_plugin.testing import (
    assert_outcomes_equivalent,
    invoke_step,
    invoke_step_in_container,
)

_EXAMPLE = Path(__file__).resolve().parents[4] / "examples" / "existing-step"
sys.path.insert(0, str(_EXAMPLE / "src"))
IMAGE_BINDING = "registry.example.test/existing-step@sha256:" + "a" * 64
IDENTITIES = {
    "action": "example.existing-step.create-ticket",
    "control": "example.existing-step.normalize-records",
    "trigger": "example.existing-step.record-opened",
}
ENTRY_POINTS = {
    "action": "existing_step.runtime:CreateTicketStep",
    "control": "existing_step.runtime:NormalizeRecordsStep",
    "trigger": "existing_step.runtime:RecordOpenedStep",
}


@pytest.mark.parametrize(
    ("kind", "inputs", "expected"),
    [
        (
            "action",
            {"title": "existing step"},
            {"ticket_id": "existing-action:existing step"},
        ),
        (
            "control",
            {"records": ["Alpha", "Beta"]},
            {"count": 2, "normalized": ["alpha", "beta"]},
        ),
        (
            "trigger",
            {"repository": "syntara-plugin-sdk"},
            {
                "event_id": "existing-run:syntara-plugin-sdk",
                "repository": "syntara-plugin-sdk",
            },
        ),
    ],
)
def test_manifest_entry_points_execute_through_both_paths(
    kind: str,
    inputs: dict[str, Any],
    expected: dict[str, Any],
    mock_execution_context,
    step_bindings,
) -> None:
    binding = step_bindings.bind_step(
        ENTRY_POINTS[kind],
        workspace=_EXAMPLE,
        identity=IDENTITIES[kind],
    )
    context = mock_execution_context(
        invocation_id="existing-action",
        workflow_run_id="existing-run",
    )

    direct = invoke_step(binding, inputs, context=context)
    container = invoke_step_in_container(binding, inputs, context=context)

    assert direct.ok
    assert direct.run_calls == 1
    assert direct.output == expected
    assert_outcomes_equivalent(direct, container)


@pytest.mark.parametrize(
    ("kind", "inputs"),
    [
        ("action", {}),
        ("control", {"records": "not-a-list"}),
        ("trigger", {"repository": 42}),
    ],
)
def test_invalid_inputs_block_both_paths_before_the_business_hook(
    kind: str,
    inputs: dict[str, Any],
    mock_execution_context,
    step_bindings,
) -> None:
    binding = step_bindings.bind_step(
        ENTRY_POINTS[kind],
        workspace=_EXAMPLE,
        identity=IDENTITIES[kind],
    )
    context = mock_execution_context()

    direct = invoke_step(binding, inputs, context=context)
    container = invoke_step_in_container(binding, inputs, context=context)

    assert not direct.ok
    assert direct.run_calls == 0
    assert direct.error is not None
    assert direct.error.code == "INVALID_INPUT"
    assert_outcomes_equivalent(direct, container)


def test_compiled_plugin_builds_reproducible_artifact_and_catalog_entry() -> None:
    request = BuildRequest(
        root_manifest=_EXAMPLE / "plugin.yaml",
        image_bindings={"existing-step-workload": IMAGE_BINDING},
    )
    compilation = compile_workspace(request)
    artifact = build_plugin_artifact(compilation)
    repeated_artifact = build_plugin_artifact(compile_workspace(request))

    targets = compilation.descriptor["spec"]["targets"]
    assert [target["identity"] for target in targets] == list(IDENTITIES.values())
    assert compilation.descriptor["spec"]["workloads"] == {
        "existing-step-workload": {
            "abi": "syntara.container/v1alpha2",
            "image": IMAGE_BINDING,
        }
    }
    assert artifact.digest == repeated_artifact.digest

    documentation = compilation.assets["docs/README.md"].content
    catalog_index = _catalog_index(artifact.digest, documentation)
    catalog = build_catalog_index_artifact(catalog_index)
    repeated_catalog = build_catalog_index_artifact(_catalog_index(artifact.digest, documentation))
    assert catalog.digest == repeated_catalog.digest


def _catalog_index(artifact_digest: str, documentation: bytes) -> dict[str, Any]:
    issued_at = datetime(2026, 1, 1, tzinfo=UTC)
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "CatalogIndex",
        "metadata": {
            "sourceId": "existing-step-prototype",
            "generation": 1,
            "issuedAt": _timestamp(issued_at),
            "expiresAt": _timestamp(issued_at + timedelta(hours=1)),
        },
        "spec": {
            "contractVersion": "syntara.io/v1alpha1",
            "entries": [
                {
                    "namespace": "example",
                    "name": "existing-step",
                    "version": "0.1.0",
                    "artifactRepository": "example/plugins/existing-step",
                    "artifactDigest": artifact_digest,
                    "documentationDigest": "sha256:" + sha256(documentation).hexdigest(),
                    "runtimeOwnerships": ["custom-runner"],
                }
            ],
        },
    }


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
