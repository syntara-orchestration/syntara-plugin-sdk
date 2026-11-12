"""Keep the classifier in sync with the SDK's live error surface.

The classifier pins SDK-emitted codes and message text as they are today.
Without these tests, an SDK change would silently degrade future analyses to
``unknown`` or a wrong category. They import the SDK from this repository, so
they run wherever the tooling suite runs with the repository environment
(CI, ``make test``) and skip in standalone runs without it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from classifier import (
    _BOUNDARY_CODES,
    _INPUT_CODES,
    _STEP_REPORTED_CODES,
    _TRANSIENT_CODES,
    _UNDETERMINED_CODES,
    Failure,
    classify,
)

RUN_CODE_SETS = (
    _TRANSIENT_CODES,
    _BOUNDARY_CODES,
    _INPUT_CODES,
    _STEP_REPORTED_CODES,
    _UNDETERMINED_CODES,
)


def test_every_failure_code_is_mapped_exactly_once() -> None:
    abi = pytest.importorskip("syntara_plugin.runtime.abi")
    codes = {member.value for member in abi.FailureCode}
    mapped: set[str] = set()
    for bucket in RUN_CODE_SETS:
        overlap = mapped & bucket
        assert not overlap, f"a FailureCode is mapped twice: {sorted(overlap)}"
        mapped |= bucket
    stale = mapped - codes
    assert not stale, f"the classifier maps codes the SDK no longer has: {sorted(stale)}"
    missing = codes - mapped
    assert not missing, (
        "the SDK added a FailureCode; map it in the run-code sets in classifier.py: "
        f"{sorted(missing)}"
    )


def cli() -> Callable[[list[str]], int]:
    module = pytest.importorskip("syntara_plugin.sdk.cli")
    return module.main


def scaffold(tmp_path: Path, main: Callable[[list[str]], int]) -> Path:
    """A valid custom-workload plugin workspace straight from `init`."""
    workspace = tmp_path / "demo"
    assert main(
        [
            "init",
            str(workspace),
            "--namespace",
            "acme",
            "--name",
            "demo",
            "--template",
            "custom-workload",
            "--runner",
            "custom-runner",
        ]
    ) == 0
    return workspace


def run_cli(
    main: Callable[[list[str]], int], workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> tuple[int, Failure]:
    monkeypatch.chdir(workspace)
    capsys.readouterr()
    code = main(["validate", "--json"])
    captured = capsys.readouterr()
    try:
        document = json.loads(captured.out) if captured.out.strip() else None
    except ValueError:
        document = None
    failure = Failure(
        stage="validate",
        plugin="demo",
        exit_code=code,
        stdout="false\n" if document else captured.out,
        stderr=captured.err,
        diagnostic=document,
        plugin_dir=workspace,
    )
    return code, failure


def test_live_unsupported_setting_still_classifies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    main = cli()
    workspace = scaffold(tmp_path, main)
    settings = workspace / "syntara-plugin.yaml"
    settings.write_text(
        settings.read_text(encoding="utf-8").replace(
            "build:\n", "build:\n  workload:\n    engine: podman\n", 1
        ),
        encoding="utf-8",
    )
    code, failure = run_cli(main, workspace, capsys, monkeypatch)
    result = classify(failure)
    assert (result.category, result.rule_id) == ("misconfiguration", "MC001")
    assert result.facts["keys"] == ["workload"]
    assert code != 0


# The required-setting signature (MC003, "build.workloads.<id> requires: ...") only
# fires during `build`'s runtime-binding resolution, which needs a registry; its
# real captured shape is pinned in test_step_types_classifier_gaps.py instead.


def test_live_missing_target_diagnostic_still_classifies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    main = cli()
    workspace = scaffold(tmp_path, main)
    target = next((workspace / "steps").glob("*/manifest.yaml"))
    manifest = workspace / "plugin.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(target.name, "missing.yaml"),
        encoding="utf-8",
    )
    code, failure = run_cli(main, workspace, capsys, monkeypatch)
    result = classify(failure)
    assert (result.category, result.rule_id) == ("misconfiguration", "MC006")
    assert result.facts["codes"] == ["TARGET_PATH_INVALID"]
    assert code != 0
