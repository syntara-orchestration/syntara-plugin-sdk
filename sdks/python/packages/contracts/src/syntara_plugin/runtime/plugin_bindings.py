"""Wrap the installed-manifest binding sequence for one packaged plugin.

Packaged resources are trusted like the package's importable code: path
checks validate document shape, not filesystem identity, so symlink
containment is not enforced (unlike the compiler's workspace reader).
"""

from __future__ import annotations

import importlib
import math
import unicodedata
from collections.abc import Mapping
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any

import yaml
from syntara_plugin.contracts import load_bundle, validate_document
from yaml.constructor import ConstructorError
from yaml.tokens import AliasToken, AnchorToken

from .bindings import (
    InstalledStepManifest,
    StepBindingError,
    StepBindings,
    StepDescriptor,
)
from .step import BaseStep

PLUGIN_MANIFEST_FILENAME = "plugin.yaml"
DEFAULT_MANIFESTS_ROOT = "manifests"
_MAX_DOCUMENT_BYTES = 1024 * 1024
_MAX_DOCUMENT_DEPTH = 100
_MAX_DOCUMENT_NODES = 10_000
_MAX_PATH_CHARACTERS = 1024
_STEP_KINDS = frozenset({"Action", "Control", "Trigger"})
_DECLARED_KINDS = _STEP_KINDS | frozenset({"IntegrationType", "CredentialRecipe"})
_SCHEMA_SOURCE_FIELDS = ("input", "output", "error")


def load_plugin_bindings(
    package: str,
    *,
    manifests_root: str = DEFAULT_MANIFESTS_ROOT,
) -> StepBindings:
    """Load one packaged plugin and bind every custom-runner Step target.

    Reads ``<package>/<manifests_root>/plugin.yaml`` and the target manifests
    it declares from package resources, in the formats the compiler accepts:
    YAML or JSON documents with inline or asset-backed schema sources. Each
    custom-runner Action, Control, and Trigger target is bound through
    ``InstalledStepManifest.from_manifest`` and
    ``StepDescriptor.for_installed_step`` with its identity derived as
    ``namespace.plugin.step``, so the existing provenance and drift checks
    apply. Platform-runner targets, ``IntegrationType`` targets, and
    ``CredentialRecipe`` targets are skipped.

    Args:
        package: The importable plugin package that carries the manifests.
        manifests_root: The package subdirectory that mirrors the plugin
            workspace and holds ``plugin.yaml``.

    Raises:
        StepBindingError: When the plugin package or any packaged manifest
            is missing or malformed, a declared asset cannot be read, an
            entry point cannot be imported or does not name a ``BaseStep``
            subclass, the manifest kind does not match the implementation,
            a step identity is declared twice, or the plugin declares no
            custom-runner Step target.
    """

    root = _plugin_root(package, manifests_root)
    plugin = _plugin_document(root)
    return _bind_declared_targets(root, plugin)


def _plugin_root(package: str, manifests_root: str) -> Traversable:
    """Return the package resource directory that mirrors the workspace."""

    root_path = _contained_relative_path(manifests_root, "manifests_root")
    try:
        return files(package).joinpath(root_path)
    except (ImportError, ValueError, TypeError) as error:
        message = f"plugin package {package!r} could not be imported: {error}"
        raise StepBindingError(message) from error


def _plugin_document(root: Traversable) -> dict[str, Any]:
    """Read and contract-validate the packaged root plugin manifest."""

    raw = _read_resource(root, PLUGIN_MANIFEST_FILENAME, "plugin manifest")
    document = _parse_document(raw, PLUGIN_MANIFEST_FILENAME)
    contract_errors = validate_document(load_bundle(), "plugin", document)
    if contract_errors:
        message = (
            "the packaged plugin.yaml violates its contract: "
            + "; ".join(contract_errors)
        )
        raise StepBindingError(message)
    return document


def _bind_declared_targets(
    root: Traversable, plugin: Mapping[str, Any]
) -> StepBindings:
    """Bind every custom-runner Step target the plugin declares, in order."""

    metadata = plugin["metadata"]
    specification = plugin["spec"]
    namespace = metadata["namespace"]
    plugin_name = metadata["name"]
    bindings = StepBindings()
    declared: dict[str, str] = {}
    for raw_path in specification["targets"]:
        target_path = _contained_relative_path(raw_path, "plugin.spec.targets entry")
        document = _parse_document(
            _read_resource(root, target_path, "target manifest"), target_path
        )
        target = _custom_runner_target(document, target_path)
        if target is None:
            continue
        target_name, runtime = target
        identity = f"{namespace}.{plugin_name}.{target_name}"
        previous = declared.get(identity)
        if previous is not None:
            message = (
                f"the packaged plugin declares step identity {identity!r} twice, "
                f"in {previous!r} and {target_path!r}; give every target a unique "
                "metadata.name"
            )
            raise StepBindingError(message)
        declared[identity] = target_path
        _bind_custom_runner_target(root, target_path, document, identity, runtime, bindings)
    if not bindings.step_identities:
        message = (
            f"the packaged plugin {namespace}.{plugin_name} declares no "
            "custom-runner Action, Control, or Trigger targets"
        )
        raise StepBindingError(message)
    return bindings


def _custom_runner_target(
    document: Mapping[str, Any], target_path: str
) -> tuple[str, Mapping[str, Any]] | None:
    """Return the metadata name and runtime of one custom-runner Step target.

    Returns ``None`` for targets the wrapper skips: ``IntegrationType`` and
    ``CredentialRecipe`` targets, and Step targets whose runtime is not
    ``custom-runner``. Unknown kinds are rejected.
    """

    kind = document.get("kind")
    if kind not in _DECLARED_KINDS:
        message = (
            f"packaged target manifest {target_path!r} declares unsupported "
            f"kind {kind!r}; expected one of {', '.join(sorted(_DECLARED_KINDS))}"
        )
        raise StepBindingError(message)
    if kind not in _STEP_KINDS:
        return None
    specification = document.get("spec")
    runtime = (
        specification.get("runtime") if isinstance(specification, Mapping) else None
    )
    if not isinstance(runtime, Mapping) or runtime.get("kind") != "custom-runner":
        return None
    target_metadata = document.get("metadata")
    target_name = (
        target_metadata.get("name") if isinstance(target_metadata, Mapping) else None
    )
    if not isinstance(target_name, str) or not target_name:
        message = f"packaged target manifest {target_path!r} must declare metadata.name"
        raise StepBindingError(message)
    return target_name, runtime


def _bind_custom_runner_target(
    root: Traversable,
    target_path: str,
    document: Mapping[str, Any],
    identity: str,
    runtime: Mapping[str, Any],
    bindings: StepBindings,
) -> None:
    """Run the wrapped binding sequence for one custom-runner Step target."""

    assets = _declared_assets(root, target_path, document)
    try:
        installed = InstalledStepManifest.from_manifest(identity, document, assets)
        step = _declared_step(runtime.get("entryPoint"), target_path)
        descriptor = StepDescriptor.for_installed_step(step, installed)
    except StepBindingError as error:
        message = f"packaged target manifest {target_path!r}: {error}"
        raise StepBindingError(message) from error
    bindings.bind(descriptor, step)


def _declared_assets(
    root: Traversable, target_path: str, document: Mapping[str, Any]
) -> dict[str, Mapping[str, Any]]:
    """Resolve the asset-backed schema documents one target declares."""

    assets: dict[str, Mapping[str, Any]] = {}
    specification = document.get("spec")
    if not isinstance(specification, Mapping):
        return assets
    for field_name in _SCHEMA_SOURCE_FIELDS:
        source = specification.get(field_name)
        if not isinstance(source, Mapping) or source.get("kind") != "asset":
            continue
        asset_path = _asset_path(source, target_path)
        if asset_path in assets:
            continue
        raw = _read_resource(root, asset_path, "asset document")
        assets[asset_path] = _parse_document(raw, asset_path)
    return assets


def _declared_step(entry_point: object, target_path: str) -> BaseStep[Any, Any]:
    """Import and instantiate the implementation one target declares."""

    if not isinstance(entry_point, str):
        message = (
            f"packaged target manifest {target_path!r} must declare "
            "spec.runtime.entryPoint"
        )
        raise StepBindingError(message)
    module_name, separator, class_name = entry_point.partition(":")
    if not module_name or not separator or not class_name:
        message = (
            f"packaged target manifest {target_path!r} declares an invalid "
            f"entry point {entry_point!r}; use the 'module:Class' form"
        )
        raise StepBindingError(message)
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        message = (
            f"could not import step entry point {entry_point!r} declared by "
            f"{target_path!r}: {error}"
        )
        raise StepBindingError(message) from error
    step_class = getattr(module, class_name, None)
    if step_class is None:
        message = (
            f"step entry point {entry_point!r} declared by {target_path!r}: "
            f"module {module_name!r} has no attribute {class_name!r}"
        )
        raise StepBindingError(message)
    if not isinstance(step_class, type) or not issubclass(step_class, BaseStep):
        message = (
            f"step entry point {entry_point!r} declared by {target_path!r} "
            "must name a BaseStep subclass"
        )
        raise StepBindingError(message)
    try:
        return step_class()
    except TypeError as error:
        message = (
            f"step class {entry_point!r} declared by {target_path!r} must be "
            "constructible without arguments"
        )
        raise StepBindingError(message) from error


def _read_resource(root: Traversable, relative: str, label: str) -> bytes:
    """Read one bounded resource file inside the packaged plugin."""

    resource = root.joinpath(*relative.split("/"))
    try:
        return resource.read_bytes()
    except (OSError, KeyError) as error:
        message = f"could not read packaged {label} {relative!r}: {error}"
        raise StepBindingError(message) from error


def _parse_document(raw: bytes, label: str) -> dict[str, Any]:
    """Parse one packaged YAML or JSON document with the compiler's rules."""

    if len(raw) > _MAX_DOCUMENT_BYTES:
        message = f"packaged manifest {label!r} is larger than {_MAX_DOCUMENT_BYTES} bytes"
        raise StepBindingError(message)
    if _uses_yaml_aliases(raw):
        message = f"packaged manifest {label!r} must not use YAML anchors or aliases"
        raise StepBindingError(message)
    try:
        value = yaml.load(raw, Loader=_UniqueKeyLoader)
    except RecursionError:
        message = f"packaged manifest {label!r} is nested too deeply to parse"
        raise StepBindingError(message) from None
    except (yaml.YAMLError, TypeError) as error:
        message = f"packaged manifest {label!r} is not a valid YAML document: {error}"
        raise StepBindingError(message) from error
    if not isinstance(value, Mapping):
        message = f"packaged manifest {label!r} must be a YAML mapping"
        raise StepBindingError(message)
    _enforce_document_limits(value, label)
    _enforce_json_values(value, label)
    return dict(value)


def _uses_yaml_aliases(raw: bytes) -> bool:
    try:
        return any(
            isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(raw)
        )
    except yaml.YAMLError:
        return False


def _enforce_document_limits(value: object, label: str) -> None:
    """Bound one parsed document the way the compiler bounds workspace files."""

    pending: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > _MAX_DOCUMENT_NODES:
            message = f"packaged manifest {label!r} exceeds {_MAX_DOCUMENT_NODES} nodes"
            raise StepBindingError(message)
        if depth > _MAX_DOCUMENT_DEPTH:
            message = f"packaged manifest {label!r} exceeds {_MAX_DOCUMENT_DEPTH} nesting levels"
            raise StepBindingError(message)
        if isinstance(item, Mapping):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend((child, depth + 1) for child in item)


def _enforce_json_values(value: object, label: str) -> None:
    """Reject YAML implicit types the runtime cannot freeze as JSON."""

    pending: list[object] = [value]
    while pending:
        pending.extend(_json_children(pending.pop(), label))


def _json_children(item: object, label: str) -> list[object]:
    """Validate one parsed value and return its children for traversal."""

    if item is None or isinstance(item, (str, bool, int)):
        return []
    if isinstance(item, float):
        if not math.isfinite(item):
            message = f"packaged manifest {label!r} contains a non-finite number"
            raise StepBindingError(message)
        return []
    if isinstance(item, Mapping):
        if any(not isinstance(key, str) for key in item):
            message = (
                f"packaged manifest {label!r} contains a non-string mapping key; "
                "quote keys that YAML would read as numbers or booleans"
            )
            raise StepBindingError(message)
        return list(item.values())
    if isinstance(item, (list, tuple)):
        return list(item)
    message = (
        f"packaged manifest {label!r} contains a {type(item).__name__} value; "
        "quote scalars that YAML would read as dates, timestamps, or other "
        "non-JSON types"
    )
    raise StepBindingError(message)


def _contained_relative_path(raw: object, label: str) -> str:
    """Validate one relative POSIX resource path inside the package."""

    if not isinstance(raw, str) or not raw:
        message = f"{label} must be a non-empty string path"
        raise StepBindingError(message)
    if len(raw) > _MAX_PATH_CHARACTERS:
        message = f"{label} must not exceed {_MAX_PATH_CHARACTERS} characters"
        raise StepBindingError(message)
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        message = f"{label} must not contain control characters"
        raise StepBindingError(message)
    if raw != unicodedata.normalize("NFC", raw):
        message = f"{label} must use Unicode NFC normalization"
        raise StepBindingError(message)
    if "\\" in raw or raw.startswith("/"):
        message = f"{label} must be a relative POSIX path"
        raise StepBindingError(message)
    if any(part in {"", ".", ".."} for part in raw.split("/")):
        message = f"{label} must not contain empty, '.', or '..' segments"
        raise StepBindingError(message)
    return raw


def _asset_path(source: Mapping[str, Any], target_path: str) -> str:
    """Validate one declared asset path the way the compiler normalizes it."""

    asset_path = _contained_relative_path(
        source.get("path"), f"asset path declared by {target_path!r}"
    )
    if any(part.startswith(".") for part in asset_path.split("/")):
        message = (
            f"asset path {asset_path!r} declared by {target_path!r} must not "
            "contain hidden segments"
        )
        raise StepBindingError(message)
    return asset_path


class _UniqueKeyLoader(yaml.SafeLoader):
    """A YAML loader that rejects duplicate mapping keys."""


def _construct_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "unsupported unhashable mapping key",
                key_node.start_mark,
            ) from None
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)
