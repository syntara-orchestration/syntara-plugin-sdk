"""Keep the CI check pending until all jobs in this workflow run succeed."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import Any


def evaluate_jobs(
    jobs: Sequence[dict[str, Any]], required: set[str], attempt: int
) -> tuple[str, list[str]]:
    """Return pending until all required jobs complete; reject every non-success."""
    found: dict[str, dict[str, Any]] = {}
    for job in jobs:
        name = job.get("name")
        if name not in required or job.get("run_attempt") != attempt:
            continue
        if name in found:
            return "failure", [f"duplicate required job: {name}"]
        found[name] = job
    waiting = sorted(
        name for name in required if name not in found or found[name].get("status") != "completed"
    )
    if waiting:
        return "pending", waiting
    failures = [
        f"{name}: {found[name].get('conclusion')}"
        for name in sorted(required)
        if found[name].get("conclusion") != "success"
    ]
    return ("failure", failures) if failures else ("success", [])


def fetch_jobs(repository: str, run_id: str) -> list[dict[str, Any]]:
    """Read the jobs GitHub reports for the latest run attempt."""
    endpoint = f"repos/{repository}/actions/runs/{run_id}/jobs?per_page=100&filter=latest"
    process = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", endpoint],
        check=True,
        capture_output=True,
        text=True,
    )
    pages = json.loads(process.stdout)
    if not isinstance(pages, list) or not pages:
        raise ValueError("GitHub returned no job pages")
    jobs: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("jobs"), list):
            raise ValueError("GitHub returned an invalid job page")
        if any(not isinstance(job, dict) for job in page["jobs"]):
            raise ValueError("GitHub returned an invalid job")
        jobs.extend(page["jobs"])
    return jobs


def required_jobs() -> set[str]:
    """Read and validate the job names the gate must check."""
    names = json.loads(os.environ["REQUIRED_JOBS"])
    if (
        not isinstance(names, list)
        or not names
        or any(not isinstance(name, str) or not name for name in names)
    ):
        raise ValueError("REQUIRED_JOBS must be a nonempty list of job names")
    if len(set(names)) != len(names):
        raise ValueError("REQUIRED_JOBS contains duplicates")
    return set(names)


def missing_from_retry(jobs: Sequence[dict[str, Any]], required: set[str], attempt: int) -> bool:
    """Check whether a retry omitted any required job from its own attempt."""
    present = {job.get("name") for job in jobs if job.get("run_attempt") == attempt}
    return bool(required - present)


def wait_for_jobs(repository: str, run_id: str, attempt: int, required: set[str]) -> int:
    """Wait for the current attempt's required jobs and report their verdict."""
    deadline = time.monotonic() + 355 * 60
    missing_retry_deadline = time.monotonic() + 10 * 60
    previous: list[str] | None = None
    while time.monotonic() < deadline:
        jobs = fetch_jobs(repository, run_id)
        status, details = evaluate_jobs(jobs, required, attempt)
        if status == "success":
            print("All required CI jobs passed.", flush=True)
            return 0
        if status == "failure":
            for detail in details:
                print(f"::error::Required CI job did not succeed: {detail}", flush=True)
            return 1
        if (
            attempt > 1
            and time.monotonic() >= missing_retry_deadline
            and missing_from_retry(jobs, required, attempt)
        ):
            print(
                "::error::Required jobs are missing from this retry; re-run all jobs.", flush=True
            )
            return 1
        if details != previous:
            print("Waiting for: " + ", ".join(details), flush=True)
            previous = details
        time.sleep(30)
    print("::error::Timed out waiting for required CI jobs.", flush=True)
    return 1


def main() -> int:
    required = required_jobs()
    repository = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
    if attempt < 1:
        raise ValueError("Invalid workflow run attempt")
    return wait_for_jobs(repository, run_id, attempt, required)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, ValueError, subprocess.CalledProcessError) as error:
        print(f"::error::Unable to check required CI jobs: {error}", file=sys.stderr)
        raise SystemExit(1) from error
