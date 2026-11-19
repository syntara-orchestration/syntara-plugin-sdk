"""Exercise the immediately-started CI gate against GitHub job snapshots."""

from __future__ import annotations

import importlib.util
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

GATE = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "required_checks.py"


def evaluate(jobs: list[dict[str, object]], attempt: int = 1) -> tuple[str, list[str]]:
    return runpy.run_path(str(GATE))["evaluate_jobs"](jobs, {"tests", "compat"}, attempt)


def job(
    name: str, status: str, conclusion: str | None = None, attempt: int = 1
) -> dict[str, object]:
    return {"name": name, "status": status, "conclusion": conclusion, "run_attempt": attempt}


def test_pending_until_every_job_for_this_run_is_complete() -> None:
    assert evaluate([])[0] == "pending"
    assert evaluate([job("tests", "completed", "success")])[0] == "pending"
    assert (
        evaluate([job("tests", "completed", "success"), job("compat", "in_progress")])[0]
        == "pending"
    )


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", "timed_out", "neutral"])
def test_non_success_is_never_accepted(conclusion: str) -> None:
    status, failures = evaluate(
        [job("tests", "completed", "success"), job("compat", "completed", conclusion)]
    )
    assert status == "failure"
    assert any("compat" in failure for failure in failures)


def test_pass_only_when_all_current_jobs_succeed() -> None:
    assert (
        evaluate([job("tests", "completed", "success"), job("compat", "completed", "success")])[0]
        == "success"
    )


def test_duplicate_job_names_fail_closed() -> None:
    status, _ = evaluate(
        [
            job("tests", "completed", "success"),
            job("tests", "completed", "success"),
            job("compat", "completed", "success"),
        ]
    )
    assert status == "failure"


def test_retry_never_uses_an_earlier_attempts_green_result() -> None:
    jobs = [job("tests", "completed", "success"), job("compat", "in_progress", attempt=2)]
    assert evaluate(jobs, attempt=2) == ("pending", ["compat", "tests"])
    jobs.append(job("tests", "completed", "success", attempt=2))
    jobs[-2] = job("compat", "completed", "failure", attempt=2)
    assert evaluate(jobs, attempt=2)[0] == "failure"


def test_full_retry_accepts_only_the_new_attempt() -> None:
    jobs = [job("tests", "completed", "success", 2), job("compat", "completed", "success", 2)]
    assert evaluate(jobs, attempt=2)[0] == "success"


def test_partial_retry_fails_with_a_full_retry_instruction(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = importlib.util.spec_from_file_location("required_checks_test", GATE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("REQUIRED_JOBS", '["tests", "compat"]')
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "1234")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setattr(module, "fetch_jobs", lambda *_: [job("tests", "completed", "success", 2)])
    ticks = iter([0, 0, 0, 601])
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    assert module.main() == 1
    assert "re-run all jobs" in capsys.readouterr().out


@pytest.mark.parametrize("compat_result", ["success", "failure"])
def test_gate_queries_the_current_run_and_propagates_results(
    tmp_path: Path, compat_result: str
) -> None:
    snapshot = [
        {"jobs": [job("tests", "completed", "success"), job("compat", "completed", compat_result)]}
    ]
    data = tmp_path / "response.json"
    data.write_text(json.dumps(snapshot), encoding="utf-8")
    args = tmp_path / "args"
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGS"\ncat "$DATA"\n', encoding="utf-8")
    gh.chmod(0o755)
    env = os.environ | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "ARGS": str(args),
        "DATA": str(data),
        "REQUIRED_JOBS": '["tests", "compat"]',
        "GITHUB_REPOSITORY": "org/repo",
        "GITHUB_RUN_ID": "1234",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    process = subprocess.run([sys.executable, str(GATE)], env=env, capture_output=True, text=True)
    assert process.returncode == (0 if compat_result == "success" else 1), process.stderr
    arguments = args.read_text(encoding="utf-8")
    assert "repos/org/repo/actions/runs/1234/jobs?per_page=100&filter=latest" in arguments
    assert "--paginate" in arguments
