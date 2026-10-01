"""Validation and discovery for step and root plugin manifests.

The SDK validates source contracts and returns descriptors. It intentionally does
not choose how a platform persists or indexes those descriptors.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, cast
from urllib.parse import urlparse

import yaml
from jsonschema import Draft7Validator  # type: ignore[import-untyped]

ManifestKind = Literal["StepTypeManifest", "PluginManifest"]


@dataclass(frozen=True)
class StepDescriptor:
    """A validated step discovered from a root plugin manifest."""

    target: Path
    manifest: dict[str, Any]
    identity: str


@dataclass(frozen=True)
class PluginDescriptor:
    """A validated plugin and its ordered, validated step descriptors."""

    root: Path
    manifest: dict[str, Any]
    steps: tuple[StepDescriptor, ...]


class PluginDiscoveryError(ValueError):
    """Raised when a root plugin manifest or one of its targets is invalid."""


def _load_yaml_mapping(path: Path, resource_name: str) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"{resource_name} not found: {path}")
    if not path.is_file():
        raise ValueError(f"{resource_name} is not a file: {path}")
    with path.open(encoding="utf-8") as stream:
        document = yaml.safe_load(stream)
    if not isinstance(document, dict):
        raise ValueError(f"{resource_name} must contain a YAML mapping: {path}")
    return document


def load_manifest(manifest_path: str | Path) -> dict[str, Any]:
    """Load a standalone StepType manifest mapping.

    This public API retains its existing name and behaviour for valid step
    manifests while producing a clear error for non-mapping YAML documents.
    """
    return _load_yaml_mapping(Path(manifest_path).resolve(), "Manifest")


def load_plugin_manifest(plugin_path: str | Path) -> dict[str, Any]:
    """Load a root ``plugin.yaml`` mapping without discovering its targets."""
    return _load_yaml_mapping(Path(plugin_path).resolve(), "Plugin manifest")


def load_common_definitions(schemas_root: Path | None = None) -> dict[str, Any]:
    """Load the SDK's shared Draft-07 schema definitions."""
    if schemas_root is None:
        schemas_root = Path(__file__).parent.parent.parent / "schemas"
    schema_path = Path(schemas_root) / "common-definitions.json"
    if not schema_path.exists():
        raise FileNotFoundError(
            f"common-definitions.json not found at {schema_path}. "
            "Pass schemas_root or preserve the SDK repository layout."
        )
    with schema_path.open(encoding="utf-8") as stream:
        return cast(dict[str, Any], json.load(stream))


def build_validator(
    schemas_root: Path | None = None,
    manifest_kind: ManifestKind = "StepTypeManifest",
) -> Draft7Validator:
    """Build a validator for a StepType or Plugin manifest.

    Calling this with no arguments remains the public standalone-step API.
    """
    common = load_common_definitions(schemas_root)
    schema: dict[str, Any] = {"$ref": f"common-definitions.json#/definitions/{manifest_kind}"}
    try:
        from referencing import Registry, Resource

        registry = Registry().with_resources(
            [
                ("common-definitions.json", Resource.from_contents(common)),
                (common["$id"], Resource.from_contents(common)),
            ]
        )
        return Draft7Validator(schema, registry=registry)
    except ImportError:  # pragma: no cover - legacy jsonschema
        from jsonschema import RefResolver

        resolver = RefResolver(
            base_uri="",
            referrer=schema,
            store={"common-definitions.json": common, common["$id"]: common},
        )
        return Draft7Validator(schema, resolver=resolver)


def _validation_errors(validator: Draft7Validator, manifest: dict[str, Any]) -> list[str]:
    errors = sorted(validator.iter_errors(manifest), key=lambda error: list(error.path))
    return [f"{'/'.join(map(str, error.path)) or '<root>'}: {error.message}" for error in errors]


def validate_manifest(manifest: dict[str, Any], schemas_root: Path | None = None) -> list[str]:
    """Validate one standalone StepType manifest and return error messages."""
    return _validation_errors(build_validator(schemas_root), manifest)


def validate_plugin_manifest(
    manifest: dict[str, Any], schemas_root: Path | None = None
) -> list[str]:
    """Validate one root Plugin manifest without loading its target files."""
    return _validation_errors(build_validator(schemas_root, "PluginManifest"), manifest)


def canonical_step_identity(plugin_namespace: str, plugin_name: str, step_name: str) -> str:
    """Return the source-contract identity for a step in a plugin."""
    return f"{plugin_namespace}/{plugin_name}/{step_name}"


def _target_path(plugin_root: Path, target: str) -> Path:
    parsed = urlparse(target)
    raw_path = Path(target)
    windows_path = PureWindowsPath(target)
    if parsed.scheme or "://" in target:
        raise PluginDiscoveryError(f"target {target!r}: URLs are not supported")
    if raw_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise PluginDiscoveryError(f"target {target!r}: absolute paths are not supported")
    if any(part == ".." for part in raw_path.parts):
        raise PluginDiscoveryError(f"target {target!r}: path traversal is not supported")

    resolved = (plugin_root / raw_path).resolve()
    if resolved != plugin_root and plugin_root not in resolved.parents:
        raise PluginDiscoveryError(f"target {target!r}: resolves outside the plugin root")
    return resolved


def discover_plugin(plugin_path: str | Path, schemas_root: Path | None = None) -> PluginDescriptor:
    """Load, validate, and explicitly discover every step named by ``plugin.yaml``.

    Targets are resolved from the root manifest's directory in listed order. No
    directory scanning occurs; the explicit target list is authoritative.
    """
    plugin_file = Path(plugin_path).resolve()
    plugin_root = plugin_file.parent
    try:
        plugin = load_plugin_manifest(plugin_file)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        raise PluginDiscoveryError(str(exc)) from exc

    errors = validate_plugin_manifest(plugin, schemas_root)
    if errors:
        raise PluginDiscoveryError("Plugin validation failed:\n" + "\n".join(errors))

    metadata = plugin["metadata"]
    steps: list[StepDescriptor] = []
    names: set[str] = set()
    for target in plugin["spec"]["targets"]:
        assert isinstance(target, str)
        try:
            target_path = _target_path(plugin_root, target)
            if not target_path.exists():
                raise PluginDiscoveryError(f"target {target!r}: file does not exist")
            if not target_path.is_file():
                raise PluginDiscoveryError(f"target {target!r}: expected a file, found a directory")
            step = _load_yaml_mapping(target_path, f"target {target!r}")
        except (OSError, ValueError, yaml.YAMLError) as exc:
            message = str(exc)
            if message.startswith("target "):
                raise PluginDiscoveryError(message) from exc
            raise PluginDiscoveryError(f"target {target!r}: {message}") from exc

        if step.get("kind") != "StepType":
            raise PluginDiscoveryError(f"target {target!r}: kind must be StepType")
        step_errors = validate_manifest(step, schemas_root)
        if step_errors:
            raise PluginDiscoveryError(
                f"target {target!r}: step validation failed:\n" + "\n".join(step_errors)
            )
        step_metadata = step["metadata"]
        step_name = step_metadata["name"]
        if step_name in names:
            raise PluginDiscoveryError(f"target {target!r}: duplicate step metadata/name {step_name!r}")
        names.add(step_name)
        steps.append(
            StepDescriptor(
                target=target_path,
                manifest=step,
                identity=canonical_step_identity(metadata["namespace"], metadata["name"], step_name),
            )
        )
    return PluginDescriptor(root=plugin_root, manifest=plugin, steps=tuple(steps))


def compile_manifest_data(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate and return an already-parsed standalone step manifest."""
    errors = validate_manifest(manifest)
    if errors:
        raise ValueError("Manifest validation failed:\n" + "\n".join(errors))
    return manifest


def compile_manifest(manifest_path: str | Path) -> dict[str, Any]:
    """Compile a standalone StepType manifest into its validated source descriptor."""
    return compile_manifest_data(load_manifest(manifest_path))
