"""Select the devel push run for the exact pull request base commit."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any


def select_run(document: Any, base_sha: str) -> int | None:
    """Ignore manual, other-branch, incomplete, and unrelated workflow runs."""
    runs = document.get("workflow_runs") if isinstance(document, dict) else None
    if not isinstance(runs, list):
        raise ValueError("GitHub did not return a workflow run list")
    for name in ("Step types compatibility", "CI"):
        for run in runs:
            if (
                isinstance(run, dict)
                and run.get("name") == name
                and run.get("event") == "push"
                and run.get("head_branch") == "devel"
                and run.get("head_sha") == base_sha
                and run.get("status") == "completed"
                and run.get("conclusion") in {"success", "failure"}
                and isinstance(run.get("id"), int)
            ):
                return run["id"]
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-sha", required=True)
    args = parser.parse_args(argv)
    try:
        run_id = select_run(json.load(sys.stdin), args.base_sha)
    except (ValueError, TypeError) as error:
        print(f"Unable to find a trusted devel baseline: {error}", file=sys.stderr)
        return 1
    print(run_id if run_id is not None else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
