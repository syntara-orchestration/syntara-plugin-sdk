"""Normative canonical JSON and generic-container request hashing helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any


class CanonicalJsonError(ValueError):
    """A value cannot be represented by the Syntara canonical JSON form."""


def canonical_json_bytes(value: object) -> bytes:
    """Return the strict deterministic JSON form used by versioned contracts.

    The format is UTF-8 JSON with sorted object keys, compact separators, and
    no non-finite numbers. Other language SDKs must implement this exact form
    before they calculate a compatible request hash.
    """

    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CanonicalJsonError("value is not canonical JSON") from error


def canonical_json_digest(value: object) -> str:
    """Return the SHA-256 digest of one canonical JSON value."""

    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def container_workload_request_digest(request: Mapping[str, Any]) -> str:
    """Hash a container workload request without its two mirrored hash fields.

    The work-item and policy snapshot each carry the resulting digest. Removing
    both fields before hashing avoids a circular representation while ensuring
    every other execution, policy, and runtime value is committed.

    An explicitly supplied ``workItem.maxOutputBytes`` participates unchanged.
    Omission remains omission in the hash; resolve its default only when
    constructing the invocation after verifying this original document.
    """

    try:
        canonical_request = deepcopy(dict(request))
        for section_name in ("workItem", "policySnapshot"):
            section = canonical_request.get(section_name)
            if isinstance(section, Mapping):
                section_copy = dict(section)
                section_copy.pop("requestHash", None)
                canonical_request[section_name] = section_copy
    except (TypeError, ValueError) as error:
        raise CanonicalJsonError("container workload request is not canonical JSON") from error
    return canonical_json_digest(canonical_request)
