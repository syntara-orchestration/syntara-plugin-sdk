"""Canonical, side-effect-free compilation of Syntara plugin source workspaces."""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

import yaml
from jsonschema import Draft202012Validator, SchemaError
from syntara_plugin.contracts import (
    ContractBundle,
    load_bundle,
    schema_digests,
    validate_document,
)
from yaml.constructor import ConstructorError
from yaml.tokens import AliasToken, AnchorToken

DEFAULT_CONTAINER_ABI = "syntara.container/v1alpha1"
DEFAULT_PROVIDER_ABI = "syntara.provider/v1alpha1"
PLUGIN_MANIFEST_FILENAME = "plugin.yaml"
_JSON_SUFFIX = ".json"
_DIGEST_REFERENCE = re.compile(
    r"^(?:[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?/)?"
    r"[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*"
    r"@sha256:[a-f0-9]{64}$"
)
_MAX_DOCUMENT_BYTES = 1024 * 1024
_MAX_DOCUMENT_DEPTH = 100
_MAX_DOCUMENT_NODES = 10_000
_RECURSIVE_ALIAS_REMEDIATION = "Replace the recursive alias with finite data."


@dataclass(frozen=True, order=True)
class SourceLocation:
    """A location in author-owned source, suitable for IDE and JSON output."""

    file: str
    path: str = "$"
    line: int = 1
    column: int = 1


@dataclass(frozen=True, order=True)
class Diagnostic:
    """A stable compiler diagnostic; codes form the public adapter contract."""

    code: str
    message: str
    location: SourceLocation
    remediation: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class CompilationError(Exception):
    """Raised when source cannot safely compile into a canonical descriptor."""

    def __init__(self, diagnostics: Sequence[Diagnostic]) -> None:
        self.diagnostics = tuple(sorted(diagnostics))
        super().__init__("; ".join(f"{item.code}: {item.message}" for item in self.diagnostics))


@dataclass(frozen=True)
class BuildRequest:
    """All explicit inputs needed to compile one plugin workspace.

    ``image_bindings`` must already contain immutable image references when
    compiling a releasable descriptor. Structural validation may defer those
    release-only bindings until the workload image exists. Network resolution
    belongs to a later OCI adapter, which records its resolution in this
    request before invoking this pure compiler.
    """

    root_manifest: Path
    image_bindings: Mapping[str, str] | None = None
    require_image_bindings: bool = True
    max_document_bytes: int = _MAX_DOCUMENT_BYTES


@dataclass(frozen=True)
class Plugin:
    """Typed in-process authoring input, equivalent to a root YAML manifest.

    ``assets`` supplies the exact bytes of externally declared source files.
    In particular, a typed plugin declaring ``spec.documentation.path`` must
    provide that Markdown file under the same normalized path.
    """

    document: Mapping[str, Any]
    locations: Mapping[str, SourceLocation] = field(default_factory=dict)
    assets: Mapping[str, bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class Target:
    """Typed in-process plugin target input, equivalent to a target YAML file."""

    path: str
    document: Mapping[str, Any]
    locations: Mapping[str, SourceLocation] = field(default_factory=dict)


@dataclass(frozen=True)
class CompiledAsset:
    """One verified source asset retained for deterministic artifact assembly.

    ``content`` is the exact bounded byte sequence read from the author
    workspace.  The compiler also records the canonical parsed-document digest
    because source formatting is allowed to differ from its semantic value.
    The OCI builder indexes the former; installed schema validation relies on
    the latter.
    """

    path: str
    media_type: str
    content: bytes
    source_digest: str
    canonical_digest: str
    references: tuple[str, ...]

    def index_entry(self) -> dict[str, object]:
        """Return the JSON-safe source-byte inventory entry for this asset."""

        return {
            "canonicalDigest": self.canonical_digest,
            "digest": self.source_digest,
            "mediaType": self.media_type,
            "references": list(self.references),
            "size": len(self.content),
        }


@dataclass(frozen=True)
class CompilationResult:
    """Canonical metadata plus exact workspace assets for an artifact builder."""

    descriptor: Mapping[str, Any]
    assets: Mapping[str, CompiledAsset]
    diagnostics: tuple[Diagnostic, ...] = ()
    deferred_image_bindings: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        return _canonical_digest(self.descriptor)

    def canonical_json(self) -> bytes:
        return _canonical_bytes(self.descriptor)

    @property
    def asset_index(self) -> Mapping[str, Mapping[str, object]]:
        """Return the deterministic, JSON-safe index of bundled source files."""

        return {path: self.assets[path].index_entry() for path in sorted(self.assets)}


def compile_workspace(request: BuildRequest) -> CompilationResult:
    """Compile one explicit root manifest and its explicitly named targets."""

    root = request.root_manifest.resolve()
    root_document = _read_document(root, root.parent, request.max_document_bytes)
    documents: list[Target] = []
    targets = _declared_target_paths(root_document.value)
    if targets is not None:
        for target_path in targets:
            if not isinstance(target_path, str):
                continue
            target = _resolve_path(
                root.parent, target_path, root_document.location, "TARGET_PATH_INVALID"
            )
            target_document = _read_document(target, root.parent, request.max_document_bytes)
            documents.append(
                Target(
                    path=target_path,
                    document=target_document.value,
                    locations=target_document.locations,
                )
            )
    return _compile(
        plugin=Plugin(root_document.value, root_document.locations),
        targets=documents,
        image_bindings=request.image_bindings or {},
        require_image_bindings=request.require_image_bindings,
        root_directory=root.parent,
        typed_assets=None,
        max_document_bytes=request.max_document_bytes,
    )


def compile_model(
    plugin: Plugin, targets: Sequence[Target], *, image_bindings: Mapping[str, str] | None = None
) -> CompilationResult:
    """Compile typed input using exactly the same canonicalization as YAML input."""

    return _compile(
        plugin=plugin,
        targets=targets,
        image_bindings=image_bindings or {},
        require_image_bindings=True,
        root_directory=None,
        typed_assets=plugin.assets,
        max_document_bytes=_MAX_DOCUMENT_BYTES,
    )


@dataclass(frozen=True)
class _LoadedDocument:
    value: Mapping[str, Any]
    location: SourceLocation
    locations: Mapping[str, SourceLocation]


def _compile(
    plugin: Plugin,
    targets: Sequence[Target],
    *,
    image_bindings: Mapping[str, str],
    require_image_bindings: bool,
    root_directory: Path | None,
    typed_assets: Mapping[str, bytes] | None,
    max_document_bytes: int,
) -> CompilationResult:
    diagnostics: list[Diagnostic] = []
    bundle = load_bundle()
    _validate_contract(
        bundle, "plugin", plugin.document, PLUGIN_MANIFEST_FILENAME, plugin.locations, diagnostics
    )
    declared_targets = _declared_target_paths(plugin.document)
    paths = [target.path for target in targets]
    if declared_targets is not None and (
        len(set(paths)) != len(paths) or len(set(declared_targets)) != len(declared_targets)
    ):
        diagnostics.append(
            _diagnostic(
                "TARGET_DUPLICATE",
                "Each target must be declared once.",
                PLUGIN_MANIFEST_FILENAME,
                "Remove the duplicate target entry.",
            )
        )
    if declared_targets is not None and list(declared_targets) != paths:
        diagnostics.append(
            _diagnostic(
                "TARGET_DECLARATION_MISMATCH",
                "Targets must be compiled in the root manifest's explicit order.",
                PLUGIN_MANIFEST_FILENAME,
                "Pass exactly the explicitly declared target manifests.",
            )
        )
    canonical_targets, assets = _canonicalize_targets(
        plugin,
        targets,
        bundle,
        root_directory,
        max_document_bytes,
        diagnostics,
    )
    documentation = _documentation_source(
        plugin.document,
        root_directory,
        typed_assets,
        max_document_bytes,
        assets,
        diagnostics,
    )
    _validate_asset_paths(assets, diagnostics)
    _validate_bindings(
        plugin.document,
        canonical_targets,
        image_bindings,
        diagnostics,
        require_image_bindings=require_image_bindings,
    )
    _validate_connection_contracts(canonical_targets, diagnostics)
    _validate_unique_target_identities(canonical_targets, diagnostics)
    if diagnostics:
        raise CompilationError(diagnostics)
    descriptor_spec: dict[str, object] = {
        "assets": {key: assets[key].index_entry() for key in sorted(assets)},
        "contractBundleDigest": bundle.digest,
        "targets": canonical_targets,
        "workloads": _canonical_workloads(plugin.document, image_bindings),
    }
    if documentation is not None:
        descriptor_spec["documentation"] = documentation
    descriptor = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "PluginDescriptor",
        "metadata": _ordered(plugin.document["metadata"]),
        "spec": descriptor_spec,
    }
    for message in validate_document(bundle, "plugin_descriptor", descriptor):
        diagnostics.append(
            _diagnostic(
                "DESCRIPTOR_INVALID",
                message,
                PLUGIN_MANIFEST_FILENAME,
                "Correct the source manifests so the compiled descriptor satisfies its contract.",
            )
        )
    if diagnostics:
        raise CompilationError(diagnostics)
    return CompilationResult(
        descriptor=cast(Mapping[str, Any], _ordered(descriptor)),
        assets={key: assets[key] for key in sorted(assets)},
        deferred_image_bindings=tuple(
            sorted(
                identifier
                for identifier in _required_image_bindings(plugin.document)
                if identifier not in image_bindings
            )
        ),
    )


def _canonicalize_targets(
    plugin: Plugin,
    targets: Sequence[Target],
    bundle: ContractBundle,
    root_directory: Path | None,
    max_document_bytes: int,
    diagnostics: list[Diagnostic],
) -> tuple[list[dict[str, Any]], dict[str, CompiledAsset]]:
    """Validate and normalize target documents in their declared order."""

    canonical_targets: list[dict[str, Any]] = []
    assets: dict[str, CompiledAsset] = {}
    seen_casefold: dict[str, str] = {}
    for target in targets:
        kind = _contract_kind(target.document)
        _validate_contract(
            bundle, kind, target.document, target.path, target.locations, diagnostics
        )
        _report_path_case_collision(target.path, seen_casefold, diagnostics)
        if _contains_cycle(target.document):
            diagnostics.append(
                _diagnostic(
                    "DOCUMENT_RECURSIVE",
                    "YAML anchors may not create recursive documents.",
                    target.path,
                    _RECURSIVE_ALIAS_REMEDIATION,
                )
            )
            continue
        normalized = _normalize_target(
            target.document,
            target.path,
            plugin.document,
            bundle,
            root_directory,
            max_document_bytes,
            assets,
            diagnostics,
        )
        if normalized is not None:
            canonical_targets.append(normalized)
    return canonical_targets, assets


def _report_path_case_collision(
    path: str, seen_casefold: dict[str, str], diagnostics: list[Diagnostic]
) -> None:
    """Report a target path that collides on a case-insensitive filesystem."""

    folded = _path_key(path)
    previous = seen_casefold.setdefault(folded, path)
    if previous == path:
        return
    diagnostics.append(
        _diagnostic(
            "PATH_CASE_COLLISION",
            f"{path!r} collides with {previous!r} on case-insensitive filesystems.",
            path,
            "Use one uniquely cased path.",
        )
    )


def _normalize_target(
    document: Mapping[str, Any],
    path: str,
    plugin: Mapping[str, Any],
    bundle: ContractBundle,
    root_directory: Path | None,
    max_bytes: int,
    assets: dict[str, CompiledAsset],
    diagnostics: list[Diagnostic],
) -> dict[str, Any] | None:
    kind = document.get("kind")
    spec = document.get("spec")
    if not isinstance(spec, Mapping) or kind not in {
        "Action",
        "Control",
        "CredentialRecipe",
        "IntegrationType",
        "Trigger",
    }:
        return None
    target: dict[str, Any] = {
        "kind": kind,
        "identity": f"{plugin.get('metadata', {}).get('namespace', '')}.{plugin.get('metadata', {}).get('name', '')}.{document.get('metadata', {}).get('name', '')}",
        "version": plugin.get("metadata", {}).get("version"),
        "metadata": _ordered(document.get("metadata", {})),
        "sourcePath": path,
    }
    context = _NormalizationContext(
        path=path,
        bundle=bundle,
        root_directory=root_directory,
        max_bytes=max_bytes,
        assets=assets,
        diagnostics=diagnostics,
    )
    if kind == "Action":
        _normalize_action(target, spec, plugin, context)
    elif kind == "Control":
        _normalize_control(target, spec, plugin, context)
    elif kind == "Trigger":
        _normalize_trigger(target, spec, plugin, context)
    elif kind == "IntegrationType":
        _normalize_integration_type(target, spec, context)
    else:
        _normalize_credential_recipe(target, spec, context)
    _record_schema_digests(target)
    return cast(dict[str, Any], _ordered(target))


@dataclass
class _NormalizationContext:
    path: str
    bundle: ContractBundle
    root_directory: Path | None
    max_bytes: int
    assets: dict[str, CompiledAsset]
    diagnostics: list[Diagnostic]


def _normalize_action(
    target: dict[str, Any],
    spec: Mapping[str, Any],
    plugin: Mapping[str, Any],
    context: _NormalizationContext,
) -> None:
    runtime = _normalize_action_runtime(spec.get("runtime"), plugin, context)
    target["runtime"] = _ordered(runtime)
    for field_name in ("input", "output"):
        target[field_name] = _target_document_source(spec.get(field_name), field_name, context)
    if "error" in spec:
        target["error"] = _target_document_source(spec.get("error"), "error", context)
    for field_name in ("domain", "integrationType"):
        value = spec.get(field_name)
        if value is not None:
            target[field_name] = value


def _normalize_action_runtime(
    raw_runtime: object, plugin: Mapping[str, Any], context: _NormalizationContext
) -> dict[str, Any]:
    runtime = _normalize_runner_runtime(raw_runtime, plugin)
    if runtime.get("kind") != "custom-runner" and runtime.get("driver") == "http.v1":
        _normalize_http_operation(runtime, context)
    return runtime


def _normalize_control(
    target: dict[str, Any],
    spec: Mapping[str, Any],
    plugin: Mapping[str, Any],
    context: _NormalizationContext,
) -> None:
    """Normalize control metadata and its selected runner."""

    target["invocation"] = _ordered(spec.get("invocation"))
    target["runtime"] = _ordered(_normalize_step_runtime(spec.get("runtime"), plugin))
    for field_name in ("input", "output"):
        target[field_name] = _target_document_source(spec.get(field_name), field_name, context)
    if "error" in spec:
        target["error"] = _target_document_source(spec.get("error"), "error", context)


def _normalize_http_operation(runtime: dict[str, Any], context: _NormalizationContext) -> None:
    operation = runtime.get("operation")
    if not isinstance(operation, Mapping):
        return
    normalized = dict(operation)
    for field_name in ("requestMap", "responseMap"):
        if field_name in normalized:
            normalized[field_name] = _target_document_source(
                normalized[field_name],
                f"runtime/operation/{field_name}",
                context,
                document_kind="http_mapping",
            )
    runtime["operation"] = _ordered(normalized)


def _normalize_trigger(
    target: dict[str, Any],
    spec: Mapping[str, Any],
    plugin: Mapping[str, Any],
    context: _NormalizationContext,
) -> None:
    runtime = spec.get("runtime")
    if (
        isinstance(runtime, Mapping)
        and runtime.get("kind") == "platform-runner"
        and runtime.get("driver") != spec.get("driver")
    ):
        context.diagnostics.append(
            _diagnostic(
                "TRIGGER_DRIVER_MISMATCH",
                "Trigger spec.driver must match runtime.driver for a platform-runner.",
                context.path,
                "Use the same versioned driver in spec.driver and spec.runtime.driver.",
            )
        )
    target["driver"] = spec.get("driver")
    target["runtime"] = _ordered(_normalize_step_runtime(spec.get("runtime"), plugin))
    target["configuration"] = _target_document_source(
        spec.get("configuration"), "configuration", context
    )
    for field_name in ("input", "output"):
        target[field_name] = _target_document_source(spec.get(field_name), field_name, context)
    if "error" in spec:
        target["error"] = _target_document_source(spec.get("error"), "error", context)


def _normalize_step_runtime(raw_runtime: object, plugin: Mapping[str, Any]) -> dict[str, Any]:
    """Materialize the shared runner metadata for non-Action Step types."""

    return _normalize_runner_runtime(raw_runtime, plugin)


def _normalize_runner_runtime(raw_runtime: object, plugin: Mapping[str, Any]) -> dict[str, Any]:
    """Materialize runner metadata shared by every executable Step type."""

    runtime = dict(raw_runtime) if isinstance(raw_runtime, Mapping) else {}
    if runtime.get("kind") == "custom-runner":
        plugin_spec = plugin.get("spec", {})
        workload = plugin_spec.get("workload", {}) if isinstance(plugin_spec, Mapping) else {}
        runtime["workload"] = workload.get("image") if isinstance(workload, Mapping) else None
        runtime["abi"] = DEFAULT_CONTAINER_ABI
    return runtime


def _normalize_integration_type(
    target: dict[str, Any], spec: Mapping[str, Any], context: _NormalizationContext
) -> None:
    target["id"] = spec.get("id")
    target["endpointKinds"] = _ordered(spec.get("endpointKinds"))
    target["credentialTypes"] = _ordered(spec.get("credentialTypes"))
    target["configuration"] = _target_document_source(
        spec.get("configuration"), "configuration", context
    )


def _normalize_credential_recipe(
    target: dict[str, Any], spec: Mapping[str, Any], context: _NormalizationContext
) -> None:
    target["id"] = spec.get("id")
    target["integrationType"] = spec.get("integrationType")
    target["credentialSchema"] = _target_document_source(
        spec.get("credentialSchema"), "credentialSchema", context
    )
    target["secretFields"] = _ordered(spec.get("secretFields"))
    target["issuance"] = _ordered(spec.get("issuance"))
    output = spec.get("output")
    if output is not None:
        target["output"] = _target_document_source(output, "output", context)


def _record_schema_digests(target: dict[str, Any]) -> None:
    """Attach canonical digests for every materialized schema in a target."""

    digests = schema_digests(target)
    if digests:
        target["schemaDigests"] = digests


def _validate_unique_target_identities(
    targets: Sequence[Mapping[str, Any]], diagnostics: list[Diagnostic]
) -> None:
    """Reject identities shared by any compiled targets, regardless of kind."""

    identities: dict[str, str] = {}
    for target in targets:
        identity = target.get("identity")
        path = target.get("sourcePath", PLUGIN_MANIFEST_FILENAME)
        if not isinstance(identity, str) or not isinstance(path, str):
            continue
        previous = identities.setdefault(identity, path)
        if previous != path:
            diagnostics.append(
                _diagnostic(
                    "COMPILED_IDENTITY_DUPLICATE",
                    f"Compiled identity {identity!r} is declared by both {previous!r} and {path!r}.",
                    path,
                    "Give every target a unique metadata.name, regardless of its Step type.",
                )
            )


def _target_document_source(
    source: object,
    field_name: str,
    context: _NormalizationContext,
    *,
    document_kind: Literal["schema", "http_mapping"] = "schema",
) -> object:
    return _document_source(
        source,
        context.path,
        field_name,
        context.bundle,
        context.root_directory,
        context.max_bytes,
        context.assets,
        context.diagnostics,
        document_kind=document_kind,
    )


def _document_source(
    source: object,
    target_path: str,
    field_name: str,
    bundle: ContractBundle,
    root_directory: Path | None,
    max_bytes: int,
    assets: dict[str, CompiledAsset],
    diagnostics: list[Diagnostic],
    *,
    document_kind: Literal["schema", "http_mapping"] = "schema",
) -> object:
    if not isinstance(source, Mapping):
        return source
    if source.get("kind") == "inline":
        canonical = _ordered(source.get("value"))
        _validate_typed_document(canonical, target_path, diagnostics, bundle, document_kind)
        return canonical
    if source.get("kind") != "asset" or not isinstance(source.get("path"), str):
        return source
    asset_path = source["path"]
    if Path(asset_path).suffix in {".zip", ".gz", ".tgz", ".tar", ".xz", ".bz2"}:
        diagnostics.append(
            _diagnostic(
                "ASSET_ARCHIVE_FORBIDDEN",
                f"Archive asset {asset_path!r} is not allowed.",
                target_path,
                "Extract and reference a bounded JSON or YAML document instead.",
            )
        )
        return None
    try:
        asset_path = _normalise_asset_path(asset_path)
    except ValueError as error:
        diagnostics.append(
            _diagnostic(
                "ASSET_PATH_INVALID",
                str(error),
                target_path,
                "Use a normalized relative POSIX path inside the plugin workspace.",
            )
        )
        return None
    if root_directory is None:
        diagnostics.append(
            _diagnostic(
                "ASSET_UNAVAILABLE",
                f"Typed input cannot resolve asset {asset_path!r} without a workspace.",
                target_path,
                "Use an inline document or compile a workspace.",
            )
        )
        return None
    try:
        resolved = _resolve_path(
            root_directory, asset_path, SourceLocation(target_path), "ASSET_PATH_INVALID"
        )
        raw = _read_regular_file(resolved, max_bytes, root=root_directory)
        value = _parse_asset(resolved, raw)
        if _contains_cycle(value):
            raise ValueError("asset contains recursive YAML aliases")
    except CompilationError as error:
        diagnostics.extend(error.diagnostics)
        return None
    except (OSError, ValueError, yaml.YAMLError) as error:
        diagnostics.append(
            _diagnostic(
                "ASSET_INVALID",
                f"Cannot load asset {asset_path!r}: {error}",
                target_path,
                "Use a contained JSON or YAML document within the size limit.",
            )
        )
        return None
    canonical = _ordered(value)
    _validate_typed_document(canonical, target_path, diagnostics, bundle, document_kind)
    asset = CompiledAsset(
        path=asset_path,
        media_type=_structured_media_type(Path(asset_path), document_kind),
        content=raw,
        source_digest=f"sha256:{sha256(raw).hexdigest()}",
        canonical_digest=_canonical_digest(canonical),
        references=(f"{target_path}#/spec/{field_name}",),
    )
    existing = assets.get(asset_path)
    if existing is None:
        assets[asset_path] = asset
    elif (
        existing.media_type != asset.media_type
        or existing.content != asset.content
        or existing.source_digest != asset.source_digest
        or existing.canonical_digest != asset.canonical_digest
    ):
        diagnostics.append(
            _diagnostic(
                "ASSET_CONFLICT",
                f"Asset {asset_path!r} resolved to different source bytes.",
                target_path,
                "Reference each asset path consistently within the plugin workspace.",
            )
        )
    else:
        assets[asset_path] = replace(
            existing, references=tuple(sorted({*existing.references, *asset.references}))
        )
    return canonical


def _documentation_source(
    plugin: Mapping[str, Any],
    root_directory: Path | None,
    typed_assets: Mapping[str, bytes] | None,
    max_bytes: int,
    assets: dict[str, CompiledAsset],
    diagnostics: list[Diagnostic],
) -> dict[str, str] | None:
    """Load required Markdown documentation into the deterministic asset bundle."""
    specification = plugin.get("spec")
    source = specification.get("documentation") if isinstance(specification, Mapping) else None
    if not isinstance(source, Mapping) or not isinstance(source.get("path"), str):
        return None
    try:
        asset_path = _normalise_asset_path(source["path"])
        if not asset_path.endswith(".md"):
            raise ValueError("Documentation must use a .md Markdown path.")
    except ValueError as error:
        diagnostics.append(
            _diagnostic(
                "DOCUMENTATION_PATH_INVALID",
                str(error),
                PLUGIN_MANIFEST_FILENAME,
                "Use a normalized relative .md file inside the plugin workspace.",
            )
        )
        return None
    if root_directory is None and typed_assets is None:
        diagnostics.append(
            _diagnostic(
                "DOCUMENTATION_UNAVAILABLE",
                f"Typed input cannot resolve documentation {asset_path!r} without a workspace.",
                PLUGIN_MANIFEST_FILENAME,
                "Compile a workspace when the root manifest declares documentation.",
            )
        )
        return None
    try:
        if root_directory is None:
            assert typed_assets is not None
            raw = typed_assets[asset_path]
            if not isinstance(raw, bytes):
                raise ValueError("Typed documentation must be bytes.")
            if len(raw) > max_bytes:
                raise ValueError("Typed documentation exceeds the maximum document size.")
        else:
            resolved = _resolve_path(
                root_directory,
                asset_path,
                SourceLocation(PLUGIN_MANIFEST_FILENAME),
                "DOCUMENTATION_PATH_INVALID",
            )
            raw = _read_regular_file(resolved, max_bytes, root=root_directory)
        _validate_markdown(raw)
    except (CompilationError, KeyError) as error:
        if isinstance(error, KeyError):
            diagnostics.append(
                _diagnostic(
                    "DOCUMENTATION_UNAVAILABLE",
                    f"Typed input did not provide documentation {asset_path!r}.",
                    PLUGIN_MANIFEST_FILENAME,
                    "Provide the declared Markdown file through Plugin.assets.",
                )
            )
            return None
        diagnostics.extend(error.diagnostics)
        return None
    except (OSError, UnicodeDecodeError, ValueError) as error:
        diagnostics.append(
            _diagnostic(
                "DOCUMENTATION_INVALID",
                f"Cannot load documentation {asset_path!r}: {error}",
                PLUGIN_MANIFEST_FILENAME,
                "Use a bounded UTF-8 Markdown file inside the plugin workspace.",
            )
        )
        return None
    digest = f"sha256:{sha256(raw).hexdigest()}"
    asset = CompiledAsset(
        path=asset_path,
        media_type="text/markdown",
        content=raw,
        source_digest=digest,
        canonical_digest=digest,
        references=("plugin.yaml#/spec/documentation",),
    )
    if asset_path in assets:
        diagnostics.append(
            _diagnostic(
                "DOCUMENTATION_ASSET_CONFLICT",
                f"Documentation path {asset_path!r} is already used by another asset.",
                PLUGIN_MANIFEST_FILENAME,
                "Use a unique Markdown path for documentation.",
            )
        )
        return None
    assets[asset_path] = asset
    return {"digest": digest, "path": asset_path}


def _canonical_workloads(
    plugin: Mapping[str, Any], bindings: Mapping[str, str]
) -> dict[str, object]:
    result: dict[str, object] = {}
    workload = plugin.get("spec", {}).get("workload")
    if isinstance(workload, Mapping):
        identifier = str(workload.get("image"))
        image = bindings.get(identifier)
        if image is not None:
            result[identifier] = {"image": image, "abi": DEFAULT_CONTAINER_ABI}
    for provider in plugin.get("spec", {}).get("providers", []):
        if isinstance(provider, Mapping):
            identifier = str(provider["image"])
            image = bindings.get(identifier)
            if image is not None:
                result[identifier] = {
                    "image": image,
                    "abi": DEFAULT_PROVIDER_ABI,
                    "provider": str(provider["id"]),
                }
    return {key: result[key] for key in sorted(result)}


def _validate_bindings(
    plugin: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
    bindings: Mapping[str, str],
    diagnostics: list[Diagnostic],
    *,
    require_image_bindings: bool,
) -> None:
    workload_image = _root_workload_image(plugin)
    if workload_image is None and any(
        isinstance(target.get("runtime"), Mapping)
        and target["runtime"].get("kind") == "custom-runner"
        for target in targets
    ):
        diagnostics.append(
            _diagnostic(
                "WORKLOAD_MISSING",
                "A custom-runner step requires one root workload binding.",
                PLUGIN_MANIFEST_FILENAME,
                "Add spec.workload.image to the root manifest.",
            )
        )
    if workload_image is not None and workload_image in _provider_image_ids(plugin):
        diagnostics.append(
            _diagnostic(
                "WORKLOAD_PROVIDER_IMAGE_CONFLICT",
                f"Workload image {workload_image!r} is also used by a provider.",
                PLUGIN_MANIFEST_FILENAME,
                "Use a unique logical image ID for the workload and each provider.",
            )
        )
    required = _required_image_bindings(plugin)
    for identifier in sorted(required):
        image = bindings.get(identifier)
        if image is None and require_image_bindings:
            diagnostics.append(
                _diagnostic(
                    "IMAGE_BINDING_MISSING",
                    f"No explicit immutable binding was supplied for {identifier!r}.",
                    PLUGIN_MANIFEST_FILENAME,
                    "Supply an image binding for this logical image ID.",
                )
            )
        elif image is not None and not _DIGEST_REFERENCE.fullmatch(image):
            diagnostics.append(
                _diagnostic(
                    "IMAGE_REFERENCE_MUTABLE",
                    f"Image binding for {identifier!r} must be repository@sha256:digest.",
                    PLUGIN_MANIFEST_FILENAME,
                    "Resolve the image to an immutable sha256 digest before compilation.",
                )
            )


def _required_image_bindings(plugin: Mapping[str, Any]) -> set[str]:
    """Return every logical image identifier declared by the root manifest.

    A declared workload is the plugin's one runner image, so it requires a
    binding even when no custom-runner target uses it yet.
    """

    required = _provider_image_ids(plugin)
    workload_image = _root_workload_image(plugin)
    if workload_image is not None:
        required.add(workload_image)
    return required


def _root_workload_image(plugin: Mapping[str, Any]) -> str | None:
    workload = plugin.get("spec", {}).get("workload")
    if not isinstance(workload, Mapping):
        return None
    return str(workload.get("image"))


def _provider_image_ids(plugin: Mapping[str, Any]) -> set[str]:
    images: set[str] = set()
    for provider in plugin.get("spec", {}).get("providers", []):
        if isinstance(provider, Mapping):
            images.add(str(provider.get("image")))
    return images


def _validate_connection_contracts(
    targets: Sequence[Mapping[str, Any]], diagnostics: list[Diagnostic]
) -> None:
    """Validate immutable integration and credential links within one plugin."""

    integrations, credentials = _collect_connection_types(targets, diagnostics)
    _validate_credential_integration_links(credentials, integrations, diagnostics)
    _validate_integration_credential_links(integrations, credentials, diagnostics)
    _validate_action_integration_links(targets, integrations, diagnostics)


def _collect_connection_types(
    targets: Sequence[Mapping[str, Any]], diagnostics: list[Diagnostic]
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    integrations: dict[str, Mapping[str, Any]] = {}
    credentials: dict[str, Mapping[str, Any]] = {}
    for target in targets:
        kind = target.get("kind")
        if kind == "IntegrationType":
            _record_connection_type("Integration", target, integrations, diagnostics)
        elif kind == "CredentialRecipe":
            _record_connection_type("Credential", target, credentials, diagnostics)
    return integrations, credentials


def _record_connection_type(
    label: str,
    target: Mapping[str, Any],
    records: dict[str, Mapping[str, Any]],
    diagnostics: list[Diagnostic],
) -> None:
    target_id = target.get("id")
    if not isinstance(target_id, str):
        return
    if target_id in records:
        diagnostics.append(
            _diagnostic(
                f"{label.upper()}_TYPE_DUPLICATE",
                f"{label} type {target_id!r} is declared more than once.",
                str(target.get("sourcePath", PLUGIN_MANIFEST_FILENAME)),
                f"Declare each {label.lower()} type identity once per plugin.",
            )
        )
    records[target_id] = target


def _validate_credential_integration_links(
    credentials: Mapping[str, Mapping[str, Any]],
    integrations: Mapping[str, Mapping[str, Any]],
    diagnostics: list[Diagnostic],
) -> None:
    for credential_id, credential in credentials.items():
        integration_id = credential.get("integrationType")
        if not isinstance(integration_id, str) or integration_id not in integrations:
            diagnostics.append(
                _diagnostic(
                    "CREDENTIAL_INTEGRATION_UNDECLARED",
                    f"Credential type {credential_id!r} references undeclared integration {integration_id!r}.",
                    str(credential.get("sourcePath", PLUGIN_MANIFEST_FILENAME)),
                    "Declare the referenced IntegrationType in this plugin.",
                )
            )


def _validate_integration_credential_links(
    integrations: Mapping[str, Mapping[str, Any]],
    credentials: Mapping[str, Mapping[str, Any]],
    diagnostics: list[Diagnostic],
) -> None:
    for integration_id, integration in integrations.items():
        declared_credentials = integration.get("credentialTypes")
        if not isinstance(declared_credentials, list):
            continue
        for credential_id in declared_credentials:
            resolved_credential = (
                credentials.get(credential_id) if isinstance(credential_id, str) else None
            )
            if (
                resolved_credential is None
                or resolved_credential.get("integrationType") != integration_id
            ):
                diagnostics.append(
                    _diagnostic(
                        "INTEGRATION_CREDENTIAL_UNDECLARED",
                        f"Integration type {integration_id!r} references undeclared or incompatible credential type {credential_id!r}.",
                        str(integration.get("sourcePath", PLUGIN_MANIFEST_FILENAME)),
                        "Declare a CredentialRecipe for this integration type in the same plugin.",
                    )
                )


def _validate_action_integration_links(
    targets: Sequence[Mapping[str, Any]],
    integrations: Mapping[str, Mapping[str, Any]],
    diagnostics: list[Diagnostic],
) -> None:
    for target in targets:
        if target.get("kind") != "Action":
            continue
        integration_id = target.get("integrationType")
        if integration_id is not None and integration_id not in integrations:
            diagnostics.append(
                _diagnostic(
                    "ACTION_INTEGRATION_UNDECLARED",
                    f"Action references undeclared integration type {integration_id!r}.",
                    str(target.get("sourcePath", PLUGIN_MANIFEST_FILENAME)),
                    "Declare the referenced IntegrationType in this plugin.",
                )
            )


def _declared_target_paths(document: Mapping[str, Any]) -> list[object] | None:
    spec = document.get("spec")
    if not isinstance(spec, Mapping):
        return None
    targets = spec.get("targets")
    return targets if isinstance(targets, list) else None


def _validate_schema_document(
    value: object, target_path: str, diagnostics: list[Diagnostic]
) -> None:
    if not isinstance(value, Mapping):
        diagnostics.append(
            _diagnostic(
                "SCHEMA_DOCUMENT_INVALID",
                "A schema document must be a JSON object.",
                target_path,
                "Use a JSON Schema object for the document source.",
            )
        )
        return
    try:
        Draft202012Validator.check_schema(value)
    except SchemaError as error:
        diagnostics.append(
            _diagnostic(
                "SCHEMA_DOCUMENT_INVALID",
                f"Invalid JSON Schema: {error.message}",
                target_path,
                "Correct the JSON Schema before compiling.",
            )
        )


def _validate_typed_document(
    value: object,
    target_path: str,
    diagnostics: list[Diagnostic],
    bundle: ContractBundle,
    document_kind: Literal["schema", "http_mapping"],
) -> None:
    """Validate an inert document source according to its declared contract."""

    if document_kind == "schema":
        _validate_schema_document(value, target_path, diagnostics)
        return
    for message in validate_document(bundle, "http_mapping", value):
        diagnostics.append(
            _diagnostic(
                "HTTP_MAPPING_INVALID",
                message,
                target_path,
                "Use the bounded HTTP mapping format declared by the local contract bundle.",
            )
        )


def _validate_asset_paths(
    assets: Mapping[str, CompiledAsset], diagnostics: list[Diagnostic]
) -> None:
    """Reject source paths that cannot be represented safely on every host."""

    seen_casefold: dict[str, str] = {}
    for path in sorted(assets):
        folded = _path_key(path)
        previous = seen_casefold.setdefault(folded, path)
        if previous != path:
            diagnostics.append(
                _diagnostic(
                    "ASSET_PATH_CASE_COLLISION",
                    f"Asset path {path!r} collides with {previous!r} on case-insensitive filesystems.",
                    path,
                    "Use one uniquely cased asset path.",
                )
            )


def _validate_contract(
    bundle: ContractBundle,
    kind: str,
    document: Mapping[str, Any],
    file: str,
    locations: Mapping[str, SourceLocation],
    diagnostics: list[Diagnostic],
) -> None:
    if kind not in {
        "plugin",
        "action",
        "control",
        "credential_recipe",
        "integration",
        "trigger",
    }:
        diagnostics.append(
            _diagnostic(
                "TARGET_KIND_INVALID",
                "Target kind must be Action, Control, Trigger, IntegrationType, or CredentialRecipe.",
                file,
                "Use one supported target kind.",
            )
        )
        return
    for message in validate_document(
        bundle,
        cast(
            Literal[
                "plugin",
                "action",
                "control",
                "credential_recipe",
                "integration",
                "trigger",
            ],
            kind,
        ),
        document,
    ):
        path = message.partition(":")[0].replace(".", "/")
        diagnostics.append(
            _diagnostic(
                "SCHEMA_INVALID",
                message,
                file,
                "Correct the source document to satisfy the published contract.",
                locations.get(path),
            )
        )


def _contract_kind(document: Mapping[str, Any]) -> str:
    return {
        "Action": "action",
        "Control": "control",
        "CredentialRecipe": "credential_recipe",
        "IntegrationType": "integration",
        "Trigger": "trigger",
    }.get(str(document.get("kind")), "unknown")


def _read_document(path: Path, root: Path, max_bytes: int) -> _LoadedDocument:
    _ensure_contained(path, root)
    try:
        raw = _read_regular_file(path, max_bytes, root=root)
        _reject_yaml_aliases(raw)
        value = yaml.load(raw, Loader=_UniqueKeyLoader)
        _enforce_document_limits(value)
    except OSError as error:
        raise CompilationError(
            [
                _diagnostic(
                    "DOCUMENT_UNREADABLE",
                    f"Cannot read {path.name}: {error}",
                    str(path),
                    "Use a readable regular file inside the plugin workspace.",
                )
            ]
        ) from error
    except ValueError as error:
        code = (
            "DOCUMENT_TOO_LARGE"
            if "larger than" in str(error)
            else "YAML_ALIAS_FORBIDDEN"
            if "anchors" in str(error)
            else "DOCUMENT_LIMIT_EXCEEDED"
        )
        raise CompilationError(
            [
                _diagnostic(
                    code,
                    str(error),
                    str(path),
                    "Use a bounded YAML document without anchors or aliases.",
                )
            ]
        ) from error
    except (yaml.YAMLError, ConstructorError) as error:
        recursive = "recursive" in str(error).lower()
        code = "DOCUMENT_RECURSIVE" if recursive else "YAML_INVALID"
        remediation = (
            _RECURSIVE_ALIAS_REMEDIATION if recursive else "Fix the YAML syntax or duplicate key."
        )
        raise CompilationError([_diagnostic(code, str(error), str(path), remediation)]) from error
    if not isinstance(value, Mapping):
        raise CompilationError(
            [
                _diagnostic(
                    "DOCUMENT_INVALID",
                    "A manifest must be a YAML mapping.",
                    str(path),
                    "Use an object at the document root.",
                )
            ]
        )
    if _contains_cycle(value):
        raise CompilationError(
            [
                _diagnostic(
                    "DOCUMENT_RECURSIVE",
                    "YAML anchors may not create recursive documents.",
                    str(path),
                    _RECURSIVE_ALIAS_REMEDIATION,
                )
            ]
        )
    relative_file = str(path.relative_to(root))
    return _LoadedDocument(
        value=value,
        location=SourceLocation(file=relative_file),
        locations=_yaml_locations(raw, relative_file),
    )


def _parse_asset(path: Path, raw: bytes) -> object:
    if path.suffix == _JSON_SUFFIX:
        return json.loads(raw)
    if path.suffix in {".yaml", ".yml"}:
        _reject_yaml_aliases(raw)
        value = yaml.load(raw, Loader=_UniqueKeyLoader)
        _enforce_document_limits(value)
        return value
    raise ValueError("only .json, .yaml, and .yml assets are supported")


def _structured_media_type(path: Path, document_kind: Literal["schema", "http_mapping"]) -> str:
    """Return the source-byte media type for a bounded structured document."""

    if document_kind == "schema":
        return (
            "application/schema+json" if path.suffix == _JSON_SUFFIX else "application/schema+yaml"
        )
    return "application/json" if path.suffix == _JSON_SUFFIX else "application/yaml"


def _validate_markdown(raw: bytes) -> None:
    """Reject ambiguous binary/non-text documentation before it enters an artifact."""
    if b"\x00" in raw:
        raise ValueError("documentation contains a NUL byte")
    _ = raw.decode("utf-8")


def _normalise_asset_path(raw: str) -> str:
    """Validate an authored asset location before it becomes an OCI tar entry."""

    if not raw:
        raise ValueError("Asset path must not be empty.")
    if raw != unicodedata.normalize("NFC", raw):
        raise ValueError("Asset paths must use Unicode NFC normalization.")
    if "\\" in raw or raw.startswith("/") or PurePosixPath(raw).is_absolute():
        raise ValueError("Asset paths must be relative POSIX paths.")
    if len(raw) > 1024:
        raise ValueError("Asset paths must not exceed 1024 characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise ValueError("Asset paths must not contain control characters.")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
        raise ValueError("Asset paths must not contain empty, hidden, '.' or '..' segments.")
    return str(PurePosixPath(raw))


def _read_regular_file(path: Path, max_bytes: int, *, root: Path) -> bytes:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("the platform does not support no-follow file opens")
    try:
        relative = path.relative_to(root.resolve())
    except ValueError as error:
        raise OSError("path escapes the workspace") from error
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    directory_descriptor = os.open(root, flags | directory)
    try:
        for component in relative.parts[:-1]:
            next_descriptor = os.open(component, flags | directory, dir_fd=directory_descriptor)
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        descriptor = os.open(relative.parts[-1], flags, dir_fd=directory_descriptor)
    finally:
        os.close(directory_descriptor)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("path is not a regular file")
        if metadata.st_size > max_bytes:
            raise ValueError(f"document is larger than {max_bytes} bytes")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            raise ValueError(f"document is larger than {max_bytes} bytes")
        return raw
    finally:
        os.close(descriptor)


def _reject_yaml_aliases(raw: bytes) -> None:
    for token in yaml.scan(raw):
        if isinstance(token, (AliasToken, AnchorToken)):
            raise ValueError("YAML anchors and aliases are not supported")


def _enforce_document_limits(value: object) -> None:
    pending: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > _MAX_DOCUMENT_NODES:
            raise ValueError(f"document exceeds {_MAX_DOCUMENT_NODES} nodes")
        if depth > _MAX_DOCUMENT_DEPTH:
            raise ValueError(f"document exceeds {_MAX_DOCUMENT_DEPTH} nesting levels")
        if isinstance(item, Mapping):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend((child, depth + 1) for child in item)


def _resolve_path(root: Path, relative: str, location: SourceLocation, code: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise CompilationError(
            [
                _diagnostic(
                    code,
                    f"{relative!r} escapes the plugin workspace.",
                    location.file,
                    "Use a relative path inside the plugin workspace.",
                )
            ]
        ) from error
    if not candidate.is_file():
        raise CompilationError(
            [
                _diagnostic(
                    code,
                    f"{relative!r} is not a regular file.",
                    location.file,
                    "Reference an existing regular file inside the plugin workspace.",
                )
            ]
        )
    return candidate


def _ensure_contained(path: Path, root: Path) -> None:
    try:
        path.relative_to(root.resolve())
    except ValueError as error:
        raise CompilationError(
            [
                _diagnostic(
                    "PATH_OUTSIDE_WORKSPACE",
                    f"{path} escapes the plugin workspace.",
                    str(path),
                    "Use a contained root manifest.",
                )
            ]
        ) from error


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _contains_cycle(value: object, ancestors: set[int] | None = None) -> bool:
    ancestors = ancestors or set()
    if not isinstance(value, (Mapping, list, tuple)):
        return False
    identifier = id(value)
    if identifier in ancestors:
        return True
    return any(
        _contains_cycle(item, ancestors | {identifier})
        for item in (value.values() if isinstance(value, Mapping) else value)
    )


def _path_key(path: str) -> str:
    """Filesystem-safe comparison key for explicit manifest paths."""

    return unicodedata.normalize("NFC", path).casefold()


def _yaml_locations(raw: bytes, file: str) -> Mapping[str, SourceLocation]:
    """Return JSON-pointer-like locations without coupling adapters to PyYAML nodes."""

    root = yaml.compose(raw)
    locations: dict[str, SourceLocation] = {"$": SourceLocation(file=file)}
    if root is not None:
        _record_yaml_locations(root, file, locations, set(), "")
    return locations


def _record_yaml_locations(
    node: yaml.Node,
    file: str,
    locations: dict[str, SourceLocation],
    visited: set[int],
    pointer: str,
) -> None:
    if id(node) in visited:
        return
    visited.add(id(node))
    locations[pointer] = SourceLocation(
        file=file,
        path=f"$/{pointer}" if pointer else "$",
        line=node.start_mark.line + 1,
        column=node.start_mark.column + 1,
    )
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            if isinstance(key, yaml.ScalarNode):
                _record_yaml_locations(
                    value, file, locations, visited, f"{pointer}/{key.value}".strip("/")
                )
    elif isinstance(node, yaml.SequenceNode):
        for index, value in enumerate(node.value):
            _record_yaml_locations(value, file, locations, visited, f"{pointer}/{index}".strip("/"))


def _ordered(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _ordered(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, list):
        return [_ordered(item) for item in value]
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _ordered(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _canonical_digest(value: object) -> str:
    return f"sha256:{sha256(_canonical_bytes(value)).hexdigest()}"


def _diagnostic(
    code: str, message: str, file: str, remediation: str, location: SourceLocation | None = None
) -> Diagnostic:
    return Diagnostic(code, message, location or SourceLocation(file=file), remediation)
