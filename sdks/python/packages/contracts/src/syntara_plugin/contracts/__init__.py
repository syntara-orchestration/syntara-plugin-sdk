"""Normative, versioned Syntara plugin contracts."""

from .bundle import (
    CatalogCheckpoint,
    CatalogDiagnostic,
    ContractBundle,
    RuntimeBindingCheckpoint,
    RuntimeBindingDiagnostic,
    SchemaDocumentError,
    build_validator,
    bundle_digest,
    format_checker,
    load_bundle,
    validate_authored_schema,
    validate_catalog_index,
    validate_declared_error_schema,
    validate_document,
    validate_runtime_binding,
    validate_runtime_binding_index,
)
from .canonical import (
    CanonicalJsonError,
    canonical_json_bytes,
    canonical_json_digest,
    container_workload_request_digest,
)
from .descriptor import (
    materialized_schemas,
    schema_digests,
    validate_plugin_descriptor_semantics,
)
from .fixtures import FixtureBundle, fixture_content_digest, load_fixture_bundle
from .image import is_digest_image_reference
from .revisions import (
    STEP_KINDS,
    plugin_revision_id,
    registration_manifest,
    revision_errors,
    step_revision_id,
    validate_registration_manifest,
)

__all__ = [
    "CatalogCheckpoint",
    "CatalogDiagnostic",
    "CanonicalJsonError",
    "ContractBundle",
    "RuntimeBindingCheckpoint",
    "RuntimeBindingDiagnostic",
    "FixtureBundle",
    "SchemaDocumentError",
    "bundle_digest",
    "build_validator",
    "canonical_json_bytes",
    "canonical_json_digest",
    "container_workload_request_digest",
    "fixture_content_digest",
    "format_checker",
    "load_bundle",
    "load_fixture_bundle",
    "materialized_schemas",
    "schema_digests",
    "STEP_KINDS",
    "plugin_revision_id",
    "registration_manifest",
    "revision_errors",
    "step_revision_id",
    "validate_authored_schema",
    "validate_registration_manifest",
    "validate_plugin_descriptor_semantics",
    "is_digest_image_reference",
    "validate_catalog_index",
    "validate_declared_error_schema",
    "validate_runtime_binding",
    "validate_runtime_binding_index",
    "validate_document",
]
