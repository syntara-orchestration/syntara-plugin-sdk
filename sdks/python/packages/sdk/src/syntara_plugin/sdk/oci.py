"""Small, deterministic OCI value objects shared by SDK artifact builders."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256

OCI_IMAGE_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"


@dataclass(frozen=True)
class OciDescriptor:
    """A content descriptor in one OCI manifest."""

    media_type: str
    digest: str
    size: int
    annotations: Mapping[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        """Return the OCI JSON representation without empty optional fields."""

        value: dict[str, object] = {
            "mediaType": self.media_type,
            "digest": self.digest,
            "size": self.size,
        }
        if self.annotations:
            value["annotations"] = {key: self.annotations[key] for key in sorted(self.annotations)}
        return value


@dataclass(frozen=True)
class OciBlob:
    """One immutable OCI blob and its descriptor."""

    descriptor: OciDescriptor
    content: bytes

    @classmethod
    def create(
        cls, content: bytes, media_type: str, *, annotations: Mapping[str, str] | None = None
    ) -> OciBlob:
        """Content-address immutable bytes using the OCI SHA-256 convention."""

        return cls(
            descriptor=OciDescriptor(
                media_type=media_type,
                digest=f"sha256:{sha256(content).hexdigest()}",
                size=len(content),
                annotations=annotations or {},
            ),
            content=content,
        )


def canonical_json_bytes(value: object) -> bytes:
    """Encode a JSON value once, using the SDK's deterministic OCI convention."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
