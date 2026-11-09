from __future__ import annotations

import json
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

    unsupported_work_item = _work_item()
    unsupported_work_item["runtimeAbi"] = "syntara.container/v1alpha2"
    assert not harness.validate_runtime(unsupported_work_item).valid

    provider = _provider_request()
    provider["runtimeAbi"] = CONTAINER_ABI
    assert not harness.validate_provider(provider).valid

    assert not harness.validate_runtime(
        {
            "apiVersion": "syntara.io/v1alpha1",
            "kind": "ContainerCancellation",
            "invocationId": "invoke-1",
            "fenceToken": "fence-1",
            "requestedAt": "not-a-time",
        }
    ).valid

    deadline = _provider_request()
    deadline["deadline"] = "not-a-time"
    assert harness.validate_provider(deadline).valid is False


def test_both_runtime_ownership_classes_materialize_deterministically() -> None:
    platform = RuntimeBinding(RuntimeOwnership.PLATFORM_RUNNER, DIGEST)
    publisher = RuntimeBinding(RuntimeOwnership.PUBLISHER_RUNNER, DIGEST)

    assert platform.to_document() == {
        "ownership": "platform-runner",
        "imageDigest": DIGEST,
        "abi": CONTAINER_ABI,
    }
    assert publisher.to_document() == {
        "ownership": "custom-runner",
        "imageDigest": DIGEST,
        "abi": CONTAINER_ABI,
    }
    assert asdict(platform) == asdict(RuntimeBinding(RuntimeOwnership.PLATFORM_RUNNER, DIGEST))


def test_runtime_package_does_not_depend_on_sdk_implementation() -> None:
    runtime_source = Path(__file__).parents[2] / "packages/contracts/src/syntara_plugin/runtime"
    assert "syntara_plugin.sdk" not in "\n".join(
        path.read_text() for path in runtime_source.glob("*.py")
    )


def test_container_result_envelope_fixtures_conform() -> None:
    harness = ConformanceHarness()
    fixtures = Path(__file__).parents[4] / "contracts" / "fixtures"

    for fixture_name in (
        "runtime-result.valid.json",
        "runtime-result-reference.valid.json",
        "runtime-result-failure.valid.json",
    ):
        fixture = json.loads((fixtures / fixture_name).read_text(encoding="utf-8"))
        assert harness.validate_runtime(fixture).valid

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
            "errorSchemaDigest": HASH,
            "errorResult": {"category": "dependency", "code": "REMOTE_REJECTED", "message": "The upstream service rejected the request.", "retryable": False},
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

    failure = dict(
        base,
        statusCode=7,
        errorSchemaDigest=HASH,
        errorResult={"category": "dependency", "code": "REMOTE_REJECTED", "message": "The upstream service rejected the request.", "retryable": True},
    )
    assert harness.validate_runtime(failure).valid
    assert not harness.validate_runtime({key: value for key, value in failure.items() if key != "errorSchemaDigest"}).valid
    assert not harness.validate_runtime(dict(failure, errorResult={**failure["errorResult"], "category": "unknown"})).valid
    assert not harness.validate_runtime(dict(failure, errorResult={key: value for key, value in failure["errorResult"].items() if key != "retryable"})).valid


def test_standard_output_wrapper_requires_object_result_and_complete_error_metadata() -> None:
    harness = ConformanceHarness()
    success = {
        "result": {"issue": 42},
        "statusCode": 0,
        "statusMessage": "Completed",
        "errorResult": None,
    }
    assert harness.validate_runtime(success).valid
    assert not harness.validate_runtime(dict(success, result=[42])).valid
    assert not harness.validate_runtime(dict(success, result=42)).valid

    failure = {
        "result": None,
        "statusCode": 1,
        "statusMessage": "Rejected",
        "errorResult": {
            "category": "dependency",
            "code": "REMOTE_REJECTED",
            "message": "The upstream service rejected the request.",
            "retryable": True,
        },
    }
    assert harness.validate_runtime(failure).valid
    fixtures = Path(__file__).parents[4] / "contracts" / "fixtures"
    deliberate_failure = json.loads(
        (fixtures / "standard-output-wrapper.deliberate-failure.valid.json").read_text(
            encoding="utf-8"
        )
    )
    assert harness.validate_runtime(deliberate_failure).valid
    assert not harness.validate_runtime(
        dict(failure, errorResult={**failure["errorResult"], "category": "unknown"})
    ).valid
    assert not harness.validate_runtime(
        dict(
            failure,
            errorResult={
                key: value
                for key, value in failure["errorResult"].items()
                if key != "retryable"
            },
        )
    ).valid


def test_standard_output_wrapper_conformance_fixtures() -> None:
    harness = ConformanceHarness()
    fixtures = Path(__file__).parents[4] / "contracts" / "fixtures"

    for fixture_name, expected_valid in (
        ("standard-output-wrapper.success.valid.json", True),
        ("standard-output-wrapper.failure-missing-error.invalid.json", False),
        ("standard-output-wrapper.no-valid-outcome.invalid.json", False),
    ):
        document = json.loads((fixtures / fixture_name).read_text(encoding="utf-8"))
        validation = harness.validate_runtime(document)
        assert validation.valid is expected_valid, f"{fixture_name}: {validation.errors}"


def test_workload_request_rejects_legacy_file_leases() -> None:
    harness = ConformanceHarness()
    fixtures = Path(__file__).parents[4] / "contracts" / "fixtures"
    valid = json.loads((fixtures / "runtime-workload-request.valid.json").read_text())
    legacy = json.loads(
        (fixtures / "runtime-workload-request-file-lease.invalid.json").read_text()
    )
    from syntara_plugin.contracts import container_workload_request_digest

    assert harness.validate_runtime(valid).valid
    for request in (valid, legacy):
        digest = container_workload_request_digest(request)
        assert request["workItem"]["requestHash"] == digest
        assert request["policySnapshot"]["requestHash"] == digest
    assert not harness.validate_runtime(legacy).valid
    assert not harness.validate_runtime(legacy["credentialLeases"][0]).valid
    assert not harness.validate_runtime({**valid, "credentialLeases": []}).valid

    from jsonschema import Draft202012Validator
    from syntara_plugin.contracts import load_bundle

    schema = load_bundle().documents["runtime.schema.json"]
    validator = Draft202012Validator({**schema, "oneOf": [{"$ref": "#/$defs/workloadRequest"}]})
    errors = list(validator.iter_errors(legacy))
    assert len(errors) == 1
    assert len(errors[0].context) == 1
    assert errors[0].context[0].validator == "additionalProperties"
