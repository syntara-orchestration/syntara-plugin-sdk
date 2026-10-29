"""Load and validate the self-contained v1alpha1 contract bundle."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
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
}
_RFC3339_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_FORMAT_CHECKER = FormatChecker()


@_FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _is_rfc3339_datetime(value: object) -> bool:
    """Accept the RFC 3339 subset used by Syntara runtime envelopes."""

    if not isinstance(value, str) or not _RFC3339_DATETIME.fullmatch(value):
        return False
    datetime.fromisoformat(value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value)
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
        value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
    )
