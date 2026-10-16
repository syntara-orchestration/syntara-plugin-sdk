from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

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
        "runtime.schema.json",
        "trigger-driver.schema.json",
        "trigger-event-mapping.schema.json",
        "trigger.schema.json",
    }
    assert RuntimeContractBundle is SdkContractBundle


def test_plugin_build_settings_schema_is_descriptive_and_strict() -> None:
    bundle = load_bundle()
    schema = bundle.documents["plugin-build-settings.schema.json"]

    assert schema["properties"]["registry"]["properties"]["artifactRepository"]["description"]
    assert schema["properties"]["build"]["properties"]["artifactOutput"]["description"]
    assert schema["properties"]["build"]["properties"]["workload"]["properties"][
        "context"
    ]["description"]
    assert validate_document(
        bundle,
        "plugin_build_settings",
        {
            "apiVersion": "syntara.io/v1alpha1",
            "kind": "PluginBuildSettings",
            "build": {"artifactOutput": "dist/plugin.oci.tar"},
        },
    ) == []
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
    action = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Action",
        "metadata": {
            "name": "create-issue",
            "displayName": "Create issue",
            "description": "Creates GitHub issues.",
        },
        "spec": {
            "runtime": {
                "kind": "platform-runner",
                "driver": "http.v1",
                "operation": {
                    "method": "POST",
                    "path": "/repos/{input.owner}/{input.repository}/issues",
                    "requestMap": {"kind": "inline", "value": {"title": {"from": "/title"}}},
                    "responseMap": {"kind": "inline", "value": {"id": {"from": "/id"}}},
                },
            },
            "input": {"kind": "asset", "path": "schemas/create-issue.input.json"},
            "output": {"kind": "inline", "value": {"type": "object"}},
            "error": {"kind": "inline", "value": {"type": "object"}},
        },
    }
    trigger = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Trigger",
        "metadata": {
            "name": "pull-request",
            "displayName": "Pull request",
            "description": "Receives pull requests.",
        },
        "spec": {
            "driver": "webhook.v1",
            "configuration": {"kind": "inline", "value": {"type": "object"}},
            "output": {"kind": "asset", "path": "schemas/pull-request.output.json"},
        },
    }

    assert validate_document(bundle, "plugin", plugin) == []
    assert validate_document(bundle, "action", action) == []
    assert validate_document(bundle, "trigger", trigger) == []

    action["spec"]["runtime"]["operation"]["path"] = "/repos/%2Fissues"
    assert validate_document(bundle, "action", action)


def test_custom_workload_action_requires_a_stable_python_entry_point() -> None:
    bundle = load_bundle()
    action = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Action",
        "metadata": {
            "name": "create-issue",
            "displayName": "Create issue",
            "description": "Creates GitHub issues.",
        },
        "spec": {
            "runtime": {"kind": "custom-workload"},
            "input": {"kind": "inline", "value": {"type": "object"}},
            "output": {"kind": "inline", "value": {"type": "object"}},
            "error": {"kind": "inline", "value": {"type": "object"}},
        },
    }

    assert "entryPoint" in " ".join(validate_document(bundle, "action", action))
    action["spec"]["runtime"]["entryPoint"] = "github.actions:CreateIssue"
    assert validate_document(bundle, "action", action) == []


def test_action_error_document_is_optional() -> None:
    bundle = load_bundle()
    action = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Action",
        "metadata": {
            "name": "create-issue",
            "displayName": "Create issue",
            "description": "Creates GitHub issues.",
        },
        "spec": {
            "runtime": {"kind": "custom-workload", "entryPoint": "github.actions:CreateIssue"},
            "input": {"kind": "inline", "value": {"type": "object"}},
            "output": {"kind": "inline", "value": {"type": "object"}},
        },
    }

    assert validate_document(bundle, "action", action) == []

    action["spec"]["error"] = {"kind": "inline", "value": {"type": "object"}}
    assert validate_document(bundle, "action", action) == []


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
    # than language-local schema copies. The control plane itself is not part
    # of the runtime package and therefore cannot execute from this fixture.
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
