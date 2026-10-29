from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from syntara_plugin.contracts import (
    CatalogCheckpoint,
    bundle_digest,
    fixture_content_digest,
    load_bundle,
    load_fixture_bundle,
    validate_catalog_index,
    validate_document,
)
from syntara_plugin.runtime import ContractBundle as RuntimeContractBundle
from syntara_plugin.sdk import ContractBundle as SdkContractBundle

from .helpers import step_document, target_metadata


def _metadata() -> dict[str, str]:
    return {
        "namespace": "acme",
        "name": "github-tools",
        "version": "0.1.0",
        "displayName": "GitHub tools",
        "description": "Creates GitHub issues.",
    }


def test_bundle_has_stable_digest_and_required_contracts() -> None:
    first = load_bundle()
    second = load_bundle()

    assert first.digest == second.digest == bundle_digest(first.documents)
    assert set(first.documents) == {
        "action.schema.json",
        "catalog-index.schema.json",
        "catalog-source-status.schema.json",
        "catalog-source.schema.json",
        "common.schema.json",
        "credential-recipe.schema.json",
        "http-operation.schema.json",
        "http-runner-context.schema.json",
        "installed-identity.schema.json",
        "integration.schema.json",
        "provider.schema.json",
        "plugin.schema.json",
        "plugin-build-settings.schema.json",
        "plugin-descriptor.schema.json",
        "runtime.schema.json",
        "step.schema.json",
        "trigger-driver.schema.json",
        "trigger-event-mapping.schema.json",
        "trigger.schema.json",
        "control.schema.json",
    }
    assert RuntimeContractBundle is SdkContractBundle


def test_plugin_build_settings_schema_is_descriptive_and_strict() -> None:
    bundle = load_bundle()
    schema = bundle.documents["plugin-build-settings.schema.json"]

    assert schema["properties"]["registry"]["properties"]["artifactRepository"]["description"]
    assert schema["properties"]["build"]["properties"]["artifactOutput"]["description"]
    assert schema["properties"]["build"]["properties"]["workload"]["properties"]["context"][
        "description"
    ]
    assert (
        validate_document(
            bundle,
            "plugin_build_settings",
            {
                "apiVersion": "syntara.io/v1alpha1",
                "kind": "PluginBuildSettings",
                "build": {"artifactOutput": "dist/plugin.oci.tar"},
            },
        )
        == []
    )
    assert validate_document(
        bundle,
        "plugin_build_settings",
        {
            "apiVersion": "syntara.io/v1alpha1",
            "kind": "PluginBuildSettings",
            "registry": {"pluginRepository": "acme/plugin"},
        },
    )


def test_valid_plugin_action_and_trigger_documents() -> None:
    bundle = load_bundle()
    plugin = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Plugin",
        "metadata": _metadata(),
        "spec": {
            "documentation": {"path": "docs/README.md"},
            "targets": ["actions/create-issue.yaml", "triggers/pull-request.yaml"],
        },
    }
    action = step_document(
        "Action",
        target_metadata("create-issue", "Create issue", "Creates GitHub issues."),
        {
            "runtime": {
                "kind": "platform-runner",
                "implementation": "syntara.http.request@1.0.0",
                "operation": {
                    "method": "POST",
                    "path": "/repos/{input.owner}/{input.repository}/issues",
                    "requestMap": {"kind": "inline", "value": {"title": {"from": "/title"}}},
                    "responseMap": {"kind": "inline", "value": {"id": {"from": "/id"}}},
                },
            },
            "input": {"kind": "asset", "path": "schemas/create-issue.input.json"},
            "output": {"kind": "inline", "value": {"type": "object"}},
        },
    )
    trigger = step_document(
        "Trigger",
        target_metadata("pull-request", "Pull request", "Receives pull requests."),
        {
            "driver": "webhook.v1",
            "runtime": {"kind": "platform-runner", "implementation": "syntara.webhook.event@1.0.0"},
            "configuration": {"kind": "inline", "value": {"type": "object"}},
            "input": {"kind": "inline", "value": {"type": "object"}},
            "output": {"kind": "asset", "path": "schemas/pull-request.output.json"},
        },
    )

    assert validate_document(bundle, "plugin", plugin) == []
    assert validate_document(bundle, "action", action) == []
    assert validate_document(bundle, "trigger", trigger) == []

    action["spec"]["runtime"]["operation"]["path"] = "/repos/%2Fissues"
    assert validate_document(bundle, "action", action)


def test_plugin_metadata_rejects_out_of_scope_tags() -> None:
    bundle = load_bundle()
    plugin: dict[str, object] = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Plugin",
        "metadata": _metadata(),
        "spec": {
            "documentation": {"path": "docs/README.md"},
            "targets": ["actions/create-issue.yaml"],
        },
    }
    assert validate_document(bundle, "plugin", plugin) == []

    plugin["metadata"] = {**_metadata(), "tags": ["automation"]}

    errors = validate_document(bundle, "plugin", plugin)
    assert any("tags" in error for error in errors)


def test_target_metadata_rejects_out_of_scope_tags() -> None:
    bundle = load_bundle()
    action = step_document(
        "Action",
        target_metadata("create-issue", "Create issue", "Creates GitHub issues."),
        {
            "runtime": {
                "kind": "platform-runner",
                "driver": "http.v1",
                "operation": {"method": "POST", "path": "/issues"},
            },
            "input": {"kind": "inline", "value": {"type": "object"}},
            "output": {"kind": "inline", "value": {"type": "object"}},
        },
    )
    assert validate_document(bundle, "action", action) == []

    action["metadata"] = {
        **target_metadata("create-issue", "Create issue", "Creates GitHub issues."),
        "tags": ["automation"],
    }

    errors = validate_document(bundle, "action", action)
    assert any("tags" in error for error in errors)


def test_custom_runner_action_requires_a_stable_python_entry_point() -> None:
    bundle = load_bundle()
    action = step_document(
        "Action",
        target_metadata("create-issue", "Create issue", "Creates GitHub issues."),
        {
            "runtime": {"kind": "custom-runner"},
            "input": {"kind": "inline", "value": {"type": "object"}},
            "output": {"kind": "inline", "value": {"type": "object"}},
        },
    )

    assert "entryPoint" in " ".join(validate_document(bundle, "action", action))
    action["spec"]["runtime"]["entryPoint"] = "github.actions:CreateIssue"
    assert validate_document(bundle, "action", action) == []


def test_platform_runner_action_rejects_a_custom_runner_entry_point() -> None:
    bundle = load_bundle()
    action = step_document(
        "Action",
        target_metadata("create-issue", "Create issue", "Creates GitHub issues."),
        {
            "runtime": {
                "kind": "platform-runner",
                "implementation": "syntara.http.request@1.0.0",
                "operation": {"method": "POST", "path": "/repos/{input.owner}/issues"},
                "entryPoint": "github.actions:CreateIssue",
            },
            "input": {"kind": "inline", "value": {"type": "object"}},
            "output": {"kind": "inline", "value": {"type": "object"}},
        },
    )

    assert "entryPoint" in " ".join(validate_document(bundle, "action", action))
    del action["spec"]["runtime"]["entryPoint"]
    assert validate_document(bundle, "action", action) == []


@pytest.mark.parametrize("kind", ["Action", "Control", "Trigger"])
@pytest.mark.parametrize(
    "implementation",
    ["acme.tools.run@1.2.3", "example.hello-world.hello@0.1.0", "acme.tools.run@1.2.3-rc.1"],
)
def test_platform_runner_accepts_registered_step_references(kind: str, implementation: str) -> None:
    source = {"kind": "inline", "value": {"type": "object"}}
    spec: dict[str, Any] = {
        "runtime": {"kind": "platform-runner", "implementation": implementation},
        "input": source,
        "output": source,
    }
    if kind == "Control":
        spec["invocation"] = {"kind": "orchestration"}
    elif kind == "Trigger":
        spec.update({"driver": "webhook.v1", "configuration": source})
    document = step_document(kind, target_metadata("step", "Step", "A step."), spec)
    assert validate_document(load_bundle(), kind.lower(), document) == []


@pytest.mark.parametrize(
    "implementation",
    ["http.v1", "acme.tools.run", "acme.tools.run@1.2", "acme.tools.run@latest", "acme.tools.run@01.2.3",
     "acme.tools.run.more@1.2.3", "Acme.tools.run@1.2.3", "acme.tools.run@1.2.3\n"],
)
def test_platform_runner_rejects_malformed_implementation_references(implementation: str) -> None:
    source = {"kind": "inline", "value": {"type": "object"}}
    document = step_document(
        "Action", target_metadata("step", "Step", "A step."),
        {"runtime": {"kind": "platform-runner", "implementation": implementation},
         "input": source, "output": source},
    )
    assert validate_document(load_bundle(), "action", document)


def test_platform_runner_rejects_legacy_driver_and_custom_runner_rejects_implementation() -> None:
    source = {"kind": "inline", "value": {"type": "object"}}
    document = step_document(
        "Action", target_metadata("step", "Step", "A step."),
        {"runtime": {"kind": "platform-runner", "driver": "http.v1"},
         "input": source, "output": source},
    )
    assert validate_document(load_bundle(), "action", document)
    document["spec"]["runtime"] = {
        "kind": "custom-runner", "entryPoint": "steps:Run",
        "implementation": "acme.tools.run@1.2.3",
    }
    assert validate_document(load_bundle(), "action", document)


@pytest.mark.parametrize(
    "entry_point",
    ["steps..bad:Run", "steps.bad.:Run", "steps:Run.More", "steps:Run\n"],
)
def test_step_contract_rejects_malformed_entry_points(entry_point: str) -> None:
    bundle = load_bundle()
    action = step_document(
        "Action",
        target_metadata("create-issue", "Create issue", "Creates GitHub issues."),
        {
            "runtime": {"kind": "custom-runner", "entryPoint": entry_point},
            "input": {"kind": "inline", "value": {"type": "object"}},
            "output": {"kind": "inline", "value": {"type": "object"}},
        },
    )

    errors = validate_document(bundle, "action", action)
    assert any("entryPoint" in error for error in errors)


def test_concrete_step_types_accept_an_optional_runtime_error_schema() -> None:
    bundle = load_bundle()
    source = {"kind": "inline", "value": {"type": "object"}}
    action = step_document(
        "Action",
        target_metadata("create-issue", "Create issue", "Creates GitHub issues."),
        {
            "runtime": {"kind": "custom-runner", "entryPoint": "github.actions:CreateIssue"},
            "input": source,
            "output": source,
            "error": source,
        },
    )
    control = step_document(
        "Control",
        target_metadata("control", "Control", "Runs control flow."),
        {
            "invocation": {"kind": "orchestration"},
            "runtime": {"kind": "custom-runner", "entryPoint": "flows:Control"},
            "input": source,
            "output": source,
            "error": source,
        },
    )
    trigger = step_document(
        "Trigger",
        target_metadata("trigger", "Trigger", "Receives events."),
        {
            "driver": "webhook.v1",
            "runtime": {"kind": "platform-runner", "implementation": "syntara.webhook.event@1.0.0"},
            "configuration": source,
            "input": source,
            "output": source,
            "error": source,
        },
    )

    documents = (("action", action), ("control", control), ("trigger", trigger))
    for kind, document in documents:
        assert validate_document(bundle, kind, document) == []
        assert validate_document(bundle, "step", document) == []


def test_step_category_contracts_are_strict_and_preserve_their_distinct_semantics() -> None:
    bundle = load_bundle()
    source = {"kind": "inline", "value": {"type": "object"}}
    metadata = {
        "name": "example",
        "displayName": "Example",
        "description": "One category-specific Step type.",
    }
    control = step_document(
        "Control",
        metadata,
        {
            "invocation": {"kind": "workflow-invocation", "workflow": "acme.release.deploy"},
            "runtime": {"kind": "custom-runner", "entryPoint": "flows:Deploy"},
            "input": source,
            "output": source,
        },
    )
    trigger = step_document(
        "Trigger",
        metadata,
        {
            "driver": "webhook.v1",
            "runtime": {"kind": "platform-runner", "implementation": "syntara.webhook.event@1.0.0"},
            "configuration": {
                "kind": "inline",
                "value": {"type": "object", "properties": {"secret": {"type": "string"}}},
            },
            "input": {
                "kind": "inline",
                "value": {"type": "object", "properties": {"event": {"type": "string"}}},
            },
            "output": {
                "kind": "inline",
                "value": {"type": "object", "properties": {"runId": {"type": "string"}}},
            },
        },
    )

    assert validate_document(bundle, "control", control) == []
    assert validate_document(bundle, "trigger", trigger) == []

    control["spec"]["runtime"] = {"kind": "custom-runner", "entryPoint": "flows:Deploy"}
    assert validate_document(bundle, "control", control) == []
    trigger["spec"]["driver"] = "webhook.v0"
    assert validate_document(bundle, "trigger", trigger)


def test_http_mapping_contract_accepts_only_bounded_pointer_or_literal_values() -> None:
    bundle = load_bundle()

    assert (
        validate_document(
            bundle,
            "http_mapping",
            {"title": {"from": "/title"}, "private": {"literal": False}},
        )
        == []
    )
    assert validate_document(bundle, "http_mapping", {"title": {"from": "title"}})


def test_contracts_reject_legacy_images_and_unsafe_asset_paths() -> None:
    bundle = load_bundle()
    legacy_plugin = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Plugin",
        "metadata": _metadata(),
        "spec": {"targets": ["../escape.yaml"], "images": []},
    }
    unsafe_action = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Action",
        "metadata": _metadata(),
        "spec": {
            "runtime": {"kind": "platform-runner", "abi": "syntara.container/v1alpha1"},
            "input": {"kind": "asset", "path": "../secret.json"},
            "output": {"kind": "inline", "value": {}},
        },
    }

    assert validate_document(bundle, "plugin", legacy_plugin)
    assert validate_document(bundle, "action", unsafe_action)


def test_fixture_corpus_exercises_valid_and_invalid_documents() -> None:
    bundle = load_bundle()
    fixtures = Path(__file__).parents[4] / "contracts" / "fixtures"
    valid_plugin = json.loads((fixtures / "plugin.valid.json").read_text())
    invalid_action = json.loads((fixtures / "action.invalid.json").read_text())

    assert validate_document(bundle, "plugin", valid_plugin) == []
    errors = validate_document(bundle, "action", invalid_action)
    assert any("version" in error for error in errors)
    assert any("abi" in error for error in errors)
    assert any("path" in error for error in errors)

    control = json.loads((fixtures / "control.invalid.json").read_text())
    trigger = json.loads((fixtures / "trigger.invalid.json").read_text())
    assert validate_document(bundle, "control", control)
    assert validate_document(bundle, "trigger", trigger)


def test_pinned_catalog_fixture_bundle_exercises_control_plane_contracts() -> None:
    bundle = load_bundle()
    fixture_bundle = load_fixture_bundle()

    assert fixture_bundle.contract_bundle_digest == bundle.digest
    assert fixture_bundle.content_digest.startswith("sha256:")
    for case in fixture_bundle.document_cases:
        document = fixture_bundle.document(case["path"])
        assert validate_document(bundle, case["kind"], document) == []

    for case in fixture_bundle.catalog_cases:
        document = fixture_bundle.document(case["path"])
        checkpoint = CatalogCheckpoint(
            generation=case["checkpoint"]["generation"],
            index_digest=case["checkpoint"]["indexDigest"],
        )
        diagnostics = validate_catalog_index(
            bundle,
            document,
            source_id=case["sourceId"],
            now=datetime.fromisoformat(case["now"].replace("Z", "+00:00")),
            checkpoint=checkpoint,
        )
        assert [item.code for item in diagnostics] == case["expectedCodes"]


def test_fixture_content_digest_changes_when_a_manifest_listed_document_changes() -> None:
    fixture_bundle = load_fixture_bundle()
    changed_documents = dict(fixture_bundle.documents)
    changed_documents["catalog/valid-20.json"] = {"tampered": True}

    assert fixture_content_digest(changed_documents) != fixture_bundle.content_digest


def test_control_plane_fixtures_are_readable_by_each_python_package_boundary() -> None:
    bundle = load_bundle()
    fixture = json.loads(
        (
            Path(__file__).parents[4] / "contracts" / "fixtures" / "installed-identity.valid.json"
        ).read_text()
    )

    # Both package boundaries receive the same immutable contract bundle rather
    # than language-local schema copies. This fixture covers contract data only;
    # it does not exercise control-plane behavior.
    assert RuntimeContractBundle is SdkContractBundle
    assert validate_document(bundle, "installed_identity", fixture) == []


def test_catalog_contract_rejects_incomplete_accepted_status_and_naive_time() -> None:
    bundle = load_bundle()
    incomplete_status = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "CatalogSourceStatus",
        "metadata": {"sourceId": "local-quay", "observedAt": "2026-10-06T12:00:00Z"},
        "status": {"state": "accepted"},
    }
    valid_index = json.loads(
        (
            Path(__file__).parents[4] / "contracts" / "fixtures" / "catalog" / "valid-20.json"
        ).read_text()
    )

    assert validate_document(bundle, "catalog_source_status", incomplete_status)
    with pytest.raises(ValueError, match="timezone-aware"):
        validate_catalog_index(
            bundle,
            valid_index,
            source_id="local-quay",
            now=datetime(2026, 10, 6, 13, 0, 0),
        )
