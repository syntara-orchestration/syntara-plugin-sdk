"""Normative, versioned Syntara plugin contracts."""

from .bundle import (
    CatalogCheckpoint,
    CatalogDiagnostic,
    ContractBundle,
    bundle_digest,
    load_bundle,
    validate_catalog_index,
    validate_document,
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

__all__ = [
    "CatalogCheckpoint",
    "CatalogDiagnostic",
    "CanonicalJsonError",
    "ContractBundle",
    "FixtureBundle",
    "bundle_digest",
    "canonical_json_bytes",
    "canonical_json_digest",
    "container_workload_request_digest",
    "fixture_content_digest",
    "load_bundle",
    "load_fixture_bundle",
    "materialized_schemas",
    "schema_digests",
    "validate_plugin_descriptor_semantics",
    "is_digest_image_reference",
    "validate_catalog_index",
    "validate_document",
]
