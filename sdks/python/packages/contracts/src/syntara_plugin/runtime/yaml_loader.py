"""The shared unique-key YAML loader.

Both the workspace compiler and the packaged-plugin bindings wrapper parse
YAML with the same rules: duplicate mapping keys are
rejected, and complex (unhashable) mapping keys surface as a constructor
error instead of a raw ``TypeError``. This module is the single loader both
parsers use so their accepted document sets stay aligned.
"""

from __future__ import annotations

from typing import Any

import yaml
from yaml.constructor import ConstructorError

__all__ = ["UniqueKeyLoader", "construct_unique_mapping"]


class UniqueKeyLoader(yaml.SafeLoader):
    """A YAML loader that rejects duplicate mapping keys."""


def construct_unique_mapping(
    loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "unsupported unhashable mapping key",
                key_node.start_mark,
            ) from None
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_unique_mapping
)
