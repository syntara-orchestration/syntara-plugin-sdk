"""Narrow OCI Distribution publication for already-built Syntara artifacts.

The publisher owns no compiler, image builder, signer, or credential store. It
uploads immutable bytes supplied by the catalog-index or plugin-artifact
builders and moves one explicitly selected channel only after every referenced
blob is in the selected repository. HTTPS is the default. HTTP is accepted
exclusively for the explicit loopback development profile used by the local
Quay proof.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .artifact import OCI_ARTIFACT_TYPE, PluginArtifact
from .artifact_archive import ArtifactArchiveError, _verify_plugin_contents
from .catalog import (
    OCI_CATALOG_INDEX_ARTIFACT_TYPE,
    OCI_CATALOG_INDEX_CONFIG_MEDIA_TYPE,
    OCI_CATALOG_INDEX_CONTENT_MEDIA_TYPE,
    CatalogIndexArtifact,
    CatalogIndexArtifactBuildError,
    build_catalog_index_artifact,
)
from .oci import OCI_IMAGE_MANIFEST_MEDIA_TYPE, OciBlob

_REPOSITORY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,254}$")
_CHANNEL_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_LOOPBACK_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1"})
_UPLOAD_SUCCESS = frozenset({201, 202})
_MAX_AUTH_RESPONSE_BYTES = 16 * 1024
_MAX_REGISTRY_RESPONSE_BYTES = 16 * 1024 * 1024
_BEARER_CHALLENGE = re.compile(r"^Bearer\s+(?P<parameters>.+)$", re.IGNORECASE)
_AUTH_PARAMETER = re.compile(r'\s*(?P<name>[A-Za-z][A-Za-z0-9_-]*)="(?P<value>[^"]*)"\s*(?:,|$)')


class CatalogIndexPublicationError(RuntimeError):
    """A registry refused one immutable OCI artifact publication step."""

    def __init__(self, code: str) -> None:
        """Expose only a stable, non-secret failure classification."""
        super().__init__(f"OCI artifact publication failed: {code}")
        self.code = code


# The registry protocol and failure classifications are identical for the two
# artifact types. Keep this compatibility alias so callers can use the more
# specific name without creating an ambiguous second error taxonomy.
PluginArtifactPublicationError = CatalogIndexPublicationError


@dataclass(frozen=True)
class OciRegistryResponse:
    """Bounded registry response details needed for a publication decision."""

    status_code: int
    headers: Mapping[str, str]
    content: bytes = b""

    def header(self, name: str) -> str | None:
        """Look up one HTTP header without trusting casing from the server."""
        lower_name = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lower_name:
                return value
        return None


class OciRegistryTransport(Protocol):
    """Minimal synchronous transport so publisher behavior is testable offline."""

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        content: bytes | None = None,
    ) -> OciRegistryResponse:
        """Issue one redirect-free OCI Distribution request."""


class _NoRedirectHandler(HTTPRedirectHandler):
    """Keep upload sessions on the configured registry origin."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        """Treat every redirect as a response for the publisher to reject."""
        return None


class UrllibOciRegistryTransport:
    """stdlib transport that does not follow registry redirects or retain credentials."""

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        """Require a finite timeout for every development or CI publication."""
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds
        self._opener = build_opener(_NoRedirectHandler())

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        content: bytes | None = None,
    ) -> OciRegistryResponse:
        """Return the bounded response needed for an OCI publication decision."""
        request = Request(url, data=content, headers=dict(headers), method=method)
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                return OciRegistryResponse(
                    response.status,
                    dict(response.headers.items()),
                    response.read(_MAX_REGISTRY_RESPONSE_BYTES + 1),
                )
        except HTTPError as error:
            return OciRegistryResponse(
                error.code,
                dict(error.headers.items()) if error.headers else {},
                error.read(_MAX_REGISTRY_RESPONSE_BYTES + 1),
            )
        except (URLError, TimeoutError, OSError) as error:
            raise CatalogIndexPublicationError("REGISTRY_UNREACHABLE") from error


@dataclass(frozen=True)
class RegistryCredentials:
    """Ephemeral basic-auth input for a single SDK publication invocation."""

    username: str
    password: str

    def authorization_header(self) -> str:
        """Encode Basic credentials only while building an in-memory request."""
        if not self.username or not self.password or "\r" in self.username or "\n" in self.username:
            raise CatalogIndexPublicationError("REGISTRY_CREDENTIALS_INVALID")
        if "\r" in self.password or "\n" in self.password:
            raise CatalogIndexPublicationError("REGISTRY_CREDENTIALS_INVALID")
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode("ascii")
        return f"Basic {token}"


@dataclass(frozen=True)
class CatalogIndexPublicationTarget:
    """One bounded OCI repository/channel to receive a catalog-index artifact."""

    registry_origin: str
    repository: str
    channel: str
    allow_insecure_loopback_http: bool = False

    def __post_init__(self) -> None:
        """Reject URL/path widening before an HTTP request can be constructed."""
        parsed = urlparse(self.registry_origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("registry_origin must be an absolute registry origin")
        if parsed.scheme == "http" and (
            not self.allow_insecure_loopback_http or parsed.hostname not in _LOOPBACK_HTTP_HOSTS
        ):
            raise ValueError(
                "HTTP registry publication requires the explicit loopback development option"
            )
        if _REPOSITORY_PATTERN.fullmatch(self.repository) is None or any(
            part in {".", ".."} for part in self.repository.split("/")
        ):
            raise ValueError("repository must be a normalized OCI repository path")
        if _CHANNEL_PATTERN.fullmatch(self.channel) is None:
            raise ValueError("channel must be a normalized OCI tag")
        object.__setattr__(self, "registry_origin", self.registry_origin.rstrip("/"))


@dataclass(frozen=True)
class PluginArtifactPublicationTarget(CatalogIndexPublicationTarget):
    """One bounded OCI repository/channel for a compiled plugin artifact.

    This is intentionally distinct from the catalog-index target in the SDK
    surface even though both use the same OCI Distribution mechanics. A caller
    must explicitly choose the repository that will hold the plugin payload;
    publishing an index cannot accidentally publish a plugin payload there.
    """


@dataclass(frozen=True)
class CatalogIndexPublication:
    """Immutable publication coordinates safe to retain in a CI result or log."""

    repository: str
    channel: str
    manifest_digest: str

    @property
    def immutable_reference(self) -> str:
        """Return the canonical repository-plus-digest identity."""
        return f"{self.repository}@{self.manifest_digest}"


@dataclass(frozen=True)
class CatalogIndexSnapshot:
    """One verified existing catalog channel state, if the channel exists."""

    manifest_digest: str
    document: Mapping[str, Any]


@dataclass(frozen=True)
class PluginArtifactPublication:
    """Immutable coordinates safe to retain after publishing one plugin artifact."""

    repository: str
    channel: str
    manifest_digest: str

    @property
    def immutable_reference(self) -> str:
        """Return the canonical repository-plus-digest identity."""
        return f"{self.repository}@{self.manifest_digest}"


class CatalogIndexPublisher:
    """Publish one deterministic catalog-index artifact to one selected repository."""

    def __init__(
        self,
        target: CatalogIndexPublicationTarget,
        *,
        credentials: RegistryCredentials | None = None,
        transport: OciRegistryTransport | None = None,
    ) -> None:
        """Bind all mutable publication inputs outside the deterministic artifact builder."""
        self._target = target
        self._credentials = credentials
        self._transport = transport or UrllibOciRegistryTransport()
        self._bearer_authorization: str | None = None

    def publish(self, artifact: CatalogIndexArtifact) -> CatalogIndexPublication:
        """Upload missing immutable blobs, then advance the selected channel atomically.

        The manifest is published only after its config and content blobs are
        admitted by the registry. The method never invokes a signer, follows a
        redirect, lists repositories, or stores credentials.
        """
        _validate_artifact(artifact)
        for blob in (artifact.config, artifact.catalog_index):
            self._ensure_blob(blob)
        self._put_manifest(artifact.manifest, artifact.digest)
        return CatalogIndexPublication(
            repository=self._target.repository,
            channel=self._target.channel,
            manifest_digest=artifact.digest,
        )

    def read_current(self) -> CatalogIndexSnapshot | None:
        """Read and verify the index currently named by this publisher's channel.

        The reader accepts only the Syntara catalog artifact envelope, verifies
        the registry's manifest digest against its returned bytes, and verifies
        the content layer before returning a document for a merge operation.
        A missing channel is a normal first-publication state.
        """

        return self._read_catalog(self._target.channel, allow_missing=True)

    def read_immutable(self, manifest_digest: str) -> CatalogIndexSnapshot:
        """Read one known immutable catalog revision for explicit recovery.

        The caller supplies the Control Plane's last accepted digest.  Reading
        by digest rather than the mutable channel prevents a deleted or
        replayed tag from becoming the recovery base.
        """

        if not _is_sha256_digest(manifest_digest):
            raise CatalogIndexPublicationError("CATALOG_INDEX_DIGEST_INVALID")
        snapshot = self._read_catalog(manifest_digest, allow_missing=False)
        assert snapshot is not None
        if snapshot.manifest_digest != manifest_digest:
            raise CatalogIndexPublicationError("CATALOG_INDEX_MANIFEST_DIGEST_INVALID")
        return snapshot

    def _read_catalog(
        self, reference: str, *, allow_missing: bool
    ) -> CatalogIndexSnapshot | None:
        """Read and verify one channel or immutable catalog reference."""

        manifest_response = self._request(
            "GET",
            f"{self._repository_base_url()}/manifests/{quote(reference, safe=':')}",
        )
        if manifest_response.status_code == 404:
            if allow_missing:
                return None
            raise CatalogIndexPublicationError("CATALOG_INDEX_NOT_FOUND")
        if manifest_response.status_code != 200:
            raise CatalogIndexPublicationError("CATALOG_INDEX_FETCH_FAILED")
        manifest_digest = manifest_response.header("Docker-Content-Digest")
        if manifest_digest is None or manifest_digest != _sha256_digest(manifest_response.content):
            raise CatalogIndexPublicationError("CATALOG_INDEX_MANIFEST_DIGEST_INVALID")
        manifest = _json_mapping(manifest_response.content, "CATALOG_INDEX_MANIFEST_INVALID")
        config_digest, layer_digest = _catalog_descriptor_digests(manifest)
        config_response = self._request("GET", self._blob_url(config_digest))
        if config_response.status_code != 200:
            raise CatalogIndexPublicationError("CATALOG_INDEX_CONFIG_FETCH_FAILED")
        if _sha256_digest(config_response.content) != config_digest:
            raise CatalogIndexPublicationError("CATALOG_INDEX_CONFIG_DIGEST_INVALID")
        config = _json_mapping(config_response.content, "CATALOG_INDEX_CONFIG_INVALID")
        content_response = self._request("GET", self._blob_url(layer_digest))
        if content_response.status_code != 200:
            raise CatalogIndexPublicationError("CATALOG_INDEX_CONTENT_FETCH_FAILED")
        if _sha256_digest(content_response.content) != layer_digest:
            raise CatalogIndexPublicationError("CATALOG_INDEX_CONTENT_DIGEST_INVALID")
        document = _json_mapping(content_response.content, "CATALOG_INDEX_CONTENT_INVALID")
        try:
            build_catalog_index_artifact(document)
        except CatalogIndexArtifactBuildError as error:
            raise CatalogIndexPublicationError("CATALOG_INDEX_CONTENT_INVALID") from error
        _validate_catalog_config(config, document, layer_digest)
        return CatalogIndexSnapshot(manifest_digest=manifest_digest, document=document)

    def _ensure_blob(self, blob: OciBlob) -> None:
        """Upload exactly one missing blob through one same-origin upload session."""
        existing = self._request("HEAD", self._blob_url(blob.descriptor.digest))
        if existing.status_code == 200:
            return
        if existing.status_code != 404:
            raise CatalogIndexPublicationError("BLOB_EXISTENCE_CHECK_FAILED")
        started = self._request("POST", self._uploads_url())
        if started.status_code not in _UPLOAD_SUCCESS:
            raise CatalogIndexPublicationError("BLOB_UPLOAD_START_FAILED")
        location = started.header("Location")
        if not location:
            raise CatalogIndexPublicationError("BLOB_UPLOAD_LOCATION_MISSING")
        upload_url = self._same_origin_upload_url(location, blob.descriptor.digest)
        completed = self._request(
            "PUT",
            upload_url,
            content=blob.content,
            content_type="application/octet-stream",
        )
        if completed.status_code not in _UPLOAD_SUCCESS:
            raise CatalogIndexPublicationError("BLOB_UPLOAD_FAILED")
        self._verify_reported_digest(completed, blob.descriptor.digest)

    def _put_manifest(self, manifest: OciBlob, digest: str) -> None:
        """Advance the single configured channel to the already-built manifest bytes."""
        result = self._request(
            "PUT",
            f"{self._repository_base_url()}/manifests/{quote(self._target.channel, safe='')}",
            content=manifest.content,
            content_type=manifest.descriptor.media_type,
        )
        if result.status_code not in _UPLOAD_SUCCESS:
            raise CatalogIndexPublicationError("MANIFEST_PUBLICATION_FAILED")
        self._verify_reported_digest(result, digest)

    def _request(
        self,
        method: str,
        url: str,
        *,
        content: bytes | None = None,
        content_type: str | None = None,
    ) -> OciRegistryResponse:
        headers: dict[str, str] = {"Accept": "application/vnd.oci.image.manifest.v1+json"}
        if content_type is not None:
            headers["Content-Type"] = content_type
        if self._bearer_authorization is not None:
            headers["Authorization"] = self._bearer_authorization
        response = self._transport.request(method=method, url=url, headers=headers, content=content)
        if response.status_code != 401 or self._bearer_authorization is not None:
            return response
        self._bearer_authorization = self._exchange_bearer_token(response)
        retry_headers = {**headers, "Authorization": self._bearer_authorization}
        return self._transport.request(
            method=method,
            url=url,
            headers=retry_headers,
            content=content,
        )

    def _exchange_bearer_token(self, unauthorized: OciRegistryResponse) -> str:
        """Resolve one same-origin registry Bearer challenge using ephemeral credentials."""
        if self._credentials is None:
            raise CatalogIndexPublicationError("REGISTRY_AUTHENTICATION_REQUIRED")
        challenge = unauthorized.header("WWW-Authenticate")
        parameters = _parse_bearer_challenge(challenge)
        realm = parameters.get("realm")
        if realm is None:
            raise CatalogIndexPublicationError("REGISTRY_AUTH_CHALLENGE_INVALID")
        endpoint = self._same_origin_auth_realm(realm)
        query = {"scope": f"repository:{self._target.repository}:pull,push"}
        service = parameters.get("service")
        if service is not None:
            query["service"] = service
        separator = "&" if urlparse(endpoint).query else "?"
        token_response = self._transport.request(
            method="GET",
            url=f"{endpoint}{separator}{urlencode(query)}",
            headers={"Authorization": self._credentials.authorization_header()},
        )
        if token_response.status_code != 200:
            raise CatalogIndexPublicationError("REGISTRY_TOKEN_EXCHANGE_FAILED")
        if len(token_response.content) > _MAX_AUTH_RESPONSE_BYTES:
            raise CatalogIndexPublicationError("REGISTRY_TOKEN_RESPONSE_TOO_LARGE")
        try:
            payload = json.loads(token_response.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CatalogIndexPublicationError("REGISTRY_TOKEN_RESPONSE_INVALID") from error
        token = payload.get("token") or payload.get("access_token")
        if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
            raise CatalogIndexPublicationError("REGISTRY_TOKEN_RESPONSE_INVALID")
        return f"Bearer {token}"

    def _repository_base_url(self) -> str:
        return f"{self._target.registry_origin}/v2/{quote(self._target.repository, safe='/')}"

    def _blob_url(self, digest: str) -> str:
        return f"{self._repository_base_url()}/blobs/{quote(digest, safe=':')}"

    def _uploads_url(self) -> str:
        return f"{self._repository_base_url()}/blobs/uploads/"

    def _same_origin_upload_url(self, location: str, digest: str) -> str:
        """Reject registry-provided upload locations that leave the configured origin."""
        candidate = urljoin(f"{self._target.registry_origin}/", location)
        expected = urlparse(self._target.registry_origin)
        parsed = urlparse(candidate)
        if (
            parsed.scheme != expected.scheme
            or parsed.netloc != expected.netloc
            or not parsed.path.startswith(f"/v2/{self._target.repository}/blobs/uploads/")
        ):
            raise CatalogIndexPublicationError("BLOB_UPLOAD_LOCATION_INVALID")
        separator = "&" if parsed.query else "?"
        return f"{candidate}{separator}{urlencode({'digest': digest})}"

    def _same_origin_auth_realm(self, realm: str) -> str:
        """Require a registry token endpoint to remain on the configured origin."""
        candidate = urljoin(f"{self._target.registry_origin}/", realm)
        expected = urlparse(self._target.registry_origin)
        parsed = urlparse(candidate)
        if parsed.scheme != expected.scheme or parsed.netloc != expected.netloc:
            raise CatalogIndexPublicationError("REGISTRY_AUTH_REALM_INVALID")
        return candidate

    @staticmethod
    def _verify_reported_digest(response: OciRegistryResponse, expected_digest: str) -> None:
        reported = response.header("Docker-Content-Digest")
        if reported is not None and reported != expected_digest:
            raise CatalogIndexPublicationError("REGISTRY_DIGEST_MISMATCH")


class PluginArtifactPublisher(CatalogIndexPublisher):
    """Publish one already-built plugin metadata artifact to one selected repository.

    This adapter deliberately does not compile a workspace, build a workload
    image, attach a signature, or update a catalog index. Those are separate
    explicit operations so CI can review and record each immutable digest.
    """

    def __init__(
        self,
        target: PluginArtifactPublicationTarget,
        *,
        credentials: RegistryCredentials | None = None,
        transport: OciRegistryTransport | None = None,
    ) -> None:
        """Bind the selected plugin-artifact destination and ephemeral credentials."""
        super().__init__(target, credentials=credentials, transport=transport)

    def publish(self, artifact: PluginArtifact) -> PluginArtifactPublication:  # type: ignore[override]
        """Upload all verified plugin blobs, then advance only the selected channel."""
        _validate_plugin_artifact(artifact)
        for blob in (artifact.config, artifact.plugin_manifest, artifact.content_bundle):
            self._ensure_blob(blob)
        self._put_manifest(artifact.manifest, artifact.digest)
        return PluginArtifactPublication(
            repository=self._target.repository,
            channel=self._target.channel,
            manifest_digest=artifact.digest,
        )


def _validate_artifact(artifact: CatalogIndexArtifact) -> None:
    """Confirm the value object has not been manually substituted before publication."""
    blobs = (artifact.config, artifact.catalog_index, artifact.manifest)
    for blob in blobs:
        expected = OciBlob.create(
            blob.content,
            blob.descriptor.media_type,
            annotations=blob.descriptor.annotations,
        )
        if expected.descriptor != blob.descriptor:
            raise CatalogIndexPublicationError("ARTIFACT_DIGEST_MISMATCH")


def _validate_plugin_artifact(artifact: PluginArtifact) -> None:
    """Confirm a caller cannot substitute blobs or an OCI envelope before upload."""
    blobs = (artifact.config, artifact.plugin_manifest, artifact.content_bundle, artifact.manifest)
    for blob in blobs:
        expected = OciBlob.create(
            blob.content,
            blob.descriptor.media_type,
            annotations=blob.descriptor.annotations,
        )
        if expected.descriptor != blob.descriptor:
            raise CatalogIndexPublicationError("ARTIFACT_DIGEST_MISMATCH")
    try:
        manifest = json.loads(artifact.manifest.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CatalogIndexPublicationError("ARTIFACT_MANIFEST_INVALID") from error
    if not isinstance(manifest, dict) or (
        manifest.get("schemaVersion") != 2
        or manifest.get("mediaType") != OCI_IMAGE_MANIFEST_MEDIA_TYPE
        or manifest.get("artifactType") != OCI_ARTIFACT_TYPE
        or manifest.get("config") != artifact.config.descriptor.as_dict()
        or manifest.get("layers")
        != [
            artifact.plugin_manifest.descriptor.as_dict(),
            artifact.content_bundle.descriptor.as_dict(),
        ]
    ):
        raise CatalogIndexPublicationError("ARTIFACT_MANIFEST_INVALID")
    try:
        _verify_plugin_contents(artifact.config, artifact.plugin_manifest, artifact.content_bundle)
    except ArtifactArchiveError as error:
        raise CatalogIndexPublicationError("ARTIFACT_CONTENT_INVALID") from error


def _json_mapping(content: bytes, failure_code: str) -> Mapping[str, Any]:
    """Decode one bounded JSON object without allowing an arbitrary envelope."""

    if len(content) > _MAX_REGISTRY_RESPONSE_BYTES:
        raise CatalogIndexPublicationError(failure_code)
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CatalogIndexPublicationError(failure_code) from error
    if not isinstance(value, Mapping):
        raise CatalogIndexPublicationError(failure_code)
    return cast(Mapping[str, Any], value)


def _catalog_descriptor_digests(manifest: Mapping[str, Any]) -> tuple[str, str]:
    """Extract config and sole catalog-content layer digests from an OCI manifest."""

    if (
        manifest.get("schemaVersion") != 2
        or manifest.get("mediaType") != OCI_IMAGE_MANIFEST_MEDIA_TYPE
        or manifest.get("artifactType") != OCI_CATALOG_INDEX_ARTIFACT_TYPE
    ):
        raise CatalogIndexPublicationError("CATALOG_INDEX_MANIFEST_INVALID")
    config = manifest.get("config")
    if not isinstance(config, Mapping):
        raise CatalogIndexPublicationError("CATALOG_INDEX_MANIFEST_INVALID")
    config_digest = config.get("digest")
    if config.get("mediaType") != OCI_CATALOG_INDEX_CONFIG_MEDIA_TYPE or not _is_sha256_digest(
        config_digest
    ):
        raise CatalogIndexPublicationError("CATALOG_INDEX_MANIFEST_INVALID")
    layers = manifest.get("layers")
    if not isinstance(layers, list) or len(layers) != 1 or not isinstance(layers[0], Mapping):
        raise CatalogIndexPublicationError("CATALOG_INDEX_MANIFEST_INVALID")
    layer = layers[0]
    digest = layer.get("digest")
    if layer.get("mediaType") != OCI_CATALOG_INDEX_CONTENT_MEDIA_TYPE or not _is_sha256_digest(digest):
        raise CatalogIndexPublicationError("CATALOG_INDEX_MANIFEST_INVALID")
    assert isinstance(config_digest, str)
    return config_digest, digest


def _validate_catalog_config(
    config: Mapping[str, Any], document: Mapping[str, Any], content_digest: str
) -> None:
    """Confirm the envelope config binds the exact downloaded catalog document."""

    metadata = document.get("metadata")
    entries = document.get("spec")
    if not isinstance(metadata, Mapping) or not isinstance(entries, Mapping):
        raise CatalogIndexPublicationError("CATALOG_INDEX_CONFIG_INVALID")
    catalog_entries = entries.get("entries")
    if (
        config.get("apiVersion") != "syntara.catalog-index.config/v1alpha1"
        or config.get("kind") != "CatalogIndexArtifactConfig"
        or config.get("catalogIndexDigest") != content_digest
        or config.get("sourceId") != metadata.get("sourceId")
        or config.get("generation") != metadata.get("generation")
        or not isinstance(catalog_entries, list)
        or config.get("entryCount") != len(catalog_entries)
    ):
        raise CatalogIndexPublicationError("CATALOG_INDEX_CONFIG_INVALID")


def _sha256_digest(content: bytes) -> str:
    return f"sha256:{sha256(content).hexdigest()}"


def _is_sha256_digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def _parse_bearer_challenge(challenge: str | None) -> dict[str, str]:
    """Parse the narrow quoted Bearer challenge shape used by OCI registries."""
    if challenge is None:
        raise CatalogIndexPublicationError("REGISTRY_AUTH_CHALLENGE_MISSING")
    match = _BEARER_CHALLENGE.fullmatch(challenge)
    if match is None:
        raise CatalogIndexPublicationError("REGISTRY_AUTH_CHALLENGE_INVALID")
    remaining = match.group("parameters")
    parameters: dict[str, str] = {}
    while remaining:
        parameter = _AUTH_PARAMETER.match(remaining)
        if parameter is None:
            raise CatalogIndexPublicationError("REGISTRY_AUTH_CHALLENGE_INVALID")
        name = parameter.group("name").lower()
        if name in parameters:
            raise CatalogIndexPublicationError("REGISTRY_AUTH_CHALLENGE_INVALID")
        parameters[name] = parameter.group("value")
        remaining = remaining[parameter.end() :]
    return parameters
