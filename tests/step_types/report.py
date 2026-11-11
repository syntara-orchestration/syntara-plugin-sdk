"""Aggregate the per-step-type reports into one verdict (the compat-report job).

This job is the single gate for the whole check, and its exit code encodes the
policy in one place:

- the change detection job did not succeed: fail (an empty answer is not a skip);
- the change does not touch anything the check covers: pass, skipped by design;
- discovery or the tooling tests did not succeed: fail;
- a discovered step type left no report (the leg died early, for example while
  building the candidate wheels, or the tooling crashed): fail;
- a step type failed: fail, unless the run is report-only, in which case the
  failures become warnings. Report-only comes from exactly two places: a manual
  run with ``report_only`` (recorded in each report as ``report-only:dispatch``)
  or the ``breaking-change`` label as read live by this job;
- a leg that did not succeed without leaving a failing report: fail;
- otherwise: pass.

Skipped jobs count as success for required checks, so this job judges its
needs' results itself instead of inheriting them. The label is read only here,
at gate time: the mode a leg recorded from the label may be stale (the label
can be removed after the legs ran, and "Re-run failed jobs" reuses outputs of
jobs that already succeeded), so it is never trusted.

Usage::

    python tests/step_types/report.py --reports-dir reports --plugins '["a","b"]' \\
        --changes success --run-compat true --discover success --tooling success \\
        --compat success [--label-report-only true]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from classifier import outcome_word
from render import CYAN, GREEN, RED, YELLOW, Renderer, append_summary, md_table


@dataclass
class Verdict:
    code: int
    reasons: list[str]
    warnings: list[str] = field(default_factory=list)


def load_reports(directory: Path, plugins: Sequence[str]) -> dict[str, dict[str, Any] | None]:
    reports: dict[str, dict[str, Any] | None] = {}
    for plugin in plugins:
        path = directory / f"{plugin}.json"
        try:
            reports[plugin] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            reports[plugin] = None
    return reports


def report_only(report: dict[str, Any], label: bool) -> bool:
    """Whether a failed report is reported only: a manual report_only run, or the live label."""
    return report.get("mode") == "report-only:dispatch" or label


def _word(report: dict[str, Any]) -> str:
    return outcome_word(str((report.get("failure") or {}).get("category", "")))


def verdict(args: argparse.Namespace, reports: dict[str, dict[str, Any] | None]) -> Verdict:
    """Return the exit code, the reasons behind it, and any report-only warnings."""
    if args.changes != "success":
        return Verdict(1, [f"the change detection job did not succeed ({args.changes})"])
    if args.run_compat != "true":
        return Verdict(
            0, ["no change in this pull request affects the step types check (skipped by design)"]
        )
    label = args.label_report_only == "true"
    reasons: list[str] = []
    warnings: list[str] = []
    if args.discover != "success":
        reasons.append(f"step-types discovery did not succeed ({args.discover})")
    elif not reports:
        reasons.append("discovery reported no step types")
    if args.tooling != "success":
        reasons.append(f"the check's tooling tests did not succeed ({args.tooling})")
    explained = True
    for plugin, report in reports.items():
        if report is None:
            explained = False
            reasons.append(
                f"{plugin}: the leg left no report (it failed before the driver finished, for "
                "example while building the candidate wheels, or the tooling crashed); open "
                "that leg's log"
            )
        elif report.get("result") == "fail":
            if report_only(report, label):
                source = "breaking-change label" if label else "manual run with report_only"
                warnings.append(f"{plugin}: {_word(report)}, reported only ({source})")
            else:
                reasons.append(f"{plugin}: {_word(report)}")
    if args.discover == "success" and args.compat != "success":
        # Failed legs must be explained by failing reports (handled above); a leg that
        # failed while every report passed, or without reports, still fails the gate.
        failing = [r for r in reports.values() if r is not None and r.get("result") == "fail"]
        if not (explained and failing):
            reasons.append(f"at least one step-type leg did not succeed ({args.compat})")
    if reasons:
        return Verdict(1, reasons, warnings)
    if warnings:
        return Verdict(0, [f"{len(warnings)} step type(s) failed; report-only, not failing"], warnings)
    return Verdict(0, ["every step type is compatible with the candidate SDK"])


def analysis_mix(reports: dict[str, dict[str, Any] | None]) -> str:
    """One line describing how the failing reports were classified, or ""."""
    counts = {"deterministic": 0, "inferred": 0, "unclassified": 0}
    for report in reports.values():
        if isinstance(report, dict) and report.get("result") == "fail":
            confidence = str((report.get("failure") or {}).get("confidence", ""))
            counts[confidence if confidence in counts else "unclassified"] += 1
    if not any(counts.values()):
        return ""
    parts = [f"{value} {name}" for name, value in counts.items() if value]
    line = f"failure analysis: {', '.join(parts)}"
    if counts["unclassified"]:
        line += " (failures without a rule; see the docs)"
    return line


def row(plugin: str, report: dict[str, Any] | None, label: bool) -> list[str]:
    if report is None:
        return [plugin, "no report", "-", "-", "-", "-"]
    failure = report.get("failure") or {}
    adapter = report.get("adapter") or {}
    if report.get("result") == "pass":
        result = "compatible"
    elif report_only(report, label):
        result = f"{_word(report)} (report-only)"
    else:
        result = _word(report)
    stage = f"{failure.get('index', '?')} {failure['stage']}" if failure else "-"
    analysis = (
        f"{failure.get('confidence')} {failure.get('rule_id')} {failure.get('rule_name')}"
        if failure
        else "-"
    )
    patch = f"{adapter.get('patch')} ({adapter.get('outcome')})" if adapter.get("patch") else "-"
    return [plugin, result, stage, failure.get("category", "-"), analysis, patch]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate step types compatibility reports.")
    parser.add_argument("--reports-dir", required=True)
    parser.add_argument("--plugins", default="[]", help="JSON list from the discover job")
    parser.add_argument("--changes", default="skipped")
    parser.add_argument("--run-compat", default="false")
    parser.add_argument("--discover", default="skipped")
    parser.add_argument("--tooling", default="skipped")
    parser.add_argument("--compat", default="skipped")
    parser.add_argument(
        "--label-report-only", default="false", help="'true' when the breaking-change label is set"
    )
    args = parser.parse_args(argv)
    out = Renderer()
    try:
        plugins = json.loads(args.plugins or "[]")
    except ValueError:
        plugins = []
    if not isinstance(plugins, list):
        plugins = []
    reports = load_reports(Path(args.reports_dir), [str(p) for p in plugins])
    result = verdict(args, reports)
    label = args.label_report_only == "true"

    headers = ["Step type", "Result", "Failed stage", "Category", "Analysis", "Migration patch"]
    rows = [row(plugin, report, label) for plugin, report in reports.items()]
    out.rule("Step types compatibility: summary", CYAN)
    out.line(
        f"jobs: changes={args.changes} discover={args.discover} tooling={args.tooling} "
        f"compat={args.compat} run_compat={args.run_compat} breaking-change label={label}"
    )
    if rows:
        colors = [
            GREEN if r[1] == "compatible" else (YELLOW if "report-only" in r[1] else RED)
            for r in rows
        ]
        out.table(headers, rows, row_colors=colors)
    pin = next((r.get("pin", {}) for r in reports.values() if r), {})
    if pin:
        out.line(f"step-types pin {str(pin.get('sha', ''))[:12]} ({pin.get('age_days')} days old)")
    mix = analysis_mix(reports)
    if mix:
        out.line(mix)
    for warning in result.warnings:
        out.line(out.paint(f"{out.glyphs['warn']} {warning}", YELLOW))
    if result.code:
        glyph, tone = out.glyphs["fail"], RED
    elif result.warnings:
        glyph, tone = out.glyphs["warn"], YELLOW
    else:
        glyph, tone = out.glyphs["pass"], GREEN
    for reason in result.reasons:
        out.line(out.paint(f"{glyph} {reason}", tone))
    if result.code:
        out.annotation("error", "step-types compat", "; ".join(result.reasons))
    elif result.warnings:
        out.annotation("warning", "step-types compat (report-only)", "; ".join(result.warnings))

    summary = ["## Step types compatibility\n\n"]
    if rows:
        summary.append(md_table(headers, rows))
    if pin:
        summary.append(
            f"\nstep-types pin `{str(pin.get('sha', ''))[:12]}` ({pin.get('age_days')} days old)\n"
        )
    if mix:
        summary.append(f"\n{mix}\n")
    summary.append("\n" + "".join(f"**Warning:** {warning}  \n" for warning in result.warnings))
    verdict_word = "FAIL" if result.code else ("PASS, report-only" if result.warnings else "PASS")
    summary.append("".join(f"**{verdict_word}:** {reason}  \n" for reason in result.reasons))
    append_summary("".join(summary))
    return result.code


if __name__ == "__main__":
    sys.exit(main())
