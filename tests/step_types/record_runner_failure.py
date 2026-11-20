"""Mark a driver failure that its compatibility report does not explain."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path


def record_unexpected_runner_failure(report_path: Path, exit_code: str) -> bool:
    """Mark a nonzero runner exit unless the report explains an enforced failure.

    A failed compatibility report with exit code 1 is the driver's expected
    enforcement result. Missing or invalid reports are left untouched because
    the aggregate gate already rejects them.
    """
    try:
        contents = report_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    try:
        report = json.loads(contents)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(report, dict):
        return False
    if (
        report.get("result") == "fail"
        and report.get("report_only") is False
        and exit_code == "1"
    ):
        return False
    report["runner_failure"] = True
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--exit-code", required=True)
    args = parser.parse_args(argv)
    try:
        marked = record_unexpected_runner_failure(args.report, args.exit_code)
    except OSError as error:
        print(f"could not record runner failure in {args.report}: {error}", file=sys.stderr)
        return 1
    if marked:
        print(f"marked {args.report} as an unexplained runner failure")
    return 0


if __name__ == "__main__":
    sys.exit(main())
