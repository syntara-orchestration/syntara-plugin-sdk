from __future__ import annotations

from collections.abc import Mapping


def step_document(
    kind: str, metadata: Mapping[str, str], specification: Mapping[str, object]
) -> dict[str, object]:
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": kind,
        "metadata": dict(metadata),
        "spec": dict(specification),
    }


def target_metadata(name: str, display_name: str, description: str) -> dict[str, str]:
    return {"name": name, "displayName": display_name, "description": description}
