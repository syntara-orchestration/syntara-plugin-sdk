"""Load and validate the self-contained v1alpha1 contract bundle."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ContractKind = Literal[
    "plugin",
    "plugin_build_settings",
    "action",
    "control",
    "trigger",
    "step",
    "plugin_descriptor",
    "registration_manifest",
    "resolved_execution",
    "runtime",
    "provider",
    "catalog_index",
    "catalog_source",
    "catalog_source_status",
    "integration",
    "http_mapping",
    "http_runner_context",
    "credential_recipe",
    "trigger_driver",
    "trigger_event_mapping",
    "installed_identity",
    "runtime_binding_source",
    "runtime_binding_resolved",
    "runtime_binding_index",
]
_SCHEMA_FILES: dict[ContractKind, str] = {
    "plugin": "plugin.schema.json",
    "plugin_build_settings": "plugin-build-settings.schema.json",
    "action": "action.schema.json",
    "control": "control.schema.json",
    "trigger": "trigger.schema.json",
    "step": "step.schema.json",
    "plugin_descriptor": "plugin-descriptor.schema.json",
    "registration_manifest": "registration-manifest.schema.json",
    "resolved_execution": "resolved-execution.schema.json",
    "runtime": "runtime.schema.json",
    "provider": "provider.schema.json",
    "catalog_index": "catalog-index.schema.json",
    "catalog_source": "catalog-source.schema.json",
    "catalog_source_status": "catalog-source-status.schema.json",
    "integration": "integration.schema.json",
    "http_mapping": "http-operation.schema.json",
    "http_runner_context": "http-runner-context.schema.json",
    "credential_recipe": "credential-recipe.schema.json",
    "trigger_driver": "trigger-driver.schema.json",
    "trigger_event_mapping": "trigger-event-mapping.schema.json",
    "installed_identity": "installed-identity.schema.json",
    "runtime_binding_source": "runtime-binding-source.schema.json",
    "runtime_binding_resolved": "runtime-binding-resolved.schema.json",
    "runtime_binding_index": "runtime-binding-index.schema.json",
}
_RFC3339_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_FORMAT_CHECKER = FormatChecker()
_STEP_TARGET_KINDS = frozenset({"Action", "Control", "Trigger"})
_UTC_OFFSET_SUFFIX = "+00:00"


@_FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _is_rfc3339_datetime(value: object) -> bool:
    """Accept the RFC 3339 subset used by Syntara runtime envelopes."""

    if not isinstance(value, str) or not _RFC3339_DATETIME.fullmatch(value):
        return False
    datetime.fromisoformat(
        value.removesuffix("Z") + _UTC_OFFSET_SUFFIX if value.endswith("Z") else value
    )
    return True


@dataclass(frozen=True)
class ContractBundle:
    """An immutable in-process view of the packaged contract documents."""

    version: str
    digest: str
    documents: dict[str, dict[str, Any]]

    def validator(self, kind: ContractKind) -> Draft202012Validator:
        """Return a validator with every local schema in this bundle registered."""
        target = self.documents[_SCHEMA_FILES[kind]]
        registry = Registry().with_resources(
            (document["$id"], Resource.from_contents(document))
            for document in self.documents.values()
        )
        return Draft202012Validator(
            target,
            registry=registry,
            format_checker=_FORMAT_CHECKER,
        )


def _schema_directory() -> Any:
    packaged = files("syntara_plugin.contracts").joinpath("schemas", "v1alpha1")
    if packaged.is_dir():
        return packaged
    # Editable source installs consume the repository's language-neutral source.
    for parent in Path(__file__).resolve().parents:
        source = parent / "contracts" / "schemas" / "v1alpha1"
        if source.is_dir():
            return source
    raise RuntimeError("cannot locate packaged or repository contract schemas")


def _canonical_bytes(documents: dict[str, dict[str, Any]]) -> bytes:
    payload = {path: documents[path] for path in sorted(documents)}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def bundle_digest(documents: dict[str, dict[str, Any]]) -> str:
    """Return the stable SHA-256 digest of a complete contract bundle."""
    return "sha256:" + hashlib.sha256(_canonical_bytes(documents)).hexdigest()


def load_bundle() -> ContractBundle:
    """Load the packaged, local-only `syntara.io/v1alpha1` contract bundle."""
    directory = _schema_directory()
    documents: dict[str, dict[str, Any]] = {}
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if entry.name.endswith(".schema.json"):
            documents[entry.name] = json.loads(entry.read_text(encoding="utf-8"))
    if set(_SCHEMA_FILES.values()) - set(documents):
        raise RuntimeError("contract bundle is missing a required manifest or runtime schema")
    return ContractBundle(
        version="syntara.io/v1alpha1",
        digest=bundle_digest(documents),
        documents=documents,
    )


def validate_document(bundle: ContractBundle, kind: ContractKind, document: object) -> list[str]:
    """Return stable, path-prefixed JSON Schema validation errors."""
    errors = sorted(
        bundle.validator(kind).iter_errors(document), key=lambda error: list(error.path)
    )
    return [
        f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
        for error in errors
    ]


@dataclass(frozen=True)
class CatalogCheckpoint:
    """Last accepted immutable catalog state for one configured source.

    The caller persists this state. The contracts package only compares a
    candidate index to it; it does not fetch, verify signatures, or write data.
    """

    generation: int = 0
    index_digest: str | None = None


@dataclass(frozen=True)
class RuntimeBindingCheckpoint:
    """Last accepted runtime-binding index state for one plugin artifact."""

    generation: int = 0
    index_digest: str | None = None


@dataclass(frozen=True, order=True)
class RuntimeBindingDiagnostic:
    """Stable, non-secret reason a runtime binding or index was rejected."""

    code: str
    path: str
    message: str


def validate_runtime_binding(
    bundle: ContractBundle,
    document: Mapping[str, Any],
    *,
    resolved: bool,
    plugin_descriptor: Mapping[str, Any] | None = None,
) -> tuple[RuntimeBindingDiagnostic, ...]:
    """Validate a source or resolved binding and its links to a compiled plugin.

    This function checks document contracts and identities only. It does not
    resolve registries, verify signatures, or authorize installation.
    """

    kind: ContractKind = "runtime_binding_resolved" if resolved else "runtime_binding_source"
    schema_errors = validate_document(bundle, kind, document)
    diagnostics = [
        RuntimeBindingDiagnostic(
            code="RUNTIME_BINDING_SCHEMA_INVALID",
            path=message.partition(":")[0],
            message=message,
        )
        for message in schema_errors
    ]
    if schema_errors:
        return tuple(sorted(diagnostics))

    spec = document["spec"]
    workloads = spec["workloads"]
    for workload_id, workload in workloads.items():
        options = workload["options"]
        names = [option["name"] for option in options]
        if len(names) != len(set(names)):
            diagnostics.append(
                RuntimeBindingDiagnostic(
                    "RUNTIME_BINDING_OPTION_DUPLICATE",
                    f"spec/workloads/{workload_id}/options",
                    f"workload {workload_id!r} has duplicate option names.",
                )
            )
        if workload["default"] not in names:
            diagnostics.append(
                RuntimeBindingDiagnostic(
                    "RUNTIME_BINDING_DEFAULT_MISSING",
                    f"spec/workloads/{workload_id}/default",
                    f"default option {workload['default']!r} is not declared for workload {workload_id!r}.",
                )
            )

    if plugin_descriptor is not None:
        diagnostics.extend(
            _runtime_binding_plugin_link_errors(document, plugin_descriptor, resolved)
        )
    return tuple(sorted(diagnostics))


def _runtime_binding_plugin_link_errors(
    document: Mapping[str, Any],
    descriptor: Mapping[str, Any],
    resolved: bool,
) -> list[RuntimeBindingDiagnostic]:
    """Require the binding to match the exact custom-runner contract closure."""

    errors: list[RuntimeBindingDiagnostic] = []
    try:
        plugin_spec = descriptor["spec"]
        binding_spec = document["spec"]
        binding_workloads = binding_spec["workloads"]
        targets = plugin_spec["targets"]
    except (KeyError, TypeError):
        return [
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_PLUGIN_INVALID",
                "spec/plugin",
                "plugin descriptor is missing required compiled contract fields.",
            )
        ]

    if resolved:
        errors.extend(_resolved_runtime_binding_errors(binding_spec, plugin_spec, targets))

    target_workloads = _custom_runner_workload_ids(targets)
    if set(binding_workloads) != target_workloads:
        errors.append(
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_WORKLOAD_SET_MISMATCH",
                "spec/workloads",
                "binding workloads must exactly match workloads used by the plugin's custom-runner steps.",
            )
        )
    for workload_id in sorted(target_workloads & set(binding_workloads)):
        errors.extend(_workload_contract_errors(workload_id, binding_workloads, targets))
    return errors


def _resolved_runtime_binding_errors(
    binding_spec: Mapping[str, Any],
    plugin_spec: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
) -> list[RuntimeBindingDiagnostic]:
    errors: list[RuntimeBindingDiagnostic] = []
    plugin_reference = binding_spec["plugin"]
    contract = binding_spec["contract"]
    checks = (
        (
            contract.get("pluginRevisionId") != plugin_spec.get("plugin_revision_id"),
            "RUNTIME_BINDING_PLUGIN_REVISION_MISMATCH",
            "spec/contract/pluginRevisionId",
            "binding does not reference this plugin contract revision.",
        ),
        (
            contract.get("contractBundleDigest") != plugin_spec.get("contractBundleDigest"),
            "RUNTIME_BINDING_CONTRACT_BUNDLE_MISMATCH",
            "spec/contract/contractBundleDigest",
            "binding contract bundle digest differs from the plugin descriptor.",
        ),
        (
            contract.get("stepRevisionIds") != _custom_runner_step_revision_ids(targets),
            "RUNTIME_BINDING_STEP_SET_MISMATCH",
            "spec/contract/stepRevisionIds",
            "binding step revision set does not match the plugin's custom-runner steps.",
        ),
    )
    errors.extend(_diagnostics_for_failed_checks(checks))
    if not isinstance(plugin_reference, Mapping) or not isinstance(
        plugin_reference.get("digest"), str
    ):
        errors.append(
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_PLUGIN_REFERENCE_INVALID",
                "spec/plugin",
                "resolved binding must identify the plugin artifact by repository and digest.",
            )
        )
    return errors


def _diagnostics_for_failed_checks(
    checks: Sequence[tuple[bool, str, str, str]],
) -> list[RuntimeBindingDiagnostic]:
    return [
        RuntimeBindingDiagnostic(code, path, message)
        for failed, code, path, message in checks
        if failed
    ]


def _custom_runner_targets(
    targets: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return [
        target
        for target in targets
        if target.get("kind") in _STEP_TARGET_KINDS
        and isinstance(target.get("runtime"), Mapping)
        and target["runtime"].get("kind") == "custom-runner"
    ]


def _custom_runner_step_revision_ids(targets: Sequence[Mapping[str, Any]]) -> list[str]:
    return sorted(target["step_revision_id"] for target in _custom_runner_targets(targets))


def _custom_runner_workload_ids(targets: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        str(target["runtime"]["workload"])
        for target in _custom_runner_targets(targets)
        if isinstance(target["runtime"].get("workload"), str)
    }


def _workload_contract_errors(
    workload_id: str,
    binding_workloads: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
) -> list[RuntimeBindingDiagnostic]:
    errors: list[RuntimeBindingDiagnostic] = []
    if _workload_needs_entry_point(workload_id, targets):
        errors.append(
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_ENTRY_POINT_MISSING",
                f"spec/workloads/{workload_id}",
                "a custom-runner step assigned to this workload has no entry point.",
            )
        )
    options = binding_workloads[workload_id]["options"]
    if not {option["name"] for option in options}:
        errors.append(
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_OPTIONS_EMPTY",
                f"spec/workloads/{workload_id}/options",
                "every declared workload must have at least one image option.",
            )
        )
    return errors


def _workload_needs_entry_point(workload_id: str, targets: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        target["runtime"].get("workload") == workload_id
        and not isinstance(target["runtime"].get("entryPoint"), str)
        for target in _custom_runner_targets(targets)
    )


def validate_runtime_binding_index(
    bundle: ContractBundle,
    document: Mapping[str, Any],
    *,
    plugin_digest: str,
    now: datetime,
    checkpoint: RuntimeBindingCheckpoint | None = None,
    allow_expired_predecessor: bool = False,
) -> tuple[RuntimeBindingDiagnostic, ...]:
    """Validate an index against plugin identity, expiry, and replay state.

    Signature, repository authorization, OCI subject verification, and
    persistence remain responsibilities of the caller. Set
    ``allow_expired_predecessor`` only when validating the current index as the
    base for publishing a fresh successor. Consumers must use the default,
    which rejects expired indexes.
    """

    schema_errors = validate_document(bundle, "runtime_binding_index", document)
    if schema_errors:
        return tuple(
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_INDEX_SCHEMA_INVALID",
                message.partition(":")[0],
                message,
            )
            for message in schema_errors
        )
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    checkpoint = checkpoint or RuntimeBindingCheckpoint()
    metadata = document["metadata"]
    diagnostics: list[RuntimeBindingDiagnostic] = []
    if metadata["pluginDigest"] != plugin_digest:
        diagnostics.append(
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_INDEX_PLUGIN_MISMATCH",
                "metadata/pluginDigest",
                "runtime binding index does not identify the installed plugin artifact.",
            )
        )
    if metadata["generation"] != checkpoint.generation + 1:
        diagnostics.append(
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_INDEX_GENERATION_INVALID",
                "metadata/generation",
                "runtime binding index generation must immediately follow the accepted generation.",
            )
        )
    previous = metadata.get("previousIndexDigest")
    if previous != checkpoint.index_digest:
        diagnostics.append(
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_INDEX_PREDECESSOR_MISMATCH",
                "metadata/previousIndexDigest",
                "runtime binding index does not extend the accepted index digest.",
            )
        )
    issued_at = datetime.fromisoformat(metadata["issuedAt"].replace("Z", _UTC_OFFSET_SUFFIX))
    expires_at = datetime.fromisoformat(metadata["expiresAt"].replace("Z", _UTC_OFFSET_SUFFIX))
    if (
        issued_at > now
        or (expires_at <= now and not allow_expired_predecessor)
        or expires_at <= issued_at
    ):
        diagnostics.append(
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_INDEX_TIME_INVALID",
                "metadata",
                "runtime binding index issue and expiry times are invalid for the current time.",
            )
        )
    seen: set[str] = set()
    for position, entry in enumerate(document["spec"]["entries"]):
        digest = entry["bindingDigest"]
        if digest in seen:
            diagnostics.append(
                RuntimeBindingDiagnostic(
                    "RUNTIME_BINDING_INDEX_DUPLICATE_ENTRY",
                    f"spec/entries/{position}/bindingDigest",
                    "runtime binding index contains the same binding digest more than once.",
                )
            )
        seen.add(digest)
    return tuple(sorted(diagnostics))


@dataclass(frozen=True, order=True)
class CatalogDiagnostic:
    """Stable, non-secret reason returned when a catalog candidate is rejected."""

    code: str
    path: str
    message: str


def validate_catalog_index(
    bundle: ContractBundle,
    document: Mapping[str, Any],
    *,
    source_id: str,
    now: datetime,
    checkpoint: CatalogCheckpoint | None = None,
) -> tuple[CatalogDiagnostic, ...]:
    """Validate a catalog index against one source's replay checkpoint.

    This is intentionally a pure control-plane contract check. OCI resolution,
    signature verification, repository-scope checks, and persistence are owned
    by the catalog service that calls it.
    """

    checkpoint = checkpoint or CatalogCheckpoint()
    schema_errors = validate_document(bundle, "catalog_index", document)
    if schema_errors:
        return tuple(
            CatalogDiagnostic(
                code="CATALOG_INDEX_SCHEMA_INVALID",
                path=message.partition(":")[0],
                message=message,
            )
            for message in schema_errors
        )
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    metadata = document["metadata"]
    specification = document["spec"]
    diagnostics: list[CatalogDiagnostic] = []
    if metadata["sourceId"] != source_id:
        diagnostics.append(
            CatalogDiagnostic(
                code="CATALOG_INDEX_SOURCE_MISMATCH",
                path="metadata/sourceId",
                message="Catalog index sourceId does not match the configured source.",
            )
        )
    if specification["contractVersion"] != bundle.version:
        diagnostics.append(
            CatalogDiagnostic(
                code="CATALOG_INDEX_CONTRACT_UNSUPPORTED",
                path="spec/contractVersion",
                message=f"Catalog index contract must be {bundle.version!r}.",
            )
        )

    issued_at = _parse_rfc3339(metadata["issuedAt"])
    expires_at = _parse_rfc3339(metadata["expiresAt"])
    if expires_at <= issued_at:
        diagnostics.append(
            CatalogDiagnostic(
                code="CATALOG_INDEX_TIME_WINDOW_INVALID",
                path="metadata/expiresAt",
                message="Catalog index expiresAt must be later than issuedAt.",
            )
        )
    if expires_at <= now:
        diagnostics.append(
            CatalogDiagnostic(
                code="CATALOG_INDEX_EXPIRED",
                path="metadata/expiresAt",
                message="Catalog index is expired at the supplied reconciliation time.",
            )
        )
    if metadata["generation"] <= checkpoint.generation:
        diagnostics.append(
            CatalogDiagnostic(
                code="CATALOG_INDEX_GENERATION_REPLAY",
                path="metadata/generation",
                message="Catalog index generation must be greater than the accepted generation.",
            )
        )
    if (
        checkpoint.index_digest is not None
        and metadata.get("previousIndexDigest") != checkpoint.index_digest
    ):
        diagnostics.append(
            CatalogDiagnostic(
                code="CATALOG_INDEX_PREDECESSOR_MISMATCH",
                path="metadata/previousIndexDigest",
                message="Catalog index predecessor does not match the accepted index digest.",
            )
        )

    seen_identities: set[tuple[str, str, str]] = set()
    for index, entry in enumerate(specification["entries"]):
        identity = (entry["namespace"], entry["name"], entry["version"])
        if identity in seen_identities:
            diagnostics.append(
                CatalogDiagnostic(
                    code="CATALOG_ENTRY_IDENTITY_DUPLICATE",
                    path=f"spec/entries/{index}",
                    message="Catalog index entries must not repeat namespace, name, and version.",
                )
            )
        seen_identities.add(identity)
    return tuple(sorted(diagnostics))


def _parse_rfc3339(value: str) -> datetime:
    """Parse a value already checked by the bundle's strict date-time format."""

    return datetime.fromisoformat(
        value.removesuffix("Z") + _UTC_OFFSET_SUFFIX if value.endswith("Z") else value
    )
