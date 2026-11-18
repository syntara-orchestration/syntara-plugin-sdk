"""Shared parsing and validation for Syntara OCI runtime annotations."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

RUNTIME_LANGUAGE_ANNOTATION = "io.syntara.runtime.language"
RUNTIME_VERSION_ANNOTATION = "io.syntara.runtime.version"

_LANGUAGE_PATTERN = re.compile(r"^[a-z][a-z0-9.+_-]{0,63}$")
_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+()_-]{0,255}$")


class RuntimeMetadataError(ValueError):
    """Raised when OCI runtime annotations are incomplete or invalid."""


def is_valid_runtime_language(value: object) -> bool:
    return isinstance(value, str) and _LANGUAGE_PATTERN.fullmatch(value) is not None


def is_valid_runtime_version(value: object) -> bool:
    return isinstance(value, str) and _VERSION_PATTERN.fullmatch(value) is not None


def runtime_metadata_from_annotations(
    *sources: Mapping[str, Any],
    source_name: str = "OCI image",
) -> dict[str, str] | None:
    """Merge and validate the language/version annotations from OCI descriptors."""

    keys = (RUNTIME_LANGUAGE_ANNOTATION, RUNTIME_VERSION_ANNOTATION)
    values: dict[str, str] = {}
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise RuntimeMetadataError(f"{source_name} annotation {key} must be a string")
            if key in values and values[key] != value:
                raise RuntimeMetadataError(f"{source_name} has conflicting {key} annotations")
            values[key] = value

    if not values:
        return None
    if set(values) != set(keys):
        raise RuntimeMetadataError(
            f"{source_name} must provide both Syntara runtime language and version annotations"
        )
    language = values[RUNTIME_LANGUAGE_ANNOTATION]
    version = values[RUNTIME_VERSION_ANNOTATION]
    if not is_valid_runtime_language(language):
        raise RuntimeMetadataError(f"{source_name} runtime language annotation is invalid")
    if not is_valid_runtime_version(version):
        raise RuntimeMetadataError(f"{source_name} runtime version annotation is invalid")
    return {"language": language, "version": version}
