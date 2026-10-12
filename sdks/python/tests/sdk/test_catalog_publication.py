"""Contract tests for bounded catalog-index OCI publication."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qs, urlparse

import pytest

from syntara_plugin.sdk import (
    CatalogIndexPublicationError,
    CatalogIndexPublicationTarget,
    CatalogIndexPublisher,
    BuildRequest,
    OciRegistryResponse,
    PluginArtifactPublicationTarget,
    PluginArtifactPublisher,
    RegistryCredentials,
    build_catalog_index_artifact,
    build_plugin_artifact,
    compile_workspace,
)


class _Transport:
    """Scripted transport that records publication requests without a registry."""

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


def _artifact():
    now = datetime.now(UTC).replace(microsecond=0)
    return build_catalog_index_artifact(
        {
            "apiVersion": "syntara.io/v1alpha1",
            "kind": "CatalogIndex",
            "metadata": {
                "sourceId": "local-poc",
                "generation": 1,
                "issuedAt": now.isoformat().replace("+00:00", "Z"),
                "expiresAt": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            },
            "spec": {
                "contractVersion": "syntara.io/v1alpha1",
                "entries": [
                    {
                        "namespace": "syntara",
                        "name": "github",
                        "version": "0.1.0",
                        "artifactRepository": "syntara/plugins/github",
                        "artifactDigest": "sha256:" + "a" * 64,
                        "documentationDigest": "sha256:" + "b" * 64,
                        "runtimeOwnerships": ["platform-runner"],
                    }
                ],
            },
        }
    )


def _target() -> CatalogIndexPublicationTarget:
    return CatalogIndexPublicationTarget(
        registry_origin="http://localhost:8443",
        repository="syntara/catalog-index",
        channel="stable",
        allow_insecure_loopback_http=True,
    )


def _plugin_artifact(tmp_path: Path):
    root = tmp_path / "plugin.yaml"
    action = tmp_path / "steps/create-issue/manifest.yaml"
    action.parent.mkdir(parents=True)
    root.write_text(
        """apiVersion: syntara.io/v1alpha1
kind: Plugin
metadata:
  namespace: syntara
  name: github
  version: 0.1.0
  displayName: GitHub
  description: GitHub workflow actions.
spec:
  targets:
    - steps/create-issue/manifest.yaml
"""
    )
    action.write_text(
        """apiVersion: syntara.io/v1alpha1
kind: Action
metadata:
  name: create-issue
  displayName: Create issue
  description: Create one GitHub issue.
spec:
  runtime:
    kind: platform-runner
    driver: http.v1
    operation:
      method: GET
      path: /resources
  input:
    kind: inline
    value:
      type: object
  output:
    kind: inline
    value:
      type: object
  error:
    kind: inline
    value:
      type: object
"""
    )
    return build_plugin_artifact(compile_workspace(BuildRequest(root)))


def test_publisher_uploads_immutable_blobs_before_advancing_the_configured_channel() -> None:
    artifact = _artifact()
    upload_one = "/v2/syntara/catalog-index/blobs/uploads/upload-one"
    upload_two = "/v2/syntara/catalog-index/blobs/uploads/upload-two"
    transport = _Transport(
        [
            OciRegistryResponse(
                401,
                {
                    "WWW-Authenticate": (
                        'Bearer realm="http://localhost:8443/v2/auth",service="localhost:8443"'
                    )
                },
            ),
            OciRegistryResponse(200, {}, b'{"token":"local-token"}'),
            OciRegistryResponse(404, {}),
            OciRegistryResponse(202, {"Location": upload_one}),
            OciRegistryResponse(201, {"Docker-Content-Digest": artifact.config.descriptor.digest}),
            OciRegistryResponse(404, {}),
            OciRegistryResponse(202, {"Location": upload_two}),
            OciRegistryResponse(
                201, {"Docker-Content-Digest": artifact.catalog_index.descriptor.digest}
            ),
            OciRegistryResponse(201, {"Docker-Content-Digest": artifact.digest}),
        ]
    )

    publication = CatalogIndexPublisher(
        _target(),
        credentials=RegistryCredentials("publisher", "not-persisted"),
        transport=transport,
    ).publish(artifact)

    assert publication.immutable_reference == f"syntara/catalog-index@{artifact.digest}"
    assert [request[0] for request in transport.requests] == [
        "HEAD",
        "GET",
        "HEAD",
        "POST",
        "PUT",
        "HEAD",
        "POST",
        "PUT",
        "PUT",
    ]
    assert (
        transport.requests[-1][1]
        == "http://localhost:8443/v2/syntara/catalog-index/manifests/stable"
    )
    assert transport.requests[-1][3] == artifact.manifest.content
    assert "Authorization" not in transport.requests[0][2]
    assert transport.requests[1][2]["Authorization"].startswith("Basic ")
    assert all(
        request[2]["Authorization"] == "Bearer local-token" for request in transport.requests[2:]
    )
    assert parse_qs(urlparse(transport.requests[4][1]).query) == {
        "digest": [artifact.config.descriptor.digest]
    }
    assert parse_qs(urlparse(transport.requests[7][1]).query) == {
        "digest": [artifact.catalog_index.descriptor.digest]
    }


def test_publisher_reads_and_verifies_the_current_catalog_channel() -> None:
    artifact = _artifact()
    transport = _Transport(
        [
            OciRegistryResponse(
                200,
                {"Docker-Content-Digest": artifact.digest},
                artifact.manifest.content,
            ),
            OciRegistryResponse(200, {}, artifact.config.content),
            OciRegistryResponse(200, {}, artifact.catalog_index.content),
        ]
    )

    snapshot = CatalogIndexPublisher(_target(), transport=transport).read_current()

    assert snapshot is not None
    assert snapshot.manifest_digest == artifact.digest
    assert snapshot.document["metadata"]["generation"] == 1
    assert [request[0] for request in transport.requests] == ["GET", "GET", "GET"]
    assert transport.requests[0][1].endswith("/manifests/stable")
    assert transport.requests[1][1].endswith(f"/blobs/{artifact.config.descriptor.digest}")
    assert transport.requests[2][1].endswith(
        f"/blobs/{artifact.catalog_index.descriptor.digest}"
    )


def test_publisher_reads_one_immutable_catalog_revision_for_recovery() -> None:
    artifact = _artifact()
    transport = _Transport(
        [
            OciRegistryResponse(
                200,
                {"Docker-Content-Digest": artifact.digest},
                artifact.manifest.content,
            ),
            OciRegistryResponse(200, {}, artifact.config.content),
            OciRegistryResponse(200, {}, artifact.catalog_index.content),
        ]
    )

    snapshot = CatalogIndexPublisher(_target(), transport=transport).read_immutable(artifact.digest)

    assert snapshot.manifest_digest == artifact.digest
    assert transport.requests[0][1].endswith(f"/manifests/{artifact.digest}")


def test_publisher_rejects_current_catalog_with_unverified_manifest_digest() -> None:
    artifact = _artifact()
    transport = _Transport(
        [
            OciRegistryResponse(
                200,
                {"Docker-Content-Digest": "sha256:" + "0" * 64},
                artifact.manifest.content,
            )
        ]
    )

    with pytest.raises(CatalogIndexPublicationError, match="CATALOG_INDEX_MANIFEST_DIGEST_INVALID"):
        CatalogIndexPublisher(_target(), transport=transport).read_current()


def test_publisher_rejects_registry_attempts_to_redirect_an_upload_off_origin() -> None:
    artifact = _artifact()
    transport = _Transport(
        [
            OciRegistryResponse(404, {}),
            OciRegistryResponse(202, {"Location": "https://registry.example.test/upload"}),
        ]
    )

    with pytest.raises(CatalogIndexPublicationError, match="BLOB_UPLOAD_LOCATION_INVALID"):
        CatalogIndexPublisher(_target(), transport=transport).publish(artifact)


def test_publisher_rejects_a_bearer_token_realm_outside_the_configured_registry() -> None:
    artifact = _artifact()
    transport = _Transport(
        [
            OciRegistryResponse(
                401,
                {"WWW-Authenticate": 'Bearer realm="https://unexpected.example.test/token"'},
            )
        ]
    )

    with pytest.raises(CatalogIndexPublicationError, match="REGISTRY_AUTH_REALM_INVALID"):
        CatalogIndexPublisher(
            _target(),
            credentials=RegistryCredentials("publisher", "not-persisted"),
            transport=transport,
        ).publish(artifact)


def test_plugin_publisher_uploads_all_plugin_payload_blobs_before_advancing_channel(
    tmp_path: Path,
) -> None:
    artifact = _plugin_artifact(tmp_path)
    uploads = [
        f"/v2/syntara/plugins/github/blobs/uploads/upload-{number}" for number in range(1, 4)
    ]
    responses: list[OciRegistryResponse] = []
    for blob, upload in zip(
        (artifact.config, artifact.plugin_manifest, artifact.content_bundle), uploads, strict=True
    ):
        responses.extend(
            [
                OciRegistryResponse(404, {}),
                OciRegistryResponse(202, {"Location": upload}),
                OciRegistryResponse(201, {"Docker-Content-Digest": blob.descriptor.digest}),
            ]
        )
    responses.append(OciRegistryResponse(201, {"Docker-Content-Digest": artifact.digest}))
    transport = _Transport(responses)

    publication = PluginArtifactPublisher(
        PluginArtifactPublicationTarget(
            registry_origin="http://localhost:8443",
            repository="syntara/plugins/github",
            channel="0.1.0",
            allow_insecure_loopback_http=True,
        ),
        transport=transport,
    ).publish(artifact)

    assert publication.immutable_reference == f"syntara/plugins/github@{artifact.digest}"
    assert [request[0] for request in transport.requests] == [
        "HEAD",
        "POST",
        "PUT",
        "HEAD",
        "POST",
        "PUT",
        "HEAD",
        "POST",
        "PUT",
        "PUT",
    ]
    assert (
        transport.requests[-1][1]
        == "http://localhost:8443/v2/syntara/plugins/github/manifests/0.1.0"
    )
    assert transport.requests[-1][3] == artifact.manifest.content


@pytest.mark.parametrize(
    "origin, enabled",
    [
        ("http://registry.example.test", True),
        ("http://localhost:8443", False),
        ("https://registry.example.test", False),
    ],
)
def test_publication_target_allows_only_explicit_loopback_http(origin: str, enabled: bool) -> None:
    if origin.startswith("https://"):
        assert (
            CatalogIndexPublicationTarget(origin, "syntara/catalog-index", "stable").registry_origin
            == origin
        )
    else:
        with pytest.raises(ValueError, match="HTTP registry publication"):
            CatalogIndexPublicationTarget(
                origin,
                "syntara/catalog-index",
                "stable",
                allow_insecure_loopback_http=enabled,
            )
