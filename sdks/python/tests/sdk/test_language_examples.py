"""Fast checks for the Go and TypeScript example plugins.

The examples contain no Python: they demonstrate that a non-Python entry
point is a first-class step type. These checks exercise what the SDK sees
without a container engine: the compiled contract and the opaque entry point.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from syntara_plugin.contracts import (
    load_bundle,
    registration_manifest,
    validate_document,
    validate_registration_manifest,
)
from syntara_plugin.runtime import is_python_entry_point
from syntara_plugin.sdk import BuildRequest, compile_workspace
from syntara_plugin.sdk.cli import main

ROOT = Path(__file__).parents[4]

# example directory -> (entry point, the source file whose handler table serves it)
EXAMPLES = {
    "go-steps": ("handlers.NormalizeRecords", "handlers.go"),
    "typescript-steps": ("handlers.ts#normalizeRecords", "src/handlers.ts"),
}


def _target(example: str) -> tuple[dict, dict]:
    result = compile_workspace(
        BuildRequest(ROOT / "examples" / example / "plugin.yaml", require_image_bindings=False)
    )
    (target,) = result.descriptor["spec"]["targets"]
    return result.descriptor, target


@pytest.mark.parametrize("example", EXAMPLES)
def test_example_validates(example: str, capsys) -> None:
    workspace = ROOT / "examples" / example
    status = main(["validate", str(workspace / "plugin.yaml"), "--json"])

    assert status == 0, capsys.readouterr().out
    assert json.loads(capsys.readouterr().out)["ok"] is True


@pytest.mark.parametrize(("example", "expected"), [(name, ep) for name, (ep, _) in EXAMPLES.items()])
def test_example_entry_point_is_opaque_and_reaches_the_registration_manifest(
    example: str, expected: str
) -> None:
    descriptor, target = _target(example)
    entry_point = target["runtime"]["entryPoint"]
    assert entry_point == expected
    assert not is_python_entry_point(entry_point)

    workload = target["runtime"]["workload"]
    manifest = registration_manifest(descriptor, workload)
    assert validate_document(load_bundle(), "registration_manifest", manifest) == []
    validate_registration_manifest(descriptor, workload, manifest)
    assert manifest["steps"][target["step_revision_id"]]["entryPoint"] == expected


@pytest.mark.parametrize(("example", "source"), [(name, src) for name, (_, src) in EXAMPLES.items()])
def test_example_handler_table_serves_the_declared_entry_point(example: str, source: str) -> None:
    _, target = _target(example)
    handler_source = (ROOT / "examples" / example / source).read_text(encoding="utf-8")

    assert f'"{target["runtime"]["entryPoint"]}"' in handler_source.replace("'", '"')


def test_examples_share_one_step_contract() -> None:
    contracts = {
        example: (target["input"], target["output"])
        for example in EXAMPLES
        for _, target in [_target(example)]
    }

    assert len({json.dumps(contract, sort_keys=True) for contract in contracts.values()}) == 1
