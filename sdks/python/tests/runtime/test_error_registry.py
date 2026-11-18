"""The deliberate failure vocabulary is shared and closed."""

import pytest
from jsonschema import Draft202012Validator
from syntara_plugin.runtime import ErrorResult, error_result_base_schema, error_result_schema


@pytest.mark.parametrize("category,code", [
    ("validation", "INVALID_INPUT"), ("validation", "VALIDATION_FAILED"),
    ("validation", "MISCONFIGURATION"), ("validation", "UNSUPPORTED"),
    ("authentication", "AUTHENTICATION_FAILED"), ("authorization", "AUTHORIZATION_FAILED"),
    ("business", "BUSINESS_RULE_FAILED"), ("dependency", "DEPENDENCY_FAILED"),
    ("dependency", "RATE_LIMITED"), ("dependency", "RESOURCE_EXHAUSTED"),
    ("dependency", "NOT_FOUND"), ("timeout", "TIMEOUT_EXCEEDED"),
    ("cancelled", "CANCELLED"), ("policy", "POLICY_DENIED"),
    ("contract", "CONTRACT_VIOLATION"), ("internal", "INTERNAL_ERROR"),
    ("unknown", "UNKNOWN"),
])
def test_registry_agrees_across_model_and_detached_schemas(category: str, code: str) -> None:
    document = {"category": category, "code": code, "message": "Safe", "retryable": True}
    assert ErrorResult(**document).model_dump(mode="json") == document
    assert Draft202012Validator(error_result_schema()).is_valid(document)
    assert Draft202012Validator(error_result_base_schema()).is_valid({**document, "extra": 1})


@pytest.mark.parametrize("category,code", [("business", "NOT_FOUND"), ("dependency", "CUSTOM"), ("other", "UNKNOWN")])
def test_registry_rejects_unknown_or_mismatched_values(category: str, code: str) -> None:
    document = {"category": category, "code": code, "message": "Safe", "retryable": False}
    with pytest.raises(ValueError):
        ErrorResult(**document)
    assert not Draft202012Validator(error_result_base_schema()).is_valid(document)


def test_public_schema_mutation_does_not_change_cached_validation() -> None:
    schema = error_result_base_schema()
    schema.clear()
    with pytest.raises(ValueError):
        ErrorResult(category="business", code="INVALID_INPUT", message="bad")
    assert ErrorResult(category="business", code="BUSINESS_RULE_FAILED", message="bad").code == "BUSINESS_RULE_FAILED"
