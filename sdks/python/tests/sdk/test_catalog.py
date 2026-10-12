from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from syntara_plugin.sdk import (
    OCI_CATALOG_INDEX_ARTIFACT_TYPE,
    OCI_CATALOG_INDEX_CONFIG_MEDIA_TYPE,
    OCI_CATALOG_INDEX_CONTENT_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    CatalogIndexArtifactBuildError,
    CatalogIndexUpdateError,
    build_catalog_index_artifact,
    merge_catalog_entry,
    recover_catalog_entry,
)


def _index() -> dict[str, object]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "CatalogIndex",
        "metadata": {
            "sourceId": "local-quay",
            "generation": 42,
            "issuedAt": "2026-10-06T12:00:00Z",
            "expiresAt": "2026-10-13T12:00:00Z",
            "previousIndexDigest": "sha256:" + "a" * 64,
        },
        "spec": {
            "contractVersion": "syntara.io/v1alpha1",
            "entries": [
                {
                    "namespace": "syntara",
                    "name": "github",
                    "version": "1.0.0",
                    "artifactRepository": "syntara/plugins/github",
                    "artifactDigest": "sha256:" + "1" * 64,
                    "documentationDigest": "sha256:" + "2" * 64,
                    "runtimeOwnerships": ["platform-runner"],
                },
                {
                    "namespace": "syntara",
                    "name": "aap",
                    "version": "1.0.0",
                    "artifactRepository": "syntara/plugins/aap",
                    "artifactDigest": "sha256:" + "3" * 64,
                    "documentationDigest": "sha256:" + "4" * 64,
                    "runtimeOwnerships": ["custom-workload"],
                },
            ],
        },
    }


def test_catalog_index_artifact_is_deterministic_and_has_a_complete_oci_envelope() -> None:
    index = _index()

    first = build_catalog_index_artifact(index)
    second = build_catalog_index_artifact(deepcopy(index))

    assert first.digest == second.digest
    assert first.manifest.content == second.manifest.content
    assert first.catalog_index.content == second.catalog_index.content
    assert first.blobs == {
        first.config.descriptor.digest: first.config,
        first.catalog_index.descriptor.digest: first.catalog_index,
        first.manifest.descriptor.digest: first.manifest,
    }

    manifest = json.loads(first.manifest.content)
    assert manifest == {
        "annotations": {
            "org.opencontainers.image.title": "catalog-index/local-quay",
            "org.opencontainers.image.version": "42",
        },
        "artifactType": OCI_CATALOG_INDEX_ARTIFACT_TYPE,
        "config": first.config.descriptor.as_dict(),
        "layers": [first.catalog_index.descriptor.as_dict()],
        "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
        "schemaVersion": 2,
    }
    assert first.config.descriptor.media_type == OCI_CATALOG_INDEX_CONFIG_MEDIA_TYPE
    assert first.catalog_index.descriptor.media_type == OCI_CATALOG_INDEX_CONTENT_MEDIA_TYPE
    assert json.loads(first.catalog_index.content) == index

    config = json.loads(first.config.content)
    assert config["catalogIndexDigest"] == first.catalog_index.descriptor.digest
    assert config["entryCount"] == 2
    assert config["generation"] == 42
    assert config["sourceId"] == "local-quay"
    assert first.catalog_index.descriptor.digest == (
        "sha256:" + sha256(first.catalog_index.content).hexdigest()
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda document: document["spec"].update({"entries": []}),
            "spec/entries",
        ),
        (
            lambda document: document["spec"].update(
                {"entries": [document["spec"]["entries"][0]] * 2}
            ),
            "CATALOG_ENTRY_IDENTITY_DUPLICATE",
        ),
        (
            lambda document: document["metadata"].update(
                {"expiresAt": document["metadata"]["issuedAt"]}
            ),
            "CATALOG_INDEX_TIME_WINDOW_INVALID",
        ),
        (
            lambda document: document["spec"]["entries"][0].update(
                {
                    "runtimeOwnership": document["spec"]["entries"][0].pop(
                        "runtimeOwnerships"
                    )
                }
            ),
            "runtimeOwnerships",
        ),
    ],
)
def test_catalog_index_artifact_rejects_invalid_catalogs(mutate, message: str) -> None:
    index = _index()
    mutate(index)

    with pytest.raises(CatalogIndexArtifactBuildError, match=message):
        build_catalog_index_artifact(index)


def test_merge_catalog_entry_creates_then_extends_an_append_only_index() -> None:
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    github = {
        "namespace": "syntara",
        "name": "github",
        "version": "0.1.0",
        "artifactRepository": "syntara/plugins/github",
        "artifactDigest": "sha256:" + "a" * 64,
        "documentationDigest": "sha256:" + "b" * 64,
        "runtimeOwnerships": ["platform-runner"],
    }
    initial, idempotent = merge_catalog_entry(
        current=None,
        current_manifest_digest=None,
        entry=github,
        source_id="local-quay",
        issued_at=now,
        expires_at=now + timedelta(days=7),
    )

    assert idempotent is False
    assert initial["metadata"] == {
        "sourceId": "local-quay",
        "generation": 1,
        "issuedAt": "2026-10-08T12:00:00Z",
        "expiresAt": "2026-10-15T12:00:00Z",
    }

    echo = {**github, "name": "local-echo", "runtimeOwnerships": ["custom-workload"]}
    extended, idempotent = merge_catalog_entry(
        current=initial,
        current_manifest_digest="sha256:" + "c" * 64,
        entry=echo,
        source_id="local-quay",
        issued_at=now + timedelta(hours=1),
        expires_at=now + timedelta(days=7, hours=1),
    )

    assert idempotent is False
    assert extended["metadata"]["generation"] == 2
    assert extended["metadata"]["previousIndexDigest"] == "sha256:" + "c" * 64
    assert [entry["name"] for entry in extended["spec"]["entries"]] == ["github", "local-echo"]


def test_merge_catalog_entry_is_idempotent_but_rejects_conflicting_release() -> None:
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    entry = {
        "namespace": "syntara",
        "name": "github",
        "version": "0.1.0",
        "artifactRepository": "syntara/plugins/github",
        "artifactDigest": "sha256:" + "a" * 64,
        "documentationDigest": "sha256:" + "b" * 64,
        "runtimeOwnerships": ["platform-runner"],
    }
    current, _ = merge_catalog_entry(
        current=None,
        current_manifest_digest=None,
        entry=entry,
        source_id="local-quay",
        issued_at=now,
        expires_at=now + timedelta(days=7),
    )

    unchanged, idempotent = merge_catalog_entry(
        current=current,
        current_manifest_digest="sha256:" + "c" * 64,
        entry=entry,
        source_id="local-quay",
        issued_at=now + timedelta(hours=1),
        expires_at=now + timedelta(days=7, hours=1),
    )
    assert idempotent is True
    assert unchanged == current

    with pytest.raises(CatalogIndexUpdateError, match="publish a new plugin version"):
        merge_catalog_entry(
            current=current,
            current_manifest_digest="sha256:" + "c" * 64,
            entry={**entry, "artifactDigest": "sha256:" + "d" * 64},
            source_id="local-quay",
            issued_at=now + timedelta(hours=1),
            expires_at=now + timedelta(days=7, hours=1),
        )


def test_merge_catalog_entry_can_explicitly_renew_an_unchanged_release() -> None:
    """Renewal advances the trusted chain without changing a released artifact."""
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    entry = {
        "namespace": "syntara",
        "name": "github",
        "version": "0.1.0",
        "artifactRepository": "syntara/plugins/github",
        "artifactDigest": "sha256:" + "a" * 64,
        "documentationDigest": "sha256:" + "b" * 64,
        "runtimeOwnerships": ["platform-runner"],
    }
    current, _ = merge_catalog_entry(
        current=None,
        current_manifest_digest=None,
        entry=entry,
        source_id="local-quay",
        issued_at=now,
        expires_at=now + timedelta(days=7),
    )

    renewed, idempotent = merge_catalog_entry(
        current=current,
        current_manifest_digest="sha256:" + "c" * 64,
        entry=entry,
        source_id="local-quay",
        issued_at=now + timedelta(days=6),
        expires_at=now + timedelta(days=13),
        renew=True,
    )

    assert idempotent is False
    assert renewed["metadata"]["generation"] == 2
    assert renewed["metadata"]["previousIndexDigest"] == "sha256:" + "c" * 64
    assert renewed["spec"]["entries"] == current["spec"]["entries"]


def test_recover_catalog_entry_continues_the_accepted_chain_and_preserves_entries() -> None:
    """Recovery ignores a lost mutable tag and advances from the accepted revision."""
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    previous = _index()
    local_echo = {
        "namespace": "syntara",
        "name": "local-echo",
        "version": "0.1.0",
        "artifactRepository": "syntara/plugins/local-echo",
        "artifactDigest": "sha256:" + "c" * 64,
        "documentationDigest": "sha256:" + "d" * 64,
        "runtimeOwnerships": ["custom-workload", "platform-runner"],
    }

    recovered = recover_catalog_entry(
        previous=previous,
        previous_manifest_digest="sha256:" + "e" * 64,
        entry=local_echo,
        source_id="local-quay",
        issued_at=now,
        expires_at=now + timedelta(days=7),
    )

    assert recovered["metadata"]["generation"] == 43
    assert recovered["metadata"]["previousIndexDigest"] == "sha256:" + "e" * 64
    assert [entry["name"] for entry in recovered["spec"]["entries"]] == [
        "aap",
        "github",
        "local-echo",
    ]


def test_recover_catalog_entry_writes_a_successor_when_the_release_is_already_present() -> None:
    """Restoring a deleted tag must progress the chain even without a new entry."""
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    previous = _index()
    recovered = recover_catalog_entry(
        previous=previous,
        previous_manifest_digest="sha256:" + "e" * 64,
        entry=previous["spec"]["entries"][0],
        source_id="local-quay",
        issued_at=now,
        expires_at=now + timedelta(days=7),
    )

    assert recovered["metadata"]["generation"] == 43
    assert [entry["name"] for entry in recovered["spec"]["entries"]] == ["aap", "github"]
