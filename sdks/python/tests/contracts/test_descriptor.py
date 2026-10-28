"""Tests for the contract-owned descriptor schema digest rule."""

from __future__ import annotations

import pytest
from syntara_plugin.contracts import (
    canonical_json_digest,
    is_digest_image_reference,
    materialized_schemas,
    schema_digests,
    validate_plugin_descriptor_semantics,
)


def _target(identity: str = "acme.plugin.step") -> dict[str, object]:
    target: dict[str, object] = {
        "identity": identity,
        "kind": "Action",
        "input": {"type": "object"},
        "output": {"type": "object"},
        "runtime": {
            "kind": "custom-runner",
            "abi": "syntara.container/v1alpha2",
            "entryPoint": "plugin:Step",
            "workload": "runner",
        },
    }
    target["schemaDigests"] = schema_digests(target)
    return target


def _descriptor(targets: list[dict[str, object]]) -> dict[str, object]:
    return {
        "spec": {
            "targets": targets,
            "workloads": {
                "runner": {
                    "abi": "syntara.container/v1alpha2",
                    "image": "quay.io/acme/runner@sha256:" + "a" * 64,
                }
            },
        }
    }


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


def test_descriptor_semantics_reject_trigger_driver_mismatch() -> None:
    target = _target()
    target.update(
        {
            "kind": "Trigger",
            "configuration": {"type": "object"},
            "driver": "webhook.v1",
            "runtime": {"kind": "platform-runner", "driver": "schedule.v1"},
        }
    )
    target["schemaDigests"] = schema_digests(target)

    errors = validate_plugin_descriptor_semantics(_descriptor([target]))

    assert errors == (
        "trigger target 'acme.plugin.step' driver does not match its platform-runner runtime driver",
    )


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
