"""RuntimeBinding index publication and predecessor trust tests."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest
from syntara_plugin.sdk import (
    CatalogIndexPublicationError,
    CatalogIndexPublicationTarget,
    OciRegistryResponse,
    RuntimeBindingIndexPublisher,
    build_runtime_binding_index_artifact,
    next_runtime_binding_index,
)
from syntara_plugin.sdk.registry import _is_strong_etag


class _Transport:
    def __init__(self, responses: list[OciRegistryResponse]) -> None:
        self._responses = deque(responses)
        self.requests: list[tuple[str, str, Mapping[str, str], bytes | None]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        content: bytes | None = None,
    ) -> OciRegistryResponse:
        self.requests.append((method, url, dict(headers), content))
        return self._responses.popleft()


def _target() -> CatalogIndexPublicationTarget:
    return CatalogIndexPublicationTarget(
        registry_origin="https://registry.example.test",
        repository="acme/plugins/issue-tracker",
        channel="runtime-binding-sha256-" + "a" * 64,
    )


def _index(now: datetime, *, current=None, current_digest=None, release: str = "1.0.0"):
    document, idempotent = next_runtime_binding_index(
        current=current,
        current_manifest_digest=current_digest,
        plugin_digest="sha256:" + "a" * 64,
        binding_repository="acme/plugins/issue-tracker",
        binding_digest="sha256:" + "b" * 64,
        expires_in_hours=24,
        release=release,
        now=now,
    )
    return document, idempotent, build_runtime_binding_index_artifact(document)


def _upload_responses(artifact, *, manifest_status: int = 201) -> list[OciRegistryResponse]:
    responses: list[OciRegistryResponse] = []
    for number, blob in enumerate((artifact.config, artifact.document), start=1):
        upload = f"/v2/acme/plugins/issue-tracker/blobs/uploads/{number}"
        responses.extend(
            [
                OciRegistryResponse(404, {}),
                OciRegistryResponse(202, {"Location": upload}),
                OciRegistryResponse(201, {"Docker-Content-Digest": blob.descriptor.digest}),
            ]
        )
    responses.append(
        OciRegistryResponse(
            manifest_status,
            {"Docker-Content-Digest": artifact.digest},
        )
    )
    return responses


def _read_responses(artifact, *, etag: str = '"index-v1"') -> list[OciRegistryResponse]:
    return [
        OciRegistryResponse(
            200,
            {"Docker-Content-Digest": artifact.digest, "ETag": etag},
            artifact.manifest.content,
        ),
        OciRegistryResponse(200, {}, artifact.config.content),
        OciRegistryResponse(200, {}, artifact.document.content),
    ]


def test_first_runtime_binding_index_publication_uses_create_precondition() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    _, _, artifact = _index(now)
    transport = _Transport(_upload_responses(artifact))

    RuntimeBindingIndexPublisher(_target(), transport=transport).publish(
        artifact,
        expected_current_digest=None,
        expected_current_etag=None,
    )

    assert transport.requests[-1][2]["If-None-Match"] == "*"


def test_successor_runtime_binding_index_uses_strong_etag_and_stops_on_conflict() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    first_document, _, first_artifact = _index(now - timedelta(minutes=1))
    successor_document, _, successor = _index(
        now,
        current=first_document,
        current_digest=first_artifact.digest,
        release="1.0.1",
    )
    assert successor_document["metadata"]["previousIndexDigest"] == first_artifact.digest
    transport = _Transport(_upload_responses(successor, manifest_status=412))
    publisher = RuntimeBindingIndexPublisher(_target(), transport=transport)

    with pytest.raises(CatalogIndexPublicationError, match="RUNTIME_BINDING_INDEX_CONFLICT"):
        publisher.publish(
            successor,
            expected_current_digest=first_artifact.digest,
            expected_current_etag='"index-v1"',
        )

    assert transport.requests[-1][2]["If-Match"] == '"index-v1"'


@pytest.mark.parametrize("etag", [None, 'W/"index-v1"', '"invalid\x7f"', '"invalidĀ"'])
def test_successor_rejects_missing_weak_or_invalid_etag_before_upload(etag: str | None) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    first_document, _, first_artifact = _index(now - timedelta(minutes=1))
    _, _, successor = _index(
        now,
        current=first_document,
        current_digest=first_artifact.digest,
    )
    transport = _Transport([])
    publisher = RuntimeBindingIndexPublisher(_target(), transport=transport)

    with pytest.raises(
        CatalogIndexPublicationError,
        match="RUNTIME_BINDING_INDEX_CONDITIONAL_UPDATE_UNSUPPORTED",
    ):
        publisher.publish(
            successor,
            expected_current_digest=first_artifact.digest,
            expected_current_etag=etag,
        )

    assert transport.requests == []


def test_runtime_binding_index_read_requires_trust_policy_or_explicit_dev_override() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    _, _, artifact = _index(now)
    untrusted_transport = _Transport(_read_responses(artifact))
    untrusted_publisher = RuntimeBindingIndexPublisher(_target(), transport=untrusted_transport)

    with pytest.raises(
        CatalogIndexPublicationError,
        match="RUNTIME_BINDING_INDEX_TRUST_POLICY_REQUIRED",
    ):
        untrusted_publisher.read_current()

    verified: list[str] = []
    trusted_transport = _Transport(_read_responses(artifact))
    snapshot = RuntimeBindingIndexPublisher(
        _target(),
        transport=trusted_transport,
        verify_current_index=verified.append,
    ).read_current()

    assert snapshot is not None
    assert verified == [artifact.digest]

    development_transport = _Transport(_read_responses(artifact))
    development_snapshot = RuntimeBindingIndexPublisher(
        _target(),
        transport=development_transport,
        allow_unverified_current=True,
    ).read_current()
    assert development_snapshot is not None


def test_binding_index_successor_renews_expired_predecessor_and_idempotency() -> None:
    issued = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    current, _, current_artifact = _index(issued)
    unchanged, idempotent = next_runtime_binding_index(
        current=current,
        current_manifest_digest=current_artifact.digest,
        plugin_digest="sha256:" + "a" * 64,
        binding_repository="acme/plugins/issue-tracker",
        binding_digest="sha256:" + "b" * 64,
        expires_in_hours=24,
        release="1.0.0",
        now=issued + timedelta(hours=1),
    )
    assert idempotent is True
    assert unchanged == current

    renewed, idempotent = next_runtime_binding_index(
        current=current,
        current_manifest_digest=current_artifact.digest,
        plugin_digest="sha256:" + "a" * 64,
        binding_repository="acme/plugins/issue-tracker",
        binding_digest="sha256:" + "b" * 64,
        expires_in_hours=24,
        release="1.0.0",
        now=issued + timedelta(hours=25),
    )
    assert idempotent is False
    assert renewed["metadata"]["generation"] == 2
    assert renewed["metadata"]["previousIndexDigest"] == current_artifact.digest


@pytest.mark.parametrize("etag", ['""', '"opaque-tag"', '"latin-1-\u00e9"'])
def test_strong_etag_accepts_rfc_valid_opaque_tags(etag: str) -> None:
    assert _is_strong_etag(etag) is True
