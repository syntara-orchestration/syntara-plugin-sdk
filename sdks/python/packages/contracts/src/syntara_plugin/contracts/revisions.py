"""Versioned canonical compiled-contract identities and image registration data."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .bundle import load_bundle, validate_document
from .canonical import canonical_json_digest

REVISION_PROJECTION_VERSION = "syntara.compiled-contract/v1"
STEP_KINDS = frozenset({"Action", "Control", "Trigger"})


def step_revision_id(descriptor: Mapping[str, Any], target: Mapping[str, Any]) -> str:
    """Identify one step contract, scoped to the declared plugin release."""

    metadata = descriptor["metadata"]
    spec = descriptor["spec"]
    return canonical_json_digest(
        {
            "projection": REVISION_PROJECTION_VERSION,
            "kind": "step",
            "plugin": {key: metadata[key] for key in ("namespace", "name", "version")},
            "sdkVersion": spec["sdkVersion"],
            "contractBundleDigest": spec["contractBundleDigest"],
            "target": {key: value for key, value in target.items() if key != "step_revision_id"},
        }
    )


def plugin_revision_id(descriptor: Mapping[str, Any]) -> str:
    """Identify the compiled contract, excluding custom-runner bindings and IDs."""

    spec = dict(descriptor["spec"])
    spec.pop("plugin_revision_id", None)
    spec["targets"] = [
        {key: value for key, value in target.items() if key != "step_revision_id"}
        for target in spec["targets"]
    ]
    # Runner workload IDs and ABIs are carried in the step runtime contract.
    # Their image bindings may not exist until after this projection is hashed.
    runner_workloads = {
        runtime["workload"]
        for target in spec["targets"]
        if target.get("kind") in STEP_KINDS
        for runtime in [target.get("runtime")]
        if isinstance(runtime, Mapping)
        and runtime.get("kind") == "custom-runner"
        and isinstance(runtime.get("workload"), str)
    }
    spec["workloads"] = {
        key: value for key, value in spec["workloads"].items() if key not in runner_workloads
    }
    return canonical_json_digest(
        {
            "projection": REVISION_PROJECTION_VERSION,
            "kind": "plugin",
            "descriptor": {**descriptor, "spec": spec},
        }
    )


def registration_manifest(descriptor: Mapping[str, Any], workload_id: str) -> dict[str, Any]:
    """Generate the complete mapping for one custom-runner workload."""

    spec = descriptor["spec"]
    steps: dict[str, dict[str, str]] = {}
    for target in spec["targets"]:
        runtime = target.get("runtime", {})
        if runtime.get("kind") == "custom-runner" and runtime.get("workload") == workload_id:
            revision = target["step_revision_id"]
            if revision in steps:
                raise ValueError("duplicate step revision in registration manifest")
            steps[revision] = {
                "stepIdentity": target["identity"],
                "entryPoint": runtime["entryPoint"],
                "abi": runtime["abi"],
            }
    if not steps:
        raise ValueError(f"workload {workload_id!r} has no custom-runner steps")
    return {
        "apiVersion": "syntara.registration/v1alpha1",
        "kind": "RegistrationManifest",
        "plugin_revision_id": spec["plugin_revision_id"],
        "sdkVersion": spec["sdkVersion"],
        "workload": workload_id,
        "steps": steps,
    }


def validate_registration_manifest(
    descriptor: Mapping[str, Any], workload_id: str, manifest: Mapping[str, Any]
) -> None:
    """Reject a malformed, incomplete, or inconsistent registration mapping."""

    from .descriptor import validate_plugin_descriptor_semantics

    bundle = load_bundle()
    errors = tuple(validate_document(bundle, "plugin_descriptor", descriptor)) + (
        validate_plugin_descriptor_semantics(descriptor, deferred_workload=workload_id)
    )
    if errors:
        raise ValueError("; ".join(errors))
    if descriptor["spec"]["contractBundleDigest"] != bundle.digest:
        raise ValueError("descriptor uses an unsupported contract bundle")
    try:
        expected = registration_manifest(descriptor, workload_id)
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("invalid descriptor for registration manifest") from error
    schema_errors = validate_document(bundle, "registration_manifest", manifest)
    if schema_errors:
        raise ValueError("; ".join(schema_errors))
    if manifest != expected:
        raise ValueError("registration manifest does not match the compiled descriptor")


def revision_errors(descriptor: Mapping[str, Any]) -> tuple[str, ...]:
    """Check every generated ID against its canonical projection."""

    try:
        spec = descriptor["spec"]
        if not isinstance(spec["sdkVersion"], str) or not spec["sdkVersion"]:
            raise ValueError("missing sdkVersion")
        errors = []
        for target in spec["targets"]:
            if target.get("kind") in STEP_KINDS:
                if target.get("step_revision_id") != step_revision_id(descriptor, target):
                    errors.append(
                        f"target {target.get('identity', '<unknown>')!r} step revision ID does not match its contract"
                    )
            elif "step_revision_id" in target:
                errors.append("non-step target has a step revision ID")
        if spec.get("plugin_revision_id") != plugin_revision_id(descriptor):
            errors.append("plugin revision ID does not match its contract")
        return tuple(errors)
    except (KeyError, TypeError, ValueError, AttributeError):
        return ("plugin descriptor has invalid revision inputs",)
