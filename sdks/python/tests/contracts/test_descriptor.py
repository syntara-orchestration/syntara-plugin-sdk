"""Tests for the contract-owned descriptor schema digest rule."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from syntara_plugin.contracts import (
    canonical_json_digest,
    is_digest_image_reference,
    load_bundle,
    materialized_schemas,
    plugin_revision_id,
    registration_manifest,
    revision_errors,
    schema_digests,
    step_revision_id,
    validate_document,
    validate_plugin_descriptor_semantics,
    validate_registration_manifest,
)


def _target(identity: str = "acme.plugin.step") -> dict[str, object]:
    target: dict[str, object] = {
        "identity": identity,
        "kind": "Action",
        "input": {"type": "object"},
        "output": {"type": "object"},
        "runtime": {
            "kind": "custom-runner",
            "abi": "syntara.container/v1alpha1",
            "entryPoint": "plugin:Step",
            "workload": "runner",
        },
    }
    target["schemaDigests"] = schema_digests(target)
    return target


def _descriptor(targets: list[dict[str, object]]) -> dict[str, object]:
    descriptor = {
        "metadata": {"namespace": "acme", "name": "plugin", "version": "0.1.0"},
        "spec": {
            "sdkVersion": "0.1.0-alpha.1",
            "contractBundleDigest": "sha256:" + "b" * 64,
            "targets": targets,
            "workloads": {
                "runner": {
                    "abi": "syntara.container/v1alpha1",
                    "image": "quay.io/acme/runner@sha256:" + "a" * 64,
                }
            },
        }
    }
    for target in targets:
        target["step_revision_id"] = step_revision_id(descriptor, target)
    descriptor["spec"]["plugin_revision_id"] = plugin_revision_id(descriptor)
    return descriptor


def test_materialized_schema_fields_and_digests_are_canonical() -> None:
    target = {
        "configuration": {"type": "object"},
        "input": {"properties": {"name": {"type": "string"}}, "type": "object"},
        "output": [],
        "sourcePath": "steps/example.yaml",
        "schemaDigests": {"input": "stale"},
    }

    assert materialized_schemas(target) == {
        "configuration": target["configuration"],
        "input": target["input"],
    }
    assert schema_digests(target) == {
        "configuration": canonical_json_digest(target["configuration"]),
        "input": canonical_json_digest(target["input"]),
    }


def test_descriptor_semantics_accept_a_valid_compiled_target() -> None:
    assert validate_plugin_descriptor_semantics(_descriptor([_target()])) == ()


def test_revision_fixture_is_a_stable_projection_vector() -> None:
    fixture = json.loads(
        (Path(__file__).parents[4] / "contracts/fixtures/plugin-descriptor.valid.json").read_text()
    )
    assert revision_errors(fixture) == ()
    assert validate_plugin_descriptor_semantics(fixture) == ()
    manifest = registration_manifest(fixture, "plugin-workload")
    assert len(manifest["steps"]) == 2
    assert validate_document(load_bundle(), "registration_manifest", manifest) == []
    validate_registration_manifest(fixture, "plugin-workload", manifest)

    for mutation in ("entryPoint", "abi", "missing", "extra"):
        altered = deepcopy(manifest)
        revision = next(iter(altered["steps"]))
        if mutation == "missing":
            del altered["steps"][revision]
        elif mutation == "extra":
            altered["unexpected"] = True
        else:
            altered["steps"][revision][mutation] = "incorrect"
        with pytest.raises(ValueError):
            validate_registration_manifest(fixture, "plugin-workload", altered)


def test_revision_ids_commit_to_sdk_plugin_and_step_contracts() -> None:
    descriptor = _descriptor([_target()])
    original_plugin = plugin_revision_id(descriptor)
    original_step = step_revision_id(descriptor, descriptor["spec"]["targets"][0])
    descriptor["spec"]["sdkVersion"] = "0.1.0-alpha.2"
    assert plugin_revision_id(descriptor) != original_plugin
    assert step_revision_id(descriptor, descriptor["spec"]["targets"][0]) != original_step
    assert len(revision_errors(descriptor)) == 2
    descriptor["spec"]["plugin_revision_id"] = "sha256:" + "0" * 64
    assert "plugin revision ID does not match its contract" in revision_errors(descriptor)


def test_platform_implementation_changes_step_and_plugin_revision() -> None:
    target = _target()
    target["runtime"] = {"kind": "platform-runner", "implementation": "acme.tools.run@1.0.0"}
    descriptor = _descriptor([target])
    original_step = step_revision_id(descriptor, target)
    original_plugin = plugin_revision_id(descriptor)
    target["runtime"]["implementation"] = "acme.tools.run@2.0.0"
    assert step_revision_id(descriptor, target) != original_step
    assert plugin_revision_id(descriptor) != original_plugin


@pytest.mark.parametrize(
    ("workload_id", "abi", "provider", "changes_revision"),
    [
        ("plugin-workload", "syntara.container/v1alpha1", None, False),
        ("provider-image", "syntara.provider/v1alpha1", "provider", True),
        ("provider-image", "syntara.container/v1alpha1", "provider", True),
        ("plugin-workload", "syntara.container/v1alpha1", "provider", False),
        ("unused-image", "syntara.container/v1alpha1", None, True),
    ],
)
def test_plugin_revision_tracks_non_runner_workload_images(
    workload_id: str, abi: str, provider: str | None, changes_revision: bool
) -> None:
    descriptor = json.loads(
        (Path(__file__).parents[4] / "contracts/fixtures/plugin-descriptor.valid.json").read_text()
    )
    workload = {
        "abi": abi,
        "image": "quay.io/acme/workload@sha256:" + "a" * 64,
    }
    if provider is not None:
        workload["provider"] = provider
    descriptor["spec"]["workloads"][workload_id] = workload
    descriptor["spec"]["plugin_revision_id"] = plugin_revision_id(descriptor)
    assert validate_document(load_bundle(), "plugin_descriptor", descriptor) == []
    assert validate_plugin_descriptor_semantics(descriptor) == ()

    original_revision = plugin_revision_id(descriptor)
    workload["image"] = "quay.io/acme/workload@sha256:" + "b" * 64
    assert (plugin_revision_id(descriptor) != original_revision) is changes_revision


def test_descriptor_semantics_reject_duplicate_targets_and_stale_schema_digests() -> None:
    first = _target()
    second = _target()
    second["schemaDigests"] = {"input": "sha256:" + "0" * 64, "output": "sha256:" + "0" * 64}

    errors = validate_plugin_descriptor_semantics(_descriptor([first, second]))

    assert "plugin descriptor contains duplicate target identities" in errors
    assert "target 'acme.plugin.step' input schema digest does not match its schema" in errors


def test_descriptor_semantics_reject_custom_runner_without_a_container_workload() -> None:
    target = _target()
    descriptor = _descriptor([target])
    descriptor["spec"]["workloads"] = {
        "runner": {
            "abi": "syntara.provider/v1alpha1",
            "image": "quay.io/acme/runner@sha256:" + "a" * 64,
        }
    }

    errors = validate_plugin_descriptor_semantics(descriptor)

    assert "custom-runner target 'acme.plugin.step' workload has an invalid ABI" in errors


def test_descriptor_semantics_accept_trigger_driver_and_implementation() -> None:
    target = _target()
    target.update(
        {
            "kind": "Trigger",
            "configuration": {"type": "object"},
            "driver": "webhook.v1",
            "runtime": {"kind": "platform-runner", "implementation": "syntara.webhook.event@1.0.0"},
        }
    )
    target["schemaDigests"] = schema_digests(target)

    errors = validate_plugin_descriptor_semantics(_descriptor([target]))

    assert errors == ()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("quay.io/acme/runner@sha256:" + "a" * 64, True),
        ("localhost:8443/acme/runner@sha256:" + "b" * 64, True),
        ("quay..io/acme/runner@sha256:" + "a" * 64, False),
        ("quay.io/acme/runner-@sha256:" + "a" * 64, False),
        ("quay.io/acme/runner:latest", False),
        (None, False),
    ],
)
def test_digest_image_reference_rejects_mutable_and_malformed_values(
    value: object, expected: bool
) -> None:
    assert is_digest_image_reference(value) is expected
