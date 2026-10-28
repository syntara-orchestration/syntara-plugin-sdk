"""Semantic rules for compiled plugin descriptors."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .canonical import canonical_json_digest
from .image import is_digest_image_reference

_MATERIALIZED_SCHEMA_FIELDS = frozenset(
    {"configuration", "credentialSchema", "error", "input", "output"}
)


def materialized_schemas(target: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Return descriptor fields that contain materialized JSON Schemas."""

    return {
        field_name: value
        for field_name, value in target.items()
        if field_name in _MATERIALIZED_SCHEMA_FIELDS and isinstance(value, Mapping)
    }


def schema_digests(target: Mapping[str, Any]) -> dict[str, str]:
    """Return canonical digests for every materialized schema in a target."""

    return {
        field_name: canonical_json_digest(schema)
        for field_name, schema in sorted(materialized_schemas(target).items())
    }


def validate_plugin_descriptor_semantics(
    descriptor: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return stable diagnostics for semantic descriptor rules.

    JSON Schema validation establishes the descriptor's shape. These rules
    establish relationships between otherwise valid fields and are shared by
    artifact publishers and installed-artifact consumers.
    """

    specification = descriptor.get("spec")
    if not isinstance(specification, Mapping):
        return ("plugin descriptor spec must be an object",)

    targets = specification.get("targets")
    workloads = specification.get("workloads")
    errors: list[str] = []
    if not isinstance(targets, list):
        errors.append("plugin descriptor targets must be an array")
        targets = []
    if not isinstance(workloads, Mapping):
        errors.append("plugin descriptor workloads must be an object")
        workloads = {}

    _validate_target_identities(targets, errors)
    _validate_workloads(targets, workloads, errors)
    _validate_schema_digests(targets, errors)
    _validate_trigger_drivers(targets, errors)
    return tuple(errors)


def _validate_target_identities(targets: list[object], errors: list[str]) -> None:
    """Require every compiled target identity to be unique."""

    identities: set[str] = set()
    for index, target in enumerate(targets):
        if not isinstance(target, Mapping):
            errors.append(f"plugin descriptor target at index {index} must be an object")
            continue
        identity = target.get("identity")
        if not isinstance(identity, str):
            errors.append(f"plugin descriptor target at index {index} has an invalid identity")
            continue
        if identity in identities:
            errors.append("plugin descriptor contains duplicate target identities")
        identities.add(identity)


def _validate_workloads(
    targets: list[object],
    workloads: Mapping[str, Any],
    errors: list[str],
) -> None:
    """Validate immutable workload images and custom-runner workload links."""

    for workload_id, workload in workloads.items():
        image = workload.get("image") if isinstance(workload, Mapping) else None
        if not is_digest_image_reference(image):
            errors.append(
                f"plugin descriptor workload {workload_id!r} image must be pinned to a sha256 digest"
            )

    for target in targets:
        if not isinstance(target, Mapping):
            continue
        runtime = target.get("runtime")
        if not isinstance(runtime, Mapping) or runtime.get("kind") != "custom-runner":
            continue
        identity = target.get("identity", "<unknown>")
        workload_id = runtime.get("workload")
        workload = workloads.get(workload_id) if isinstance(workload_id, str) else None
        if not isinstance(workload, Mapping):
            errors.append(
                f"custom-runner target {identity!r} references a missing workload"
            )
        elif workload.get("abi") != "syntara.container/v1alpha2":
            errors.append(
                f"custom-runner target {identity!r} workload has an invalid ABI"
            )


def _validate_schema_digests(targets: list[object], errors: list[str]) -> None:
    """Require schema digests to bind every materialized schema exactly."""

    for target in targets:
        if not isinstance(target, Mapping):
            continue
        identity = target.get("identity", "<unknown>")
        digests = target.get("schemaDigests")
        if not isinstance(digests, Mapping):
            errors.append(f"target {identity!r} schema digests are invalid")
            continue
        schemas = materialized_schemas(target)
        if set(digests) != set(schemas):
            errors.append(
                f"target {identity!r} schema digests do not match its materialized schemas"
            )
            continue
        expected_digests = schema_digests(target)
        for field_name, expected_digest in expected_digests.items():
            if digests.get(field_name) != expected_digest:
                errors.append(
                    f"target {identity!r} {field_name} schema digest does not match its schema"
                )


def _validate_trigger_drivers(targets: list[object], errors: list[str]) -> None:
    """Require platform Trigger targets to use one consistent driver."""

    for target in targets:
        if not isinstance(target, Mapping) or target.get("kind") != "Trigger":
            continue
        runtime = target.get("runtime")
        if not isinstance(runtime, Mapping) or runtime.get("kind") != "platform-runner":
            continue
        if target.get("driver") != runtime.get("driver"):
            identity = target.get("identity", "<unknown>")
            errors.append(
                f"trigger target {identity!r} driver does not match its platform-runner runtime driver"
            )
