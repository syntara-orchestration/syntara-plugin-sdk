from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from syntara_plugin.sdk import (
    BuildRequest,
    CompilationError,
    Plugin,
    Target,
    compile_model,
    compile_workspace,
)


DIGEST = "quay.io/acme/issue-tools@sha256:" + "a" * 64


def _plugin(target: str = "steps/create-issue/manifest.yaml") -> dict[str, object]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Plugin",
        "metadata": {
            "namespace": "acme",
            "name": "issue-tools",
            "version": "0.1.0",
            "displayName": "Issue tools",
            "description": "Creates GitHub issues.",
        },
        "spec": {"workload": {"image": "plugin-workload"}, "targets": [target]},
    }


def _action(source: dict[str, object]) -> dict[str, object]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Action",
        "metadata": {
            "name": "create-issue",
            "displayName": "Create issue",
            "description": "Creates one issue.",
        },
        "spec": {
            "runtime": {"kind": "custom-workload"},
            "input": source,
            "output": {"kind": "inline", "value": {"type": "object"}},
            "error": {"kind": "inline", "value": {"type": "object"}},
        },
    }


def _integration() -> dict[str, object]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "IntegrationType",
        "metadata": {
            "name": "github-connection",
            "displayName": "GitHub connection",
            "description": "Connects a GitHub account or server.",
        },
        "spec": {
            "id": "acme.issue-tools.github-connection",
            "endpointKinds": ["github-cloud", "github-enterprise"],
            "credentialTypes": ["acme.issue-tools.github-token"],
            "configuration": {"kind": "inline", "value": {"type": "object"}},
        },
    }


def _credential_recipe(
    *, integration_type: str = "acme.issue-tools.github-connection"
) -> dict[str, object]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "CredentialRecipe",
        "metadata": {
            "name": "github-token",
            "displayName": "GitHub token",
            "description": "A GitHub service token.",
        },
        "spec": {
            "id": "acme.issue-tools.github-token",
            "integrationType": integration_type,
            "credentialSchema": {
                "kind": "inline",
                "value": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["token"],
                    "properties": {"token": {"type": "string"}},
                },
            },
            "secretFields": ["token"],
            "issuance": {"kind": "static-bearer.v1"},
        },
    }


def _yaml(value: object) -> str:
    import yaml

    return yaml.safe_dump(value, sort_keys=False)


def _workspace(tmp_path: Path, *, action: dict[str, object]) -> Path:
    root = tmp_path / "plugin.yaml"
    target = tmp_path / "steps/create-issue/manifest.yaml"
    target.parent.mkdir(parents=True)
    root.write_text(_yaml(_plugin()))
    target.write_text(_yaml(action))
    return root


def _compile(root: Path):
    return compile_workspace(BuildRequest(root, image_bindings={"plugin-workload": DIGEST}))


def _http_operation(*, asset_maps: bool = False) -> dict[str, object]:
    if asset_maps:
        request_map: dict[str, object] = {
            "kind": "asset",
            "path": "mappings/create-issue.request.yaml",
        }
        response_map: dict[str, object] = {
            "kind": "asset",
            "path": "mappings/create-issue.response.yaml",
        }
    else:
        request_map = {"kind": "inline", "value": {"title": {"from": "/title"}}}
        response_map = {"kind": "inline", "value": {"id": {"from": "/id"}}}
    return {
        "method": "POST",
        "path": "/repos/{input.owner}/{input.repository}/issues",
        "requestMap": request_map,
        "responseMap": response_map,
    }


def test_yaml_and_typed_authoring_compile_to_identical_canonical_output(tmp_path: Path) -> None:
    action = _action(
        {"kind": "inline", "value": {"type": "object", "properties": {"title": {"type": "string"}}}}
    )
    root = _workspace(tmp_path, action=action)

    yaml_result = _compile(root)
    typed_result = compile_model(
        Plugin(_plugin()),
        [Target("steps/create-issue/manifest.yaml", action)],
        image_bindings={"plugin-workload": DIGEST},
    )

    assert yaml_result.canonical_json() == typed_result.canonical_json()
    assert yaml_result.descriptor["spec"]["targets"][0]["runtime"] == {
        "abi": "syntara.container/v1alpha1",
        "kind": "custom-workload",
        "workload": "plugin-workload",
    }


def test_platform_runner_action_requires_and_preserves_its_versioned_driver() -> None:
    plugin = _plugin()
    plugin["spec"] = {"targets": ["steps/create-issue/manifest.yaml"]}
    action = _action({"kind": "inline", "value": {"type": "object"}})
    specification = action["spec"]
    assert isinstance(specification, dict)
    specification["runtime"] = {
        "kind": "platform-runner",
        "driver": "http.v1",
        "operation": _http_operation(),
    }

    result = compile_model(
        Plugin(plugin),
        [Target("steps/create-issue/manifest.yaml", action)],
    )

    assert result.descriptor["spec"]["targets"][0]["runtime"] == {
        "driver": "http.v1",
        "kind": "platform-runner",
        "operation": {
            "method": "POST",
            "path": "/repos/{input.owner}/{input.repository}/issues",
            "requestMap": {"title": {"from": "/title"}},
            "responseMap": {"id": {"from": "/id"}},
        },
    }
    assert result.descriptor["spec"]["workloads"] == {}


def test_compiler_canonicalizes_connection_types_and_binds_an_action_to_them() -> None:
    plugin = _plugin()
    plugin["spec"] = {
        "targets": [
            "integrations/github.yaml",
            "credentials/github-token.yaml",
            "steps/create-issue/manifest.yaml",
        ]
    }
    action = _action({"kind": "inline", "value": {"type": "object"}})
    action_spec = action["spec"]
    assert isinstance(action_spec, dict)
    action_spec["runtime"] = {
        "kind": "platform-runner",
        "driver": "http.v1",
        "operation": _http_operation(),
    }
    action_spec["integrationType"] = "acme.issue-tools.github-connection"

    result = compile_model(
        Plugin(plugin),
        [
            Target("integrations/github.yaml", _integration()),
            Target("credentials/github-token.yaml", _credential_recipe()),
            Target("steps/create-issue/manifest.yaml", action),
        ],
    )

    integration, credential, compiled_action = result.descriptor["spec"]["targets"]
    assert integration["kind"] == "IntegrationType"
    assert integration["configuration"] == {"type": "object"}
    assert credential["kind"] == "CredentialRecipe"
    assert credential["credentialSchema"]["required"] == ["token"]
    assert "output" not in credential
    assert compiled_action["integrationType"] == "acme.issue-tools.github-connection"


def test_compiler_rejects_a_connection_type_link_to_another_plugin_or_missing_type() -> None:
    plugin = _plugin()
    plugin["spec"] = {
        "targets": ["credentials/github-token.yaml", "steps/create-issue/manifest.yaml"]
    }
    action = _action({"kind": "inline", "value": {"type": "object"}})
    action_spec = action["spec"]
    assert isinstance(action_spec, dict)
    action_spec["integrationType"] = "acme.issue-tools.github-connection"

    with pytest.raises(CompilationError) as error:
        compile_model(
            Plugin(plugin),
            [
                Target("credentials/github-token.yaml", _credential_recipe()),
                Target("steps/create-issue/manifest.yaml", action),
            ],
            image_bindings={"plugin-workload": DIGEST},
        )

    assert {item.code for item in error.value.diagnostics} >= {
        "ACTION_INTEGRATION_UNDECLARED",
        "CREDENTIAL_INTEGRATION_UNDECLARED",
    }


def test_platform_runner_action_without_a_versioned_driver_is_rejected() -> None:
    plugin = _plugin()
    plugin["spec"] = {"targets": ["steps/create-issue/manifest.yaml"]}
    action = _action({"kind": "inline", "value": {"type": "object"}})
    specification = action["spec"]
    assert isinstance(specification, dict)
    specification["runtime"] = {"kind": "platform-runner"}

    with pytest.raises(CompilationError) as error:
        compile_model(
            Plugin(plugin),
            [Target("steps/create-issue/manifest.yaml", action)],
        )

    assert "SCHEMA_INVALID" in {item.code for item in error.value.diagnostics}


def test_http_operation_assets_are_typed_and_bound_to_the_nested_operation_fields(
    tmp_path: Path,
) -> None:
    plugin = _plugin()
    plugin["spec"] = {"targets": ["steps/create-issue/manifest.yaml"]}
    action = _action({"kind": "inline", "value": {"type": "object"}})
    specification = action["spec"]
    assert isinstance(specification, dict)
    specification["runtime"] = {
        "kind": "platform-runner",
        "driver": "http.v1",
        "operation": _http_operation(asset_maps=True),
    }
    root = _workspace(tmp_path, action=action)
    root.write_text(_yaml(plugin))
    mappings = root.parent / "mappings"
    mappings.mkdir()
    (mappings / "create-issue.request.yaml").write_text("title:\n  from: /title\n")
    (mappings / "create-issue.response.yaml").write_text("id:\n  from: /id\n")

    result = _compile(root)

    assert result.descriptor["spec"]["targets"][0]["runtime"]["operation"] == {
        "method": "POST",
        "path": "/repos/{input.owner}/{input.repository}/issues",
        "requestMap": {"title": {"from": "/title"}},
        "responseMap": {"id": {"from": "/id"}},
    }
    assert (
        result.asset_index["mappings/create-issue.request.yaml"]["mediaType"] == "application/yaml"
    )
    assert result.asset_index["mappings/create-issue.request.yaml"]["references"] == [
        "steps/create-issue/manifest.yaml#/spec/runtime/operation/requestMap"
    ]


def test_http_operation_rejects_an_invalid_mapping_source() -> None:
    plugin = _plugin()
    plugin["spec"] = {"targets": ["steps/create-issue/manifest.yaml"]}
    action = _action({"kind": "inline", "value": {"type": "object"}})
    specification = action["spec"]
    assert isinstance(specification, dict)
    specification["runtime"] = {
        "kind": "platform-runner",
        "driver": "http.v1",
        "operation": {
            "method": "POST",
            "path": "/repos/{input.owner}/{input.repository}/issues",
            "requestMap": {"kind": "inline", "value": {"title": {"from": "title"}}},
        },
    }

    with pytest.raises(CompilationError) as error:
        compile_model(Plugin(plugin), [Target("steps/create-issue/manifest.yaml", action)])

    assert "HTTP_MAPPING_INVALID" in {item.code for item in error.value.diagnostics}


def test_inline_and_asset_documents_preserve_identical_schema_semantics(tmp_path: Path) -> None:
    schema = {
        "type": "object",
        "description": "Inputs used to create an issue.",
        "properties": {
            "title": {
                "type": "string",
                "description": "Title shown to repository collaborators.",
            }
        },
    }
    inline_root = _workspace(
        tmp_path / "inline", action=_action({"kind": "inline", "value": schema})
    )
    asset_root = _workspace(
        tmp_path / "asset", action=_action({"kind": "asset", "path": "schemas/input.json"})
    )
    asset_path = asset_root.parent / "schemas/input.json"
    asset_path.parent.mkdir()
    asset_path.write_text(
        '{"description":"Inputs used to create an issue.",'
        '"properties":{"title":{"description":"Title shown to repository collaborators.",'
        '"type":"string"}},"type":"object"}'
    )

    inline = _compile(inline_root)
    asset = _compile(asset_root)

    assert (
        inline.descriptor["spec"]["targets"][0]["input"]
        == asset.descriptor["spec"]["targets"][0]["input"]
    )
    assert inline.asset_index == {}
    assert set(asset.asset_index) == {"schemas/input.json"}
    assert asset.asset_index["schemas/input.json"]["mediaType"] == "application/schema+json"
    assert b'"description":"Inputs used to create an issue."' in asset.assets[
        "schemas/input.json"
    ].content


def test_workspace_documentation_is_a_signed_markdown_asset(tmp_path: Path) -> None:
    root = _workspace(tmp_path, action=_action({"kind": "inline", "value": {"type": "object"}}))
    document = b"# Issue tools\n\nCreate and label GitHub issues.\n"
    documentation = root.parent / "docs/README.md"
    documentation.parent.mkdir()
    documentation.write_bytes(document)
    plugin = _plugin()
    specification = plugin["spec"]
    assert isinstance(specification, dict)
    specification["documentation"] = {"path": "docs/README.md"}
    root.write_text(_yaml(plugin))

    result = _compile(root)

    digest = f"sha256:{sha256(document).hexdigest()}"
    assert result.descriptor["spec"]["documentation"] == {
        "digest": digest,
        "path": "docs/README.md",
    }
    assert result.asset_index["docs/README.md"] == {
        "canonicalDigest": digest,
        "digest": digest,
        "mediaType": "text/markdown",
        "references": ["plugin.yaml#/spec/documentation"],
        "size": len(document),
    }


@pytest.mark.parametrize("content", [b"\x00", b"\xff"])
def test_workspace_documentation_rejects_binary_or_non_utf8_content(
    tmp_path: Path, content: bytes
) -> None:
    root = _workspace(tmp_path, action=_action({"kind": "inline", "value": {"type": "object"}}))
    documentation = root.parent / "docs/README.md"
    documentation.parent.mkdir()
    documentation.write_bytes(content)
    plugin = _plugin()
    specification = plugin["spec"]
    assert isinstance(specification, dict)
    specification["documentation"] = {"path": "docs/README.md"}
    root.write_text(_yaml(plugin))

    with pytest.raises(CompilationError) as error:
        _compile(root)

    assert "DOCUMENTATION_INVALID" in {item.code for item in error.value.diagnostics}


def test_provider_binding_materializes_the_provider_abi() -> None:
    plugin = _plugin()
    plugin["spec"]["providers"] = [{"id": "resource-provider", "image": "provider-image"}]
    result = compile_model(
        Plugin(plugin),
        [
            Target(
                "steps/create-issue/manifest.yaml",
                _action({"kind": "inline", "value": {"type": "object"}}),
            )
        ],
        image_bindings={"plugin-workload": DIGEST, "provider-image": DIGEST},
    )

    assert result.descriptor["spec"]["workloads"]["provider-image"] == {
        "abi": "syntara.provider/v1alpha1",
        "image": DIGEST,
        "provider": "resource-provider",
    }


def test_authoring_reports_stable_path_diagnostics(tmp_path: Path) -> None:
    root = _workspace(tmp_path, action=_action({"kind": "inline", "value": {"type": "object"}}))
    root.write_text(_yaml(_plugin("../outside.yaml")))

    with pytest.raises(CompilationError) as raised:
        _compile(root)

    assert "TARGET_PATH_INVALID" in {diagnostic.code for diagnostic in raised.value.diagnostics}
    assert all(diagnostic.location.file for diagnostic in raised.value.diagnostics)


def test_rejects_duplicate_yaml_keys_recursive_alias_and_mutable_image(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text("apiVersion: syntara.io/v1alpha1\napiVersion: syntara.io/v1alpha1\n")
    with pytest.raises(CompilationError) as duplicate_error:
        compile_workspace(BuildRequest(duplicate))
    assert duplicate_error.value.diagnostics[0].code == "YAML_INVALID"

    root = _workspace(
        tmp_path / "cycle", action=_action({"kind": "inline", "value": {"type": "object"}})
    )
    action_path = root.parent / "steps/create-issue/manifest.yaml"
    action_path.write_text(
        "apiVersion: syntara.io/v1alpha1\nkind: Action\nmetadata:\n  name: create-issue\n  displayName: Create issue\n  description: Creates one issue.\nspec: &cycle\n  runtime: {kind: custom-workload}\n  input: {kind: inline, value: {type: object}}\n  output: {kind: inline, value: {type: object}}\n  self: *cycle\n"
    )
    with pytest.raises(CompilationError) as cycle_error:
        _compile(root)
    assert cycle_error.value.diagnostics[0].code == "YAML_ALIAS_FORBIDDEN"

    root = _workspace(
        tmp_path / "image", action=_action({"kind": "inline", "value": {"type": "object"}})
    )
    with pytest.raises(CompilationError) as image_error:
        compile_workspace(
            BuildRequest(
                root, image_bindings={"plugin-workload": "quay.io/acme/issue-tools:latest"}
            )
        )
    assert "IMAGE_REFERENCE_MUTABLE" in {
        diagnostic.code for diagnostic in image_error.value.diagnostics
    }


def test_custom_workload_accepts_digest_pinned_registry_reference_with_port(tmp_path: Path) -> None:
    """A local OCI registry port is part of an immutable repository reference."""
    root = _workspace(
        tmp_path, action=_action({"kind": "inline", "value": {"type": "object"}})
    )
    image = "localhost:8443/syntara-admin/plugins/echo@sha256:" + "a" * 64

    result = compile_workspace(BuildRequest(root, image_bindings={"plugin-workload": image}))

    assert result.descriptor["spec"]["workloads"] == {
        "plugin-workload": {"abi": "syntara.container/v1alpha1", "image": image}
    }


def test_rejects_case_collisions_archives_and_oversized_assets(tmp_path: Path) -> None:
    first = _action({"kind": "inline", "value": {"type": "object"}})
    second = {
        **first,
        "metadata": {
            "name": "other-action",
            "displayName": "Other action",
            "description": "Other.",
        },
    }
    model = _plugin("steps/create-issue/manifest.yaml")
    model["spec"]["targets"] = [
        "steps/create-issue/manifest.yaml",
        "steps/CREATE-issue/manifest.yaml",
    ]
    with pytest.raises(CompilationError) as collision:
        compile_model(
            Plugin(model),
            [
                Target("steps/create-issue/manifest.yaml", first),
                Target("steps/CREATE-issue/manifest.yaml", second),
            ],
            image_bindings={"plugin-workload": DIGEST},
        )
    assert "PATH_CASE_COLLISION" in {item.code for item in collision.value.diagnostics}

    root = _workspace(
        tmp_path / "archive", action=_action({"kind": "asset", "path": "schemas/input.zip"})
    )
    archive = root.parent / "schemas/input.zip"
    archive.parent.mkdir()
    archive.write_bytes(b"not an archive")
    with pytest.raises(CompilationError) as archive_error:
        _compile(root)
    assert "ASSET_ARCHIVE_FORBIDDEN" in {item.code for item in archive_error.value.diagnostics}

    root = _workspace(
        tmp_path / "size", action=_action({"kind": "asset", "path": "schemas/input.json"})
    )
    asset = root.parent / "schemas/input.json"
    asset.parent.mkdir()
    asset.write_text(" " * (1024 * 1024 + 1))
    with pytest.raises(CompilationError) as size_error:
        _compile(root)
    assert "ASSET_INVALID" in {item.code for item in size_error.value.diagnostics}


@pytest.mark.parametrize("asset_path", ["./schemas/input.json", "schemas/.input.json"])
def test_rejects_non_normalized_asset_paths(tmp_path: Path, asset_path: str) -> None:
    root = _workspace(
        tmp_path,
        action=_action({"kind": "asset", "path": asset_path}),
    )
    asset = root.parent / "schemas/input.json"
    asset.parent.mkdir()
    asset.write_text('{"type":"object"}')

    with pytest.raises(CompilationError) as error:
        _compile(root)

    assert "ASSET_PATH_INVALID" in {item.code for item in error.value.diagnostics}


def test_reports_invalid_root_shapes_and_missing_root_as_diagnostics(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text(_yaml({**_plugin(), "spec": {"targets": None}}))

    with pytest.raises(CompilationError) as malformed_error:
        compile_workspace(BuildRequest(malformed))
    assert "SCHEMA_INVALID" in {item.code for item in malformed_error.value.diagnostics}

    with pytest.raises(CompilationError) as missing_error:
        compile_workspace(BuildRequest(tmp_path / "missing.yaml"))
    assert missing_error.value.diagnostics[0].code == "DOCUMENT_UNREADABLE"


def test_rejects_non_object_and_invalid_schema_documents(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path / "scalar", action=_action({"kind": "asset", "path": "schemas/input.json"})
    )
    asset = root.parent / "schemas/input.json"
    asset.parent.mkdir()
    asset.write_text("[]")

    with pytest.raises(CompilationError) as scalar_error:
        _compile(root)
    assert "SCHEMA_DOCUMENT_INVALID" in {item.code for item in scalar_error.value.diagnostics}

    root = _workspace(
        tmp_path / "invalid", action=_action({"kind": "inline", "value": {"not": "a JSON Schema"}})
    )
    with pytest.raises(CompilationError) as invalid_error:
        _compile(root)
    assert "SCHEMA_DOCUMENT_INVALID" in {item.code for item in invalid_error.value.diagnostics}


def test_rejects_yaml_anchors_and_aliases(tmp_path: Path) -> None:
    root = _workspace(tmp_path, action=_action({"kind": "inline", "value": {"type": "object"}}))
    root.write_text(
        "&root\napiVersion: syntara.io/v1alpha1\nkind: Plugin\nmetadata: {namespace: acme, name: issue-tools, version: 0.1.0, displayName: Issue tools, description: Creates GitHub issues.}\nspec: {targets: [steps/create-issue/manifest.yaml]}\n"
    )

    with pytest.raises(CompilationError) as error:
        _compile(root)
    assert error.value.diagnostics[0].code == "YAML_ALIAS_FORBIDDEN"
