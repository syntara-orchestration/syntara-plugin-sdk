"""Run discovered step types sequentially and retain every compatibility report."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

from baseline import load as load_baseline
from record_runner_failure import record_unexpected_runner_failure

_PLUGIN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


def parse_plugins(value: str) -> list[str]:
    """Reject an empty, duplicated, or unsafe discovered list."""
    names = json.loads(value)
    if (
        not isinstance(names, list)
        or not names
        or any(not isinstance(name, str) or not _PLUGIN.fullmatch(name) for name in names)
        or len(set(names)) != len(names)
    ):
        raise ValueError("discovery must provide a nonempty list of unique step types")
    return names


def run_plugins(
    plugins: Sequence[str],
    *,
    driver: Path,
    checkout: Path,
    wheels: Path,
    reports: Path,
    mode: str,
) -> int:
    """Run all drivers; expected incompatibilities are decided by the final report."""
    reports.mkdir(parents=True, exist_ok=True)
    incomplete = False
    for plugin in plugins:
        report = reports / f"{plugin}.json"
        report.unlink(missing_ok=True)
        try:
            for command in (("reset", "--quiet", "--hard"), ("clean", "-fdq")):
                subprocess.run(["git", "-C", str(checkout), *command], check=True)
            result = subprocess.run(
                [
                    "uv", "run", "--no-project", "--python", "3.12", "python", str(driver),
                    "--plugin", plugin, "--step-types-dir", str(checkout), "--wheels-dir",
                    str(wheels), "--report-dir", str(reports), "--mode", mode,
                ],
                check=False,
            )
            if result.returncode != 0:
                record_unexpected_runner_failure(report, str(result.returncode))
            try:
                document = json.loads(report.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                document = None
            if (
                not isinstance(document, dict) or document.get("schema") != 1
                or document.get("result") not in {"pass", "fail"}
                or document.get("runner_failure") is True
            ):
                incomplete = True
        except (OSError, subprocess.CalledProcessError) as error:
            print(f"Could not run {plugin}: {error}")
            incomplete = True
    return int(incomplete)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugins", required=True)
    parser.add_argument("--check-baseline", type=Path)
    parser.add_argument("--driver", type=Path)
    parser.add_argument("--checkout", type=Path)
    parser.add_argument("--wheels", type=Path)
    parser.add_argument("--reports", type=Path)
    parser.add_argument("--mode", default="enforcing")
    args = parser.parse_args(argv)
    try:
        plugins = parse_plugins(args.plugins)
    except (ValueError, TypeError) as error:
        print(f"Step-type discovery failed: {error}")
        return 1
    if args.check_baseline is not None:
        reports, _ = load_baseline(args.check_baseline, plugins)
        return int(any(
            report is None or report.get("result") not in {"pass", "fail"}
            or report.get("runner_failure") is True for report in reports.values()
        ))
    if any(value is None for value in (args.driver, args.checkout, args.wheels, args.reports)):
        parser.error("--driver, --checkout, --wheels and --reports are required for a run")
    return run_plugins(
        plugins, driver=args.driver.resolve(), checkout=args.checkout.resolve(),
        wheels=args.wheels.resolve(), reports=args.reports.resolve(), mode=args.mode,
    )


if __name__ == "__main__":
    raise SystemExit(main())
