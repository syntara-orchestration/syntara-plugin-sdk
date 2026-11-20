from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from syntara_plugin.contracts import (
    CatalogCheckpoint,
    RuntimeBindingCheckpoint,
    SchemaDocumentError,
    build_validator,
    bundle_digest,
    fixture_content_digest,
    format_checker,
    load_bundle,
    load_fixture_bundle,
    validate_authored_schema,
    validate_catalog_index,
    validate_document,
    validate_runtime_binding,
    validate_runtime_binding_index,
)
from syntara_plugin.runtime import ContractBundle as RuntimeContractBundle
from syntara_plugin.sdk import ContractBundle as SdkContractBundle
from syntara_plugin.testing import schema_without_descriptions

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
        "registration-manifest.schema.json",
        "runtime-binding-index.schema.json",
        "runtime-binding-resolved.schema.json",
        "runtime-binding-source.schema.json",
        "resolved-execution.schema.json",
        "runtime.schema.json",
        "step.schema.json",
        "trigger-driver.schema.json",
        "trigger-event-mapping.schema.json",
        "trigger.schema.json",
        "control.schema.json",
    }
    assert RuntimeContractBundle is SdkContractBundle


def test_plugin_schema_accepts_only_nonempty_named_workloads() -> None:
    plugin: dict[str, Any] = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Plugin",
        "metadata": _metadata(),
        "spec": {"documentation": {"path": "docs/README.md"}, "targets": ["step.yaml"]},
    }
    bundle = load_bundle()
    assert validate_document(bundle, "plugin", plugin) == []
    plugin["spec"]["workloads"] = {"python": {"image": "runtime-image"}}
    assert validate_document(bundle, "plugin", plugin) == []
    plugin["spec"]["workload"] = {"image": "runtime-image"}
    assert validate_document(bundle, "plugin", plugin)
    del plugin["spec"]["workload"]
    plugin["spec"]["workloads"] = {}
    assert validate_document(bundle, "plugin", plugin)


def test_load_bundle_rejects_a_non_2020_12_dialect_declaration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from syntara_plugin.contracts import bundle as bundle_module

    tampered = tmp_path / "v1alpha1"
    tampered.mkdir()
    for source in bundle_module._schema_directory().iterdir():
        if source.name.endswith(".schema.json"):
            document = json.loads(source.read_text(encoding="utf-8"))
            if source.name == "step.schema.json":
                document["$schema"] = "http://json-schema.org/draft-07/schema#"
            (tampered / source.name).write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(bundle_module, "_schema_directory", lambda: tampered)

    with pytest.raises(RuntimeError, match="step.schema.json.*draft/2020-12"):
        load_bundle()


def test_load_bundle_rejects_a_structurally_invalid_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from syntara_plugin.contracts import bundle as bundle_module

    tampered = tmp_path / "v1alpha1"
    tampered.mkdir()
    for source in bundle_module._schema_directory().iterdir():
        if source.name.endswith(".schema.json"):
            document = json.loads(source.read_text(encoding="utf-8"))
            if source.name == "step.schema.json":
                document["type"] = "invalid"
            (tampered / source.name).write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(bundle_module, "_schema_directory", lambda: tampered)

    with pytest.raises(RuntimeError, match="step.schema.json.*not a valid Draft 2020-12 schema"):
        load_bundle()


def test_asserted_format_set_is_declared_by_the_common_contract() -> None:
    bundle = load_bundle()

    assert bundle.asserted_formats == frozenset({"date-time", "uri"})
    assert bundle.asserted_formats <= frozenset(format_checker().checkers)


def test_format_checker_asserts_only_the_contract_format_set() -> None:
    checker = format_checker()
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "when": {"type": "string", "format": "date-time"},
            "where": {"type": "string", "format": "uri"},
            "who": {"type": "string", "format": "email"},
        },
    }
    validator = build_validator(schema)

    assert not validator.is_valid(
        {"when": "not-a-timestamp", "where": "https://example.test", "who": "user@example.test"}
    )
    assert not validator.is_valid(
        {"when": "2026-01-01T12:00:00Z", "where": "not-a-uri", "who": "not-an-email"}
    )
    # Format names outside the asserted set are annotations only.
    assert validator.is_valid(
        {"when": "2026-01-01T12:00:00Z", "where": "https://example.test", "who": "not-an-email"}
    )
    # Absolute URIs with a scheme are accepted, including fragments and
    # scheme-specific forms such as mailto; values urlsplit cannot parse fail
    # the check instead of raising.
    for uri in (
        "https://example.test/a#section",
        "mailto:user@example.test",
        "https://user:pass@example.test:8080/a/b?x=1&y=2#frag",
        "http://[2001:db8::1]:9090/path",
        # RFC 3986 permits any digit-only port syntactically (section 3.2.3)
        # and IPvFuture host literals (section 3.2.2); neither carries a
        # numeric range or address-family restriction at the syntax level.
        "http://example.test:99999/",
        "http://[v1.aabb]/index",
        "urn:uuid:6fa459ea-ee8a-3ca4-894e-db77e160355e",
    ):
        assert validator.is_valid(
            {"when": "2026-01-01T12:00:00Z", "where": uri, "who": "user@example.test"}
        )
    for uri in (
        "//example.test/path",  # scheme-relative reference, not an absolute URI
        "not a uri",  # space is not allowed in a URI
        "http://[::1",  # unparseable IPv6 authority
        "https://example.test/a b",  # space inside the path
        "https://exämple.test",  # non-ASCII (IRIs are a different format)
        "https://example.test/a%zz",  # malformed percent-encoding
        "https://example.test/a[b]",  # brackets are only valid in IPv6 hosts
        "https://example.test:bad",  # authority port must be numeric
        "https://user{name}@example.test",  # brace is not valid in userinfo
        "http://[v.aabb]/index",  # IPvFuture needs at least one hex digit
        "http://[v1.]/index",  # IPvFuture needs at least one char after "."
    ):
        assert not validator.is_valid(
            {"when": "2026-01-01T12:00:00Z", "where": uri, "who": "user@example.test"}
        )
    # Formats apply to string instances only; non-strings succeed per the
    # Draft 2020-12 format-assertion vocabulary.
    format_only = build_validator(
        {"$schema": "https://json-schema.org/draft/2020-12/schema", "properties": {"where": {"format": "uri"}}}
    )
    assert format_only.is_valid({"where": 42})
    assert build_validator(
        {"$schema": "https://json-schema.org/draft/2020-12/schema", "properties": {"when": {"format": "date-time"}}}
    ).is_valid({"when": 42})
    assert checker is format_checker()


def test_validate_authored_schema_requires_the_2020_12_dialect() -> None:
    dialect = "https://json-schema.org/draft/2020-12/schema"
    validate_authored_schema({"$schema": dialect, "type": "object"})

    for document, code in (
        ({"type": "object"}, "SCHEMA_DOCUMENT_DIALECT_MISSING"),
        (
            {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"},
            "SCHEMA_DOCUMENT_UNSUPPORTED_DRAFT",
        ),
        ([], "SCHEMA_DOCUMENT_NOT_OBJECT"),
    ):
        with pytest.raises(SchemaDocumentError) as error:
            validate_authored_schema(document)
        assert error.value.code == code


def test_validate_authored_schema_rejects_embedded_dialect_switches() -> None:
    # An embedded resource with its own $id and $schema would evaluate under
    # the declared dialect when referenced (draft-07 "dependencies" below),
    # silently bypassing the root-only draft check.
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://example.test/root.json",
        "type": "object",
        "properties": {"value": {"$ref": "https://example.test/legacy.json"}},
        "$defs": {
            "legacy": {
                "$id": "https://example.test/legacy.json",
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "dependencies": {"a": ["b"]},
            }
        },
    }
    with pytest.raises(SchemaDocumentError) as error:
        validate_authored_schema(document)
    assert error.value.code == "SCHEMA_EMBEDDED_DIALECT_UNSUPPORTED"
    assert "$defs/legacy" in error.value.message

    # The same embedded resource without a $schema declaration is fine: it
    # inherits the root's Draft 2020-12 semantics.
    del document["$defs"]["legacy"]["$schema"]
    validate_authored_schema(document)


def test_resolved_execution_schema_is_strict() -> None:
    document = {
        "bindings": [
            {
                "stepRevisionId": "sha256:" + "a" * 64,
                "stepIdentity": "acme.plugin.step",
                "runtimeKind": "platform-runner",
                "entryPoint": "acme_plugin.steps:CreateIssue",
                "abi": "syntara.container/v1alpha1",
                "schemaDigests": {
                    "input": "sha256:" + "a" * 64,
                    "output": "sha256:" + "b" * 64,
                },
            }
        ]
    }

    assert validate_document(load_bundle(), "resolved_execution", document) == []
    identity = document["bindings"][0].pop("stepIdentity")
    assert validate_document(load_bundle(), "resolved_execution", document)
    document["bindings"][0]["stepIdentity"] = identity
    document["bindings"][0]["unexpected"] = True
    assert validate_document(load_bundle(), "resolved_execution", document)


def test_plugin_build_settings_schema_is_descriptive_and_strict() -> None:
    bundle = load_bundle()
    schema = bundle.documents["plugin-build-settings.schema.json"]

    assert schema["properties"]["registry"]["properties"]["artifactRepository"]["description"]
    assert schema["properties"]["build"]["properties"]["artifactOutput"]["description"]
    workload_build = schema["properties"]["build"]["properties"]["workloads"][
        "additionalProperties"
    ]
    assert workload_build["properties"]["containerfile"]["description"]
    assert "workload" not in schema["properties"]["build"]["properties"]
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
            "output": {"kind": "inline", "value": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}},
        },
    )
    trigger = step_document(
        "Trigger",
        target_metadata("pull-request", "Pull request", "Receives pull requests."),
        {
            "driver": "webhook.v1",
            "runtime": {"kind": "platform-runner", "implementation": "syntara.webhook.event@1.0.0"},
            "configuration": {"kind": "inline", "value": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}},
            "input": {"kind": "inline", "value": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}},
            "output": {"kind": "asset", "path": "schemas/pull-request.output.json"},
        },
    )

    assert validate_document(bundle, "plugin", plugin) == []
    assert validate_document(bundle, "action", action) == []
    assert validate_document(bundle, "trigger", trigger) == []

    action["spec"]["runtime"]["operation"]["path"] = "/repos/%2Fissues"
    assert validate_document(bundle, "action", action)


def test_plugin_metadata_preserves_discovery_tags() -> None:
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

    plugin["metadata"] = {**_metadata(), "tags": ["automation", "github-issues"]}

    assert validate_document(bundle, "plugin", plugin) == []


@pytest.mark.parametrize(
    "tags",
    [
        ["automation", "automation"],
        [""],
        ["Automation"],
        ["a" * 65],
        ["tag"] * 33,
        [" automation"],
        ["issues/github"],
        ["under_score"],
        ["name.with.dot"],
        ["-leading"],
        ["trailing-"],
        ["double--hyphen"],
        ["automation\n"],
    ],
)
def test_plugin_metadata_rejects_malformed_tags(tags: list[str]) -> None:
    bundle = load_bundle()
    plugin: dict[str, object] = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Plugin",
        "metadata": {**_metadata(), "tags": tags},
        "spec": {
            "documentation": {"path": "docs/README.md"},
            "targets": ["actions/create-issue.yaml"],
        },
    }

    errors = validate_document(bundle, "plugin", plugin)

    assert any("tags" in error for error in errors)


def test_target_metadata_preserves_discovery_tags() -> None:
    bundle = load_bundle()
    action = step_document(
        "Action",
        {**target_metadata("create-issue", "Create issue", "Creates GitHub issues."), "tags": ["automation"]},
        {
            "runtime": {
                "kind": "platform-runner",
                "implementation": "syntara.http.request@1.0.0",
                "operation": {"method": "POST", "path": "/issues"},
            },
            "input": {"kind": "inline", "value": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}},
            "output": {"kind": "inline", "value": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}},
        },
    )

    assert validate_document(bundle, "action", action) == []


def test_custom_runner_action_requires_an_entry_point() -> None:
    bundle = load_bundle()
    action = step_document(
        "Action",
        target_metadata("create-issue", "Create issue", "Creates GitHub issues."),
        {
            "runtime": {"kind": "custom-runner"},
            "input": {"kind": "inline", "value": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}},
            "output": {"kind": "inline", "value": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}},
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
            "input": {"kind": "inline", "value": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}},
            "output": {"kind": "inline", "value": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}},
        },
    )

    assert "entryPoint" in " ".join(validate_document(bundle, "action", action))
    del action["spec"]["runtime"]["entryPoint"]
    assert validate_document(bundle, "action", action) == []


@pytest.mark.parametrize("kind", ["Action", "Control", "Trigger"])
@pytest.mark.parametrize(
    "implementation",
    [
        "acme.tools.run@1.2.3",
        "example.hello-world.hello@0.1.0",
        "acme.tools.run@1.2.3-rc.1",
        "acme.tools.run@1.2.3-alpha.1+build.01",
        "acme.tools.run@0.0.0-01a+001",
    ],
)
def test_platform_runner_accepts_registered_step_references(kind: str, implementation: str) -> None:
    source = {"kind": "inline", "value": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}}
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
    [
        "http.v1",
        "acme.tools.run",
        "acme.tools.run@1.2",
        "acme.tools.run@latest",
        "acme.tools.run@01.2.3",
        "acme.tools.run@1.02.3",
        "acme.tools.run@1.2.03",
        "acme.tools.run@1.2.3-alpha..1",
        "acme.tools.run@1.2.3-alpha.01",
        "acme.tools.run@1.2.3-",
        "acme.tools.run@1.2.3+",
        "acme.tools.run@1.2.3+build..1",
        "acme.tools.run.more@1.2.3",
        "Acme.tools.run@1.2.3",
        "acme.tools.run@1.2.3\n",
    ],
)
def test_platform_runner_rejects_malformed_implementation_references(implementation: str) -> None:
    source = {"kind": "inline", "value": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}}
    document = step_document(
        "Action",
        target_metadata("step", "Step", "A step."),
        {
            "runtime": {"kind": "platform-runner", "implementation": implementation},
            "input": source,
            "output": source,
        },
    )
    assert validate_document(load_bundle(), "action", document)


@pytest.mark.parametrize(
    ("implementation", "valid"),
    [
        ("acme.tools.run@1.2.3-rc.1+build.01", True),
        ("acme.tools.run@1.02.3", False),
        ("acme.tools.run@1.2.03", False),
        ("acme.tools.run@1.2.3-alpha..1", False),
        ("acme.tools.run@1.2.3-alpha.01", False),
    ],
)
def test_compiled_descriptor_validates_implementation_semver(
    implementation: str, valid: bool
) -> None:
    descriptor = json.loads(
        (Path(__file__).parents[4] / "contracts/fixtures/plugin-descriptor.valid.json").read_text()
    )
    target = next(
        target
        for target in descriptor["spec"]["targets"]
        if target.get("runtime", {}).get("kind") == "platform-runner"
    )
    target["runtime"]["implementation"] = implementation
    assert (validate_document(load_bundle(), "plugin_descriptor", descriptor) == []) is valid


def test_platform_runner_rejects_legacy_driver_and_custom_runner_rejects_implementation() -> None:
    source = {"kind": "inline", "value": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}}
    document = step_document(
        "Action",
        target_metadata("step", "Step", "A step."),
        {
            "runtime": {"kind": "platform-runner", "driver": "http.v1"},
            "input": source,
            "output": source,
        },
    )
    assert validate_document(load_bundle(), "action", document)


def test_http_operation_requires_the_builtin_http_runner() -> None:
    source = {"kind": "inline", "value": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}}
    document = step_document(
        "Action",
        target_metadata("step", "Step", "A step."),
        {
            "runtime": {
                "kind": "platform-runner",
                "implementation": "acme.tools.run@1.2.3",
                "operation": {"method": "GET", "path": "/items"},
            },
            "input": source,
            "output": source,
        },
    )
    assert validate_document(load_bundle(), "action", document)

    document["spec"]["runtime"]["implementation"] = "syntara.http.request@1.0.0"
    assert validate_document(load_bundle(), "action", document) == []
    document["spec"]["runtime"] = {
        "kind": "custom-runner",
        "entryPoint": "steps:Run",
        "implementation": "acme.tools.run@1.2.3",
    }
    assert validate_document(load_bundle(), "action", document)


# The shared contract treats entryPoint as an opaque, language-SDK-defined
# handler identifier: Python module:Class is only one adapter's convention.
OPAQUE_ENTRY_POINTS = [
    "steps:Run",
    "greeter",
    "cmd/server:Echo",
    "github.com/acme/records/handlers.NormalizeRecords",
    "./dist/handlers.js#normalizeRecords",
    "@acme/records:normalize",
    "steps..bad:Run",
    "x" * 256,
]

MALFORMED_ENTRY_POINTS = [
    "",
    "steps:Run\n",
    "has space",
    " leading",
    "tab\tseparated",
    "nul\x00byte",
    "caf\u00e9:Run",
    "x" * 257,
]


def _custom_runner_step(kind: str, entry_point: str) -> dict[str, Any]:
    source = {
        "kind": "inline",
        "value": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
        },
    }
    spec: dict[str, Any] = {
        "runtime": {"kind": "custom-runner", "entryPoint": entry_point},
        "input": source,
        "output": source,
    }
    if kind == "Control":
        spec["invocation"] = {"kind": "workflow-invocation", "workflow": "acme.release.deploy"}
    if kind == "Trigger":
        spec["driver"] = "webhook.v1"
        spec["configuration"] = source
    return step_document(
        kind, target_metadata("normalize-records", "Normalize records", "Normalizes."), spec
    )


@pytest.mark.parametrize("kind", ["Action", "Control", "Trigger"])
@pytest.mark.parametrize("entry_point", OPAQUE_ENTRY_POINTS)
def test_step_contracts_accept_opaque_entry_points(kind: str, entry_point: str) -> None:
    document = _custom_runner_step(kind, entry_point)

    assert validate_document(load_bundle(), kind.lower(), document) == []


@pytest.mark.parametrize("kind", ["Action", "Control", "Trigger"])
@pytest.mark.parametrize("entry_point", MALFORMED_ENTRY_POINTS)
def test_step_contracts_reject_unbounded_or_unprintable_entry_points(
    kind: str, entry_point: str
) -> None:
    document = _custom_runner_step(kind, entry_point)

    errors = validate_document(load_bundle(), kind.lower(), document)
    assert any("entryPoint" in error for error in errors)


def _resolved_execution(entry_point: str) -> dict[str, Any]:
    return {
        "bindings": [
            {
                "stepRevisionId": f"sha256:{'a' * 64}",
                "stepIdentity": "acme.records.normalize-records",
                "runtimeKind": "custom-runner",
                "entryPoint": entry_point,
                "abi": "syntara.container/v1alpha1",
                "schemaDigests": {"input": f"sha256:{'b' * 64}", "output": f"sha256:{'c' * 64}"},
            }
        ]
    }


def test_resolved_execution_carries_an_opaque_entry_point() -> None:
    bundle = load_bundle()

    assert validate_document(bundle, "resolved_execution", _resolved_execution("cmd/x#run")) == []
    assert validate_document(bundle, "resolved_execution", _resolved_execution("has space"))


def test_concrete_step_types_accept_an_optional_runtime_error_schema() -> None:
    bundle = load_bundle()
    source = {"kind": "inline", "value": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}}
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
    source = {"kind": "inline", "value": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}}
    metadata = {
        "name": "example",
        "displayName": "Example",
        "description": "One category-specific step type.",
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
                "value": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "properties": {"secret": {"type": "string"}}},
            },
            "input": {
                "kind": "inline",
                "value": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "properties": {"event": {"type": "string"}}},
            },
            "output": {
                "kind": "inline",
                "value": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "properties": {"runId": {"type": "string"}}},
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


def _runtime_binding_source() -> dict[str, Any]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "RuntimeBinding",
        "metadata": {"namespace": "acme", "name": "issue-tools-runtime"},
        "spec": {
            "plugin": {"source": "plugin.yaml"},
            "workloads": {
                "python": {
                    "default": "base",
                    "options": [
                        {
                            "name": "base",
                            "displayName": "Python base",
                            "runtime": {
                                "components": [{"name": "python", "executable": "/usr/bin/python3"}]
                            },
                            "image": {"buildTarget": "python-base"},
                        },
                        {
                            "name": "aws",
                            "displayName": "Python with AWS SDK",
                            "runtime": {
                                "components": [{"name": "python", "executable": "/usr/bin/python3"}]
                            },
                            "image": {"buildTarget": "python-aws"},
                        },
                    ],
                }
            },
        },
    }


def _runtime_binding_resolved() -> dict[str, Any]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "RuntimeBinding",
        "metadata": {"namespace": "acme", "name": "issue-tools-runtime"},
        "spec": {
            "plugin": {
                "repository": "quay.io/acme/issue-tools",
                "digest": "sha256:" + "a" * 64,
            },
            "contract": {
                "pluginRevisionId": "sha256:" + "b" * 64,
                "contractBundleDigest": load_bundle().digest,
                "stepRevisionIds": ["sha256:" + "c" * 64],
            },
            "workloads": {
                "python": {
                    "default": "base",
                    "options": [
                        {
                            "name": "base",
                            "displayName": "Python base",
                            "image": "quay.io/acme/python-base@sha256:" + "d" * 64,
                            "runtime": {
                                "language": "python",
                                "version": "3.12.8",
                                "probeStatus": "matched",
                                "components": [
                                    {
                                        "name": "python",
                                        "executable": "/usr/bin/python3",
                                        "version": "3.12.8",
                                        "probeStatus": "matched",
                                    }
                                ],
                            },
                        }
                    ],
                }
            },
        },
    }


def _runtime_binding_index() -> dict[str, Any]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "RuntimeBindingIndex",
        "metadata": {
            "pluginDigest": "sha256:" + "a" * 64,
            "generation": 1,
            "issuedAt": "2026-11-03T12:00:00Z",
            "expiresAt": "2026-11-04T12:00:00Z",
        },
        "spec": {
            "contractVersion": "syntara.io/v1alpha1",
            "entries": [
                {
                    "bindingRepository": "quay.io/acme/issue-tools/runtime",
                    "bindingDigest": "sha256:" + "e" * 64,
                    "release": "security-refresh-1",
                    "status": "available",
                }
            ],
        },
    }


def test_runtime_binding_contracts_accept_source_resolved_and_index_documents() -> None:
    bundle = load_bundle()
    assert validate_runtime_binding(bundle, _runtime_binding_source(), resolved=False) == ()
    resolved = _runtime_binding_resolved()
    assert validate_runtime_binding(bundle, resolved, resolved=True) == ()
    assert (
        validate_runtime_binding_index(
            bundle,
            _runtime_binding_index(),
            plugin_digest="sha256:" + "a" * 64,
            now=datetime.fromisoformat("2026-11-03T12:30:00+00:00"),
        )
        == ()
    )


def test_runtime_binding_component_version_is_present_only_for_matched_probe() -> None:
    bundle = load_bundle()
    matched = _runtime_binding_resolved()
    component = matched["spec"]["workloads"]["python"]["options"][0]["runtime"]["components"][0]
    component["version"] = ""
    assert list(bundle.validator("runtime_binding_resolved").iter_errors(matched))

    unsupported = _runtime_binding_resolved()
    component = unsupported["spec"]["workloads"]["python"]["options"][0]["runtime"]["components"][0]
    component["probeStatus"] = "unsupported"
    assert list(bundle.validator("runtime_binding_resolved").iter_errors(unsupported))


def test_runtime_binding_rejects_duplicate_option_names_and_unknown_defaults() -> None:
    source = _runtime_binding_source()
    options = source["spec"]["workloads"]["python"]["options"]
    options[1]["name"] = "base"
    source["spec"]["workloads"]["python"]["default"] = "missing"

    diagnostics = validate_runtime_binding(load_bundle(), source, resolved=False)

    assert {item.code for item in diagnostics} == {
        "RUNTIME_BINDING_OPTION_DUPLICATE",
        "RUNTIME_BINDING_DEFAULT_MISSING",
    }


def test_resolved_runtime_binding_must_match_plugin_contract_and_workloads() -> None:
    resolved = _runtime_binding_resolved()
    descriptor = {
        "spec": {
            "plugin_revision_id": "sha256:" + "f" * 64,
            "contractBundleDigest": "sha256:" + "1" * 64,
            "targets": [
                {
                    "kind": "Action",
                    "step_revision_id": "sha256:" + "2" * 64,
                    "runtime": {
                        "kind": "custom-runner",
                        "workload": "bash",
                        "entryPoint": "plugin:Step",
                    },
                }
            ],
        }
    }

    diagnostics = validate_runtime_binding(
        load_bundle(), resolved, resolved=True, plugin_descriptor=descriptor
    )

    assert {
        "RUNTIME_BINDING_PLUGIN_REVISION_MISMATCH",
        "RUNTIME_BINDING_CONTRACT_BUNDLE_MISMATCH",
        "RUNTIME_BINDING_STEP_SET_MISMATCH",
        "RUNTIME_BINDING_WORKLOAD_SET_MISMATCH",
    } <= {item.code for item in diagnostics}


def test_resolved_runtime_binding_requires_annotation_and_probe_to_agree() -> None:
    resolved = _runtime_binding_resolved()
    runtime = resolved["spec"]["workloads"]["python"]["options"][0]["runtime"]
    runtime["version"] = "3.12.9"

    diagnostics = validate_runtime_binding(load_bundle(), resolved, resolved=True)

    assert "RUNTIME_BINDING_RUNTIME_VERSION_MISMATCH" in {item.code for item in diagnostics}


def test_runtime_binding_index_rejects_replay_and_duplicate_binding_digest() -> None:
    index = _runtime_binding_index()
    index["metadata"]["generation"] = 2
    index["metadata"]["previousIndexDigest"] = "sha256:" + "f" * 64
    index["spec"]["entries"].append(dict(index["spec"]["entries"][0]))

    diagnostics = validate_runtime_binding_index(
        load_bundle(),
        index,
        plugin_digest="sha256:" + "a" * 64,
        now=datetime.fromisoformat("2026-11-03T12:30:00+00:00"),
        checkpoint=RuntimeBindingCheckpoint(generation=1, index_digest="sha256:" + "0" * 64),
    )

    assert {item.code for item in diagnostics} == {
        "RUNTIME_BINDING_INDEX_PREDECESSOR_MISMATCH",
        "RUNTIME_BINDING_INDEX_DUPLICATE_ENTRY",
    }


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


def _subworkflow_fixture(name: str) -> dict[str, object]:
    path = Path(__file__).parents[4] / "contracts" / "fixtures" / name
    document: dict[str, object] = json.loads(path.read_text())
    return document


def test_subworkflow_composition_maps_onto_existing_control_and_trigger_contracts() -> None:
    bundle = load_bundle()
    parent = _subworkflow_fixture("subworkflow-call.valid.json")
    child = _subworkflow_fixture("subworkflow-trigger.valid.json")

    # Parent-side subworkflow_call is a Control that invokes a named workflow
    # through the syntara.workflow.invoke platform runner. Child-side
    # subworkflow_trigger is a Trigger. Neither needs a new step type category or
    # schema.
    assert validate_document(bundle, "control", parent) == []
    assert validate_document(bundle, "step", parent) == []
    assert validate_document(bundle, "trigger", child) == []
    assert validate_document(bundle, "step", child) == []

    parent_spec = parent["spec"]
    assert isinstance(parent_spec, dict)
    child_spec = child["spec"]
    assert isinstance(child_spec, dict)

    # The parent's declared input is the child's declared event payload. The
    # contracts express both sides but do not themselves bind one to the other;
    # agreement is an authoring obligation, not a validated constraint. Compare
    # the complete schemas, not just property names: descriptions differ by
    # perspective, types, formats, and required must not.
    assert schema_without_descriptions(parent_spec["input"]["value"]) == (
        schema_without_descriptions(child_spec["input"]["value"])
    )
    assert parent_spec["invocation"] == {
        "kind": "workflow-invocation",
        "workflow": "acme.release.publish-artifact",
    }
    assert parent_spec["runtime"] == {
        "kind": "platform-runner",
        "implementation": "syntara.workflow.invoke@1.0.0",
    }


def test_subworkflow_input_comparison_detects_schema_drift() -> None:
    """The deep input comparison must fail when a field type or constraint diverges.

    Guards the guard: if the check above ever degrades back to comparing
    property names only, this mutation — the child's ``channel`` becoming an
    optional integer — would slip through.
    """

    parent = _subworkflow_fixture("subworkflow-call.valid.json")
    drifted = _subworkflow_fixture("subworkflow-trigger.valid.json")
    channel = drifted["spec"]["input"]["value"]["properties"]["channel"]
    channel["type"] = "integer"
    drifted["spec"]["input"]["value"]["required"].remove("channel")
    assert schema_without_descriptions(parent["spec"]["input"]["value"]) != (
        schema_without_descriptions(drifted["spec"]["input"]["value"])
    )


def test_subworkflow_composition_gaps_are_rejected_by_current_contracts() -> None:
    bundle = load_bundle()

    # AAP-95476 gap: invocation.workflow is a version-less qualified identifier,
    # so a parent cannot pin a published child version in the contract.
    pinned = _subworkflow_fixture("subworkflow-call.invalid.json")
    pinned_errors = validate_document(bundle, "control", pinned)
    assert [error.split(":")[0] for error in pinned_errors] == ["spec/invocation/workflow"]

    # AAP-95476 gap: TriggerDriver.spec.transport has no value for a driver
    # fired by a parent workflow rather than by an external transport.
    driver = _subworkflow_fixture("subworkflow-trigger-driver.invalid.json")
    driver_errors = validate_document(bundle, "trigger_driver", driver)
    assert [error.split(":")[0] for error in driver_errors] == ["spec/transport"]

    # AAP-95476 gap: InstalledIdentity.spec.target.kind excludes control, so an
    # installed subworkflow_call Control has no installed-identity form.
    identity = _subworkflow_fixture("subworkflow-call-identity.invalid.json")
    identity_errors = validate_document(bundle, "installed_identity", identity)
    assert [error.split(":")[0] for error in identity_errors] == ["spec/target/kind"]
