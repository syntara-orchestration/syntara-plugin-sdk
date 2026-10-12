"""Tests for the capability-free ``http.v1`` mapper."""

from __future__ import annotations

import pytest
from syntara_plugin.runtime import (
    HttpOperationError,
    HttpOperationMapper,
    HttpRunnerContextError,
    build_http_request,
    load_http_runner_context,
    map_http_response,
)


def _github_create_issue_operation() -> dict[str, object]:
    return {
        "method": "POST",
        "path": "/repos/{input.owner}/{input.repository}/issues",
        "requestMap": {
            "title": {"from": "/title"},
            "body": {"from": "/body", "optional": True},
        },
        "responseMap": {
            "id": {"from": "/id"},
            "number": {"from": "/number"},
            "url": {"from": "/html_url"},
        },
    }


def test_build_request_renders_one_safe_path_and_omits_missing_optional_body() -> None:
    request = build_http_request(
        _github_create_issue_operation(),
        {"owner": "red hat", "repository": "syntara", "title": "A new issue"},
    )

    assert request.method == "POST"
    assert request.relative_path == "/repos/red%20hat/syntara/issues"
    assert request.json_body == {"title": "A new issue"}


def test_build_request_uses_nested_json_pointer_and_literal_values() -> None:
    request = build_http_request(
        {
            "method": "PATCH",
            "path": "/projects/{input.project}",
            "requestMap": {
                "title": {"from": "/details/title"},
                "visible": {"literal": True},
            },
        },
        {"project": "hello", "details": {"title": "Visible project"}},
    )

    assert request.json_body == {"title": "Visible project", "visible": True}


@pytest.mark.parametrize(
    "unsafe_value", ["", ".", "..", "owner/repository", "owner%2Frepository", "x?y", "x#y"]
)
def test_build_request_rejects_ambiguous_path_input(unsafe_value: str) -> None:
    with pytest.raises(HttpOperationError, match="safe non-empty string"):
        build_http_request(
            _github_create_issue_operation(),
            {"owner": unsafe_value, "repository": "syntara", "title": "A new issue"},
        )


@pytest.mark.parametrize(
    "path",
    [
        "//attacker.example.test/issues",
        "https://attacker.example.test/issues",
        "/repos/../issues",
        "/repos?q=x",
        "/repos/%2Fissues",
    ],
)
def test_build_request_rejects_an_operation_that_can_escape_the_connection_origin(
    path: str,
) -> None:
    operation = _github_create_issue_operation()
    operation["path"] = path

    with pytest.raises(HttpOperationError, match="origin-less relative path"):
        build_http_request(
            operation, {"owner": "syntara", "repository": "platform", "title": "A new issue"}
        )


def test_build_request_rejects_missing_required_mapping_input_without_echoing_value() -> None:
    with pytest.raises(HttpOperationError, match="missing required input for 'title'"):
        build_http_request(
            _github_create_issue_operation(),
            {"owner": "syntara", "repository": "platform", "body": "not a title"},
        )


def test_map_response_returns_only_declared_fields() -> None:
    result = map_http_response(
        _github_create_issue_operation(),
        {
            "id": 42,
            "number": 7,
            "html_url": "https://github.example.test/syntara/platform/issues/7",
            "token": "must-not-be-copied",
        },
    )

    assert result == {
        "id": 42,
        "number": 7,
        "url": "https://github.example.test/syntara/platform/issues/7",
    }


def test_mapper_revalidates_operation_contract_before_using_it() -> None:
    mapper = HttpOperationMapper()
    operation = _github_create_issue_operation()
    operation["requestMap"] = {"title": {"from": "title"}}

    with pytest.raises(HttpOperationError, match="does not satisfy the mapping contract"):
        mapper.build_request(
            operation, {"owner": "syntara", "repository": "platform", "title": "A new issue"}
        )


def test_http_runner_context_binds_the_resolved_operation_and_output_schema() -> None:
    context = load_http_runner_context(
        {
            "driver": "http.v1",
            "operation": _github_create_issue_operation(),
            "outputSchemaDigest": "sha256:" + "a" * 64,
        }
    )

    assert context.operation == _github_create_issue_operation()
    assert context.output_schema_digest == "sha256:" + "a" * 64


def test_http_runner_context_rejects_an_authoring_asset_reference_or_secret_field() -> None:
    with pytest.raises(HttpRunnerContextError, match="does not satisfy"):
        load_http_runner_context(
            {
                "driver": "http.v1",
                "operation": {
                    "method": "POST",
                    "path": "/issues",
                    "requestMap": {"kind": "asset", "path": "mappings/create-issue.request.yaml"},
                },
                "outputSchemaDigest": "sha256:" + "a" * 64,
                "authorization": "must-not-cross-the-ABI",
            }
        )
