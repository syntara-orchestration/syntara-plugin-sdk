"""Validation helpers for immutable workload image references."""

from __future__ import annotations

import re

_REGISTRY_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_REGISTRY = rf"(?:{_REGISTRY_LABEL}(?:\.{_REGISTRY_LABEL})*(?::[1-9][0-9]{{0,4}})?/)?"
_REPOSITORY_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_DIGEST_IMAGE_REFERENCE = re.compile(
    rf"^{_REGISTRY}{_REPOSITORY_COMPONENT}(?:/{_REPOSITORY_COMPONENT})*"
    r"@sha256:[a-f0-9]{64}$"
)


def is_digest_image_reference(value: object) -> bool:
    """Return whether a workload image is pinned to a SHA-256 digest."""

    return isinstance(value, str) and _DIGEST_IMAGE_REFERENCE.fullmatch(value) is not None
