"""Tests for root plugin manifests and explicit multi-step discovery."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import pytest
import yaml
from jsonschema import Draft7Validator  # type: ignore[import-untyped]
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource
from syntara_tools.compiler import (
    PluginDiscoveryError,
    canonical_step_identity,
    discover_plugin,
    validate_manifest,
    validate_plugin_manifest,
)


def test_plugin_discovery_apis_are_exported_from_root_facade() -> None:
    from syntara_tools import (
        PluginDescriptor,
        PluginDiscoveryError,
        StepDescriptor,
        canonical_step_identity,
        discover_plugin,
        load_plugin_manifest,
        validate_plugin_manifest,
    )

    assert callable(canonical_step_identity)
    assert callable(discover_plugin)
    assert callable(load_plugin_manifest)
    assert callable(validate_plugin_manifest)
    assert PluginDescriptor.__name__ == "PluginDescriptor"
    assert StepDescriptor.__name__ == "StepDescriptor"
    assert issubclass(PluginDiscoveryError, ValueError)


def _step(name: str, category: str = "action") -> dict[str, Any]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "StepType",
        "metadata": {
            "name": name,
            "displayName": name.replace("_", " ").title(),
            "description": "A test step.",
        },
        "spec": {
            "category": category,
            "execution": {"image": None, "entrypoint": None},
            "inputs": {"type": "object", "properties": {}, "required": []},
            "outputs": {},
        },
    }


def _plugin(targets: list[str], namespace: str = "example", name: str = "sample") -> dict[str, Any]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Plugin",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "displayName": "Sample Plugin",
            "version": "0.1.0",
            "description": "A test plugin.",
            "authors": [{"name": "Example Organization", "email": "plugins@example.com"}],
        },
        "spec": {"targets": targets},
    }


def _write_yaml(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _write_plugin(tmp_path: Path, targets: list[str]) -> Path:
    plugin_path = tmp_path / "plugin.yaml"
    _write_yaml(plugin_path, _plugin(targets))
    return plugin_path


def test_discovers_ordered_multistep_plugin_and_canonical_identity(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "steps" / "first" / "manifest.yaml", _step("first", category="action"))
    _write_yaml(tmp_path / "steps" / "second" / "manifest.yaml", _step("second", category="trigger"))
    plugin_path = _write_plugin(
        tmp_path, ["./steps/second/manifest.yaml", "./steps/first/manifest.yaml"]
    )

    descriptor = discover_plugin(plugin_path)

    assert descriptor.manifest["metadata"]["version"] == "0.1.0"
    assert all("version" not in step.manifest["metadata"] for step in descriptor.steps)
    assert [step.manifest["metadata"]["name"] for step in descriptor.steps] == ["second", "first"]
    assert [step.identity for step in descriptor.steps] == [
        "example/sample/second",
        "example/sample/first",
    ]
    assert canonical_step_identity("example", "sample", "first") == "example/sample/first"


def test_same_step_name_is_allowed_in_different_plugins(tmp_path: Path) -> None:
    for plugin_name in ("one", "two"):
        root = tmp_path / plugin_name
        _write_yaml(root / "steps" / "shared" / "manifest.yaml", _step("shared"))
        _write_yaml(root / "plugin.yaml", _plugin(["steps/shared/manifest.yaml"], name=plugin_name))

    assert discover_plugin(tmp_path / "one" / "plugin.yaml").steps[0].identity == "example/one/shared"
    assert discover_plugin(tmp_path / "two" / "plugin.yaml").steps[0].identity == "example/two/shared"


def test_duplicate_step_names_are_rejected_with_target(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "steps" / "one" / "manifest.yaml", _step("shared"))
    _write_yaml(tmp_path / "steps" / "two" / "manifest.yaml", _step("shared"))
    plugin_path = _write_plugin(
        tmp_path, ["steps/one/manifest.yaml", "steps/two/manifest.yaml"]
    )

    with pytest.raises(PluginDiscoveryError, match="steps/two/manifest.yaml.*duplicate"):
        discover_plugin(plugin_path)


@pytest.mark.parametrize("category", ["action", "task", "workflow", "trigger"])
def test_all_categories_are_valid(category: str) -> None:
    assert validate_manifest(_step("example_step", category=category)) == []


@pytest.mark.parametrize("category", [None, "invalid", ["action"]])
def test_category_must_be_a_valid_scalar(category: object) -> None:
    step = _step("example_step")
    if category is None:
        del step["spec"]["category"]
    else:
        step["spec"]["category"] = category
    assert any("category" in error for error in validate_manifest(step))


def test_step_authors_are_optional_and_step_version_is_rejected() -> None:
    step = _step("example_step")
    assert validate_manifest(step) == []
    step["metadata"]["version"] = "1.0.0"
    assert any("version" in error for error in validate_manifest(step))


def test_step_namespace_is_derived_from_plugin_and_rejected_in_step_manifest() -> None:
    step = _step("example_step")
    step["metadata"]["namespace"] = "example"
    assert any("namespace" in error for error in validate_manifest(step))


def test_plugin_authors_are_required_and_non_empty() -> None:
    plugin = _plugin(["steps/example/manifest.yaml"])
    del plugin["metadata"]["authors"]
    assert any("authors" in error for error in validate_plugin_manifest(plugin))


@pytest.mark.parametrize(
    "target",
    [
        "/tmp/manifest.yaml",
        "https://example.com/manifest.yaml",
        "C:\\plugins\\manifest.yaml",
        "\\\\server\\share\\manifest.yaml",
        "../outside/manifest.yaml",
        "..\\outside\\manifest.yaml",
    ],
)
def test_plugin_schema_validation_rejects_nonlocal_targets(target: str) -> None:
    errors = validate_plugin_manifest(_plugin([target]))
    assert any("spec/targets/0" in error for error in errors)
    plugin = _plugin(["steps/example/manifest.yaml"])
    plugin["metadata"]["authors"] = []
    assert any("authors" in error for error in validate_plugin_manifest(plugin))


@pytest.mark.parametrize("targets", [None, [], ["steps/example/manifest.yaml", "steps/example/manifest.yaml"]])
def test_targets_must_be_present_nonempty_and_unique(targets: list[str] | None) -> None:
    plugin = _plugin(["steps/example/manifest.yaml"])
    if targets is None:
        del plugin["spec"]["targets"]
    else:
        plugin["spec"]["targets"] = targets
    assert any("targets" in error for error in validate_plugin_manifest(plugin))


@pytest.mark.parametrize(
    "target",
    ["/tmp/manifest.yaml", "https://example.com/manifest.yaml", "../outside/manifest.yaml"],
)
def test_invalid_target_paths_are_rejected(tmp_path: Path, target: str) -> None:
    plugin_path = _write_plugin(tmp_path, [target])
    with pytest.raises(PluginDiscoveryError, match="targets|target"):
        discover_plugin(plugin_path)


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-plugin-test"
    outside.mkdir(exist_ok=True)
    _write_yaml(outside / "manifest.yaml", _step("escaped"))
    link = tmp_path / "steps" / "link"
    link.parent.mkdir()
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unsupported: {exc}")
    plugin_path = _write_plugin(tmp_path, ["steps/link/manifest.yaml"])
    with pytest.raises(PluginDiscoveryError, match="outside the plugin root"):
        discover_plugin(plugin_path)


@pytest.mark.parametrize("document", ["not: [valid", ["a", "list"]])
def test_invalid_or_nonmapping_target_yaml_is_rejected(tmp_path: Path, document: object) -> None:
    target = tmp_path / "steps" / "broken" / "manifest.yaml"
    target.parent.mkdir(parents=True)
    if isinstance(document, str):
        target.write_text(document, encoding="utf-8")
    else:
        _write_yaml(target, document)
    plugin_path = _write_plugin(tmp_path, ["steps/broken/manifest.yaml"])
    with pytest.raises(PluginDiscoveryError, match="steps/broken/manifest.yaml"):
        discover_plugin(plugin_path)


def test_missing_directory_wrong_kind_and_invalid_child_are_rejected(tmp_path: Path) -> None:
    plugin_path = _write_plugin(tmp_path, ["steps/missing/manifest.yaml"])
    with pytest.raises(PluginDiscoveryError, match="does not exist"):
        discover_plugin(plugin_path)

    directory = tmp_path / "steps" / "directory.yaml"
    directory.mkdir(parents=True)
    _write_yaml(plugin_path, _plugin(["steps/directory.yaml"]))
    with pytest.raises(PluginDiscoveryError, match="directory"):
        discover_plugin(plugin_path)

    _write_yaml(tmp_path / "steps" / "wrong" / "manifest.yaml", {"kind": "Plugin"})
    _write_yaml(plugin_path, _plugin(["steps/wrong/manifest.yaml"]))
    with pytest.raises(PluginDiscoveryError, match="kind must be StepType"):
        discover_plugin(plugin_path)

    invalid = _step("invalid")
    del invalid["spec"]["inputs"]
    _write_yaml(tmp_path / "steps" / "invalid" / "manifest.yaml", invalid)
    _write_yaml(plugin_path, _plugin(["steps/invalid/manifest.yaml"]))
    with pytest.raises(PluginDiscoveryError, match="step validation failed"):
        discover_plugin(plugin_path)

def test_explicit_targets_do_not_discover_unlisted_steps(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "steps" / "listed" / "manifest.yaml", _step("listed"))
    _write_yaml(tmp_path / "steps" / "unlisted" / "manifest.yaml", _step("unlisted"))
    descriptor = discover_plugin(_write_plugin(tmp_path, ["steps/listed/manifest.yaml"]))
    assert [step.manifest["metadata"]["name"] for step in descriptor.steps] == ["listed"]


def test_representative_multistep_fixture_is_valid() -> None:
    fixture = Path(__file__).parent / "fixtures" / "plugins" / "terraform_enterprise" / "plugin.yaml"
    descriptor = discover_plugin(fixture)
    assert [step.identity for step in descriptor.steps] == [
        "terraform/terraform_enterprise/create_workspace",
        "terraform/terraform_enterprise/list_workspaces",
    ]


def test_root_example_manifests_are_schema_valid() -> None:
    sdk_root = Path(__file__).resolve().parent.parent
    plugin = yaml.safe_load((sdk_root / "plugin.example.yaml").read_text(encoding="utf-8"))
    step = yaml.safe_load((sdk_root / "manifest.example.yaml").read_text(encoding="utf-8"))

    assert validate_plugin_manifest(plugin) == []
    assert validate_manifest(step) == []


@pytest.mark.parametrize(
    ("entrypoint_name", "document_path"),
    [
        ("manifest.schema.json", "fixtures/steps/http_request/manifest.yaml"),
        ("plugin.schema.json", "fixtures/plugins/terraform_enterprise/plugin.yaml"),
    ],
)
def test_public_schema_entrypoints_resolve_common_definitions(
    entrypoint_name: str, document_path: str
) -> None:
    sdk_root = Path(__file__).resolve().parent.parent
    entrypoint_path = sdk_root / entrypoint_name
    entrypoint_uri = entrypoint_path.as_uri()
    entrypoint = json.loads(entrypoint_path.read_text(encoding="utf-8"))
    document = yaml.safe_load((Path(__file__).parent / document_path).read_text(encoding="utf-8"))

    def retrieve_local_schema(uri: str) -> Resource[object]:
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            raise NoSuchResource(ref=uri)  # type: ignore[call-arg]
        schema_path = Path(unquote(parsed.path))
        return Resource.from_contents(json.loads(schema_path.read_text(encoding="utf-8")))

    registry = Registry(retrieve=retrieve_local_schema).with_resource(  # type: ignore[call-arg]
        entrypoint_uri, Resource.from_contents(entrypoint)
    )

    validator = Draft7Validator({"$ref": entrypoint_uri}, registry=registry)
    assert list(validator.iter_errors(document)) == []
