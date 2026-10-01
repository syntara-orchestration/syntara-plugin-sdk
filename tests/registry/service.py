"""Manifest and OCI registration handlers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import yaml
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
from syntara_tools.compiler import compile_manifest_data, validate_manifest

from tests.registry.models import StepCategory, StepType

OCI_ARTIFACT_TYPE = "application/vnd.syntara.step.manifest.v1+yaml"
OCI_MANIFEST_ANNOTATION = "org.syntara.step.manifest"


class RegistryService:
    """Persist validated step definitions through either registration path.

    The OCI path deliberately accepts an already-fetched OCI manifest. Keeping
    registry I/O outside this service makes annotation inspection a bounded,
    allocation-light operation suitable for the image admission path.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def register_manifest(
        self,
        manifest: Mapping[str, Any],
        *,
        plugin_namespace: str,
        plugin_version: str,
        image_ref: str | None = None,
    ) -> StepType:
        """Register a raw YAML/JSON manifest from the Git or REST path."""

        descriptor = compile_manifest_data(dict(manifest))
        metadata = descriptor["metadata"]
        spec = descriptor["spec"]
        # image_ref is where the plugin artifact was published. It is a
        # different thing from spec.execution.image, the runtime the step runs
        # in, so neither is derived from or written over the other.

        name = metadata["name"]
        namespace = plugin_namespace
        # The legacy model calls this column ``version``; it stores the parent
        # plugin version and is never sourced from step metadata.
        version = plugin_version
        row = self.session.exec(
            select(StepType)
            .where(StepType.namespace == namespace)
            .where(StepType.name == name)
            .where(StepType.version == version)
        ).one_or_none()
        if row is None:
            row = StepType(
                namespace=namespace, name=name, version=version, descriptor=descriptor
            )
            self.session.add(row)

        row.display_name = metadata["displayName"]
        row.category = StepCategory(spec["category"])
        row.image_ref = image_ref
        row.descriptor = descriptor
        row.enabled = True
        row.updated_at = datetime.now(UTC)

        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            raise
        self.session.refresh(row)
        return row

    def register_manifest_payload(
        self,
        payload: str | bytes,
        *,
        plugin_namespace: str,
        plugin_version: str,
        image_ref: str | None = None,
    ) -> StepType:
        """Parse and register a raw Git or REST YAML/JSON request body."""

        try:
            manifest = yaml.safe_load(payload)
        except yaml.YAMLError as exc:
            raise ValueError(f"manifest payload is not valid YAML: {exc}") from exc
        if not isinstance(manifest, Mapping):
            raise TypeError("manifest payload must contain a mapping")
        return self.register_manifest(
            manifest,
            plugin_namespace=plugin_namespace,
            plugin_version=plugin_version,
            image_ref=image_ref,
        )

    def register_oci_manifest(
        self,
        image_ref: str,
        oci_manifest: Mapping[str, Any],
        *,
        plugin_namespace: str,
        plugin_version: str,
    ) -> StepType:
        """Inspect OCI artifact metadata and register its embedded manifest."""

        if oci_manifest.get("artifactType") != OCI_ARTIFACT_TYPE:
            raise ValueError("OCI artifact is not a step manifest artifact")
        annotations = oci_manifest.get("annotations")
        if not isinstance(annotations, Mapping):
            raise TypeError("OCI step artifact is missing annotations")
        raw_manifest = annotations.get(OCI_MANIFEST_ANNOTATION)
        if not isinstance(raw_manifest, str) or not raw_manifest.strip():
            raise ValueError(f"OCI annotation {OCI_MANIFEST_ANNOTATION!r} is missing")
        manifest = yaml.safe_load(raw_manifest)
        if not isinstance(manifest, Mapping):
            raise TypeError("OCI step annotation does not contain a manifest mapping")
        return self.register_manifest(
            manifest,
            plugin_namespace=plugin_namespace,
            plugin_version=plugin_version,
            image_ref=image_ref,
        )

    def register_oci_headers(
        self,
        image_ref: str,
        headers: Mapping[str, Any],
        *,
        plugin_namespace: str,
        plugin_version: str,
    ) -> StepType:
        """Compatibility entry point for callers naming OCI metadata headers."""

        return self.register_oci_manifest(
            image_ref,
            headers,
            plugin_namespace=plugin_namespace,
            plugin_version=plugin_version,
        )


def validate_oci_manifest(oci_manifest: Mapping[str, Any]) -> list[str]:
    """Perform the fast, network-free OCI metadata validation step."""

    if oci_manifest.get("artifactType") != OCI_ARTIFACT_TYPE:
        return ["artifactType must identify a step manifest artifact"]
    annotations = oci_manifest.get("annotations")
    if not isinstance(annotations, Mapping) or not annotations.get(OCI_MANIFEST_ANNOTATION):
        return [f"annotations.{OCI_MANIFEST_ANNOTATION} is required"]
    try:
        manifest = yaml.safe_load(annotations[OCI_MANIFEST_ANNOTATION])
    except yaml.YAMLError as exc:
        return [f"embedded manifest is invalid YAML: {exc}"]
    if not isinstance(manifest, dict):
        return ["embedded manifest must be a mapping"]
    return validate_manifest(manifest)
