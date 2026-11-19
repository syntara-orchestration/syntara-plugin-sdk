"""The grouped CI runner retains every result and never hides runner errors."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import run_grouped


@pytest.mark.parametrize("value", ['[]', '["same", "same"]', '["../escape"]', '[1]', '{}'])
def test_discovery_must_be_nonempty_unique_and_safe(value: str) -> None:
    with pytest.raises(ValueError):
        run_grouped.parse_plugins(value)


def test_runs_all_step_types_when_one_is_incompatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def execute(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0)
        plugin = command[command.index("--plugin") + 1]
        directory = Path(command[command.index("--report-dir") + 1])
        calls.append(plugin)
        failed = plugin == "one"
        (directory / f"{plugin}.json").write_text(
            json.dumps({"schema": 1, "result": "fail" if failed else "pass", "report_only": False})
        )
        return subprocess.CompletedProcess(command, int(failed))

    monkeypatch.setattr(run_grouped.subprocess, "run", execute)
    result = run_grouped.run_plugins(
        ["one", "two"], driver=tmp_path / "driver.py", checkout=tmp_path,
        wheels=tmp_path, reports=tmp_path / "reports", mode="enforcing",
    )
    assert result == 0
    assert calls == ["one", "two"]
    assert json.loads((tmp_path / "reports/one.json").read_text())["result"] == "fail"


def test_unexplained_driver_failure_is_recorded_and_does_not_skip_later_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def execute(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0)
        plugin = command[command.index("--plugin") + 1]
        directory = Path(command[command.index("--report-dir") + 1])
        calls.append(plugin)
        (directory / f"{plugin}.json").write_text(
            json.dumps({"schema": 1, "result": "pass", "report_only": False})
        )
        return subprocess.CompletedProcess(command, 3 if plugin == "one" else 0)

    monkeypatch.setattr(run_grouped.subprocess, "run", execute)
    result = run_grouped.run_plugins(
        ["one", "two"], driver=tmp_path / "driver.py", checkout=tmp_path,
        wheels=tmp_path, reports=tmp_path / "reports", mode="enforcing",
    )
    assert result == 1
    assert calls == ["one", "two"]
    assert json.loads((tmp_path / "reports/one.json").read_text())["runner_failure"] is True


def test_missing_or_invalid_baseline_fails_closed(tmp_path: Path) -> None:
    plugins = '["one", "two"]'
    (tmp_path / "one.json").write_text(json.dumps({"schema": 1, "result": "pass"}))
    assert run_grouped.main(["--check-baseline", str(tmp_path), "--plugins", plugins]) == 1
    (tmp_path / "two.json").write_text(json.dumps({"schema": 1, "result": "fail"}))
    assert run_grouped.main(["--check-baseline", str(tmp_path), "--plugins", plugins]) == 0
    (tmp_path / "two.json").write_text(json.dumps({"schema": 1, "result": "fail", "runner_failure": True}))
    assert run_grouped.main(["--check-baseline", str(tmp_path), "--plugins", plugins]) == 1
