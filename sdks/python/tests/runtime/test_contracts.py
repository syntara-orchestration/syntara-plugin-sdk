from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from syntara_plugin.runtime import (
    CONTAINER_ABI,
    PROVIDER_ABI,
    ConformanceHarness,
    RuntimeBinding,
    RuntimeOwnership,
)

DIGEST = "quay.io/acme/runtime@sha256:" + "a" * 64
HASH = "sha256:" + "b" * 64


def _work_item() -> dict[str, object]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "ContainerStepWorkItem",
        "invocationId": "invoke-1",
        "workflowRunId": "workflow-1",
        "stepRevisionId": "step-1",
        "pluginDigest": HASH,
        "runtimeAbi": CONTAINER_ABI,
        "imageDigest": DIGEST,
        "integrationHandle": "integration-1",
        "inputs": {"title": "Example"},
        "runtimeContext": {
            "driver": "http.v1",
            "operation": {"method": "POST", "path": "/issues"},
            "outputSchemaDigest": HASH,
        },
        "credentialHandles": ["credential-1"],
        "capabilityGrantId": "grant-1",
        "executionPolicyId": "policy-1",
        "timeoutSeconds": 300,
        "requestHash": HASH,
    }


def _provider_request() -> dict[str, object]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "FormProviderRequest",
        "invocationId": "form-1",
        "providerRevisionId": "provider-1",
        "imageDigest": DIGEST,
        "runtimeAbi": PROVIDER_ABI,
        "connectionHandle": "connection-1",
        "fieldPath": "/repository",
        "dependencies": {"organization": "acme"},
        "deadline": "2026-10-06T12:00:00Z",
        "capabilityHandle": "capability-1",
    }


def test_runtime_and_provider_positive_fixtures_conform() -> None:
    harness = ConformanceHarness()
    assert harness.validate_runtime(_work_item()).valid
    assert (
        harness.validate_runtime(
            {
                "driver": "http.v1",
                "operation": {"method": "POST", "path": "/issues"},
                "outputSchemaDigest": HASH,
            }
        ).valid
        is False
    )
    assert harness.validate_runtime(
        {
            "apiVersion": "syntara.io/v1alpha1",
            "kind": "CredentialLease",
            "leaseId": "lease-1",
            "credentialHandle": "credential-1",
            "audience": "execution-plane",
            "expiresAt": "2026-10-06T12:00:00Z",
            "secretFiles": [{"name": "token", "path": "/var/run/syntara/secrets/token"}],
        }
    ).valid
    assert harness.validate_provider(_provider_request()).valid
    assert harness.validate_provider(
        {
            "apiVersion": "syntara.io/v1alpha1",
            "kind": "FormProviderResult",
            "invocationId": "form-1",
            "options": [{"label": "Acme", "value": "acme"}],
        }
    ).valid


def test_malicious_runtime_and_provider_fixtures_are_rejected() -> None:
    harness = ConformanceHarness()
    item = _work_item()
    item["imageDigest"] = "quay.io/acme/runtime:latest"
    assert not harness.validate_runtime(item).valid

    item = _work_item()
    item["runtimeContext"] = ["not-an-object"]
    assert not harness.validate_runtime(item).valid

    lease = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "CredentialLease",
        "leaseId": "lease-1",
        "credentialHandle": "credential-1",
        "audience": "execution-plane",
        "expiresAt": "2026-10-06T12:00:00Z",
        "secretFiles": [],
        "token": "plaintext-secret",
    }
    assert not harness.validate_runtime(lease).valid

    provider = _provider_request()
    provider["runtimeAbi"] = CONTAINER_ABI
    assert not harness.validate_provider(provider).valid

    expired = _work_item()
    expired["timeoutSeconds"] = 10
    assert (
        harness.validate_runtime(
            {
                "apiVersion": "syntara.io/v1alpha1",
                "kind": "CredentialLease",
                "leaseId": "lease-1",
                "credentialHandle": "credential-1",
                "audience": "execution-plane",
                "expiresAt": "not-a-time",
                "secretFiles": [],
            }
        ).valid
        is False
    )

    deadline = _provider_request()
    deadline["deadline"] = "not-a-time"
    assert harness.validate_provider(deadline).valid is False


def test_both_runtime_ownership_classes_materialize_deterministically() -> None:
    platform = RuntimeBinding(RuntimeOwnership.PLATFORM_RUNNER, DIGEST)
    publisher = RuntimeBinding(RuntimeOwnership.PUBLISHER_WORKLOAD, DIGEST)

    assert platform.to_document() == {
        "ownership": "platform-runner",
        "imageDigest": DIGEST,
        "abi": CONTAINER_ABI,
    }
    assert publisher.to_document() == {
        "ownership": "custom-workload",
        "imageDigest": DIGEST,
        "abi": CONTAINER_ABI,
    }
    assert asdict(platform) == asdict(RuntimeBinding(RuntimeOwnership.PLATFORM_RUNNER, DIGEST))


def test_runtime_package_does_not_depend_on_sdk_implementation() -> None:
    runtime_source = Path(__file__).parents[2] / "packages/runtime/src/syntara_plugin/runtime"
    assert "syntara_plugin.sdk" not in "\n".join(
        path.read_text() for path in runtime_source.glob("*.py")
    )


def test_container_result_envelope_fixtures_conform() -> None:
    harness = ConformanceHarness()

    assert harness.validate_runtime(
        {
            "apiVersion": "syntara.io/v1alpha1",
            "kind": "ContainerResult",
            "invocationId": "invoke-1",
            "outputSchemaDigest": HASH,
            "statusCode": 0,
            "statusMessage": "Completed",
            "output": {"issue": 42},
        }
    ).valid
    assert harness.validate_runtime(
        {
            "apiVersion": "syntara.io/v1alpha1",
            "kind": "ContainerResult",
            "invocationId": "invoke-2",
            "outputSchemaDigest": HASH,
            "statusCode": 0,
            "objectReference": "object-store:large-result-1",
        }
    ).valid
    assert harness.validate_runtime(
        {
            "apiVersion": "syntara.io/v1alpha1",
            "kind": "ContainerResult",
            "invocationId": "invoke-3",
            "outputSchemaDigest": HASH,
            "statusCode": 1,
            "statusMessage": "The upstream service rejected the request.",
            "errorCode": "REMOTE_REJECTED",
            "errorMessage": "The upstream service rejected the request.",
            "retryable": False,
        }
    ).valid
    # A legacy result that predates the status fields decodes as a success.
    assert harness.validate_runtime(
        {
            "apiVersion": "syntara.io/v1alpha1",
            "kind": "ContainerResult",
            "invocationId": "invoke-legacy",
            "outputSchemaDigest": HASH,
            "output": {"issue": 42},
        }
    ).valid


def test_container_result_envelope_rejects_invalid_payload_shapes() -> None:
    harness = ConformanceHarness()
    base = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "ContainerResult",
        "invocationId": "invoke-1",
        "outputSchemaDigest": HASH,
        "statusCode": 0,
        "statusMessage": "Completed",
    }

    assert not harness.validate_runtime(dict(base)).valid

    assert not harness.validate_runtime(
        dict(base, output={"issue": 42}, objectReference="object-store:result-1")
    ).valid

    assert not harness.validate_runtime(dict(base, statusCode=-1, output={"issue": 42})).valid

    assert not harness.validate_runtime(
        {
            "apiVersion": "syntara.io/v1alpha1",
            "kind": "ContainerResult",
            "invocationId": "invoke-4",
            "outputSchemaDigest": HASH,
            "statusCode": 1,
            "errorCode": "not-a-safe-code",
            "errorMessage": "boom",
        }
    ).valid

    assert not harness.validate_runtime(
        dict(base, output={"issue": 42}, errorCode="REMOTE_REJECTED")
    ).valid

    assert not harness.validate_runtime(dict(base, output={"issue": 42}, retryable=True)).valid

    assert not harness.validate_runtime(
        dict(base, statusCode=4_294_967_296, output={"issue": 42})
    ).valid

    # A nonzero status must carry error metadata in both representations.
    assert not harness.validate_runtime(dict(base, statusCode=7)).valid
    assert not harness.validate_runtime(
        dict(base, statusCode=7, errorCode="REMOTE_REJECTED")
    ).valid
