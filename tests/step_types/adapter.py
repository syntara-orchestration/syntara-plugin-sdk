"""Mechanical migrations for misconfiguration-class breaks.

When the classifier recognises a settings change with a known migration, the
driver applies it to the throwaway step-types checkout, re-runs the stages, and
saves ``git diff`` of the plugin directory as a migration patch. Nothing is
committed or pushed, and a successful adapted run never turns the check green:
it only proves the break is migratable and hands step-types the patch.

Transforms edit YAML as text so the patch stays minimal and keeps comments.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


class NotApplicable(Exception):
    """The transform does not fit the files it was asked to change."""


@dataclass(frozen=True)
class Transform:
    """One named, mechanical migration."""

    name: str
    title: str
    caveat: str
    apply: Callable[[Path], list[str]]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def rewrite_block(
    text: str, parent: str, key: str, new_key: str, entry_id: str, drop: frozenset[str]
) -> str:
    """Turn ``parent.key: {...}`` into ``parent.new_key: {entry_id: {...}}``.

    Operates on block-style YAML: finds the top-level ``parent:`` line, the
    ``key:`` line directly under it (no inline value), and re-indents that
    block one level deeper under ``new_key:`` / ``entry_id:``. First-level
    children named in ``drop`` are removed together with their nested lines.
    Raises :class:`NotApplicable` when the shape is not found.
    """
    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if line.rstrip() == f"{parent}:" and not line[0:1].isspace()),
        None,
    )
    if start is None:
        raise NotApplicable(f"no top-level '{parent}:' block")
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].strip() and not lines[i][0].isspace() and not lines[i].lstrip().startswith("#")
        ),
        len(lines),
    )
    target = next(
        (
            i
            for i in range(start + 1, end)
            if lines[i].strip() == f"{key}:"
        ),
        None,
    )
    if target is None:
        raise NotApplicable(f"no '{key}:' block under '{parent}:'")
    base = _indent(lines[target])
    body_end = target + 1
    while body_end < end and (not lines[body_end].strip() or _indent(lines[body_end]) > base):
        body_end += 1
    # Trailing blank lines belong to the parent block, not to ``key``.
    while body_end > target + 1 and not lines[body_end - 1].strip():
        body_end -= 1
    body = lines[target + 1 : body_end]
    if not body:
        raise NotApplicable(f"'{parent}.{key}' has no nested settings")
    step = _indent(next(line for line in body if line.strip())) - base
    if step <= 0:
        raise NotApplicable(f"'{parent}.{key}' is not an indented block")
    child_indent = base + step
    kept: list[str] = []
    skipping = False
    for line in body:
        if line.strip() and _indent(line) == child_indent:
            name = line.strip().split(":", 1)[0]
            skipping = name in drop
        if not skipping:
            kept.append(" " * step + line if line.strip() else line)
    replacement = [
        " " * base + f"{new_key}:\n",
        " " * (base + step) + f"{entry_id}:\n",
        *kept,
    ]
    return "".join(lines[:target] + replacement + lines[body_end:])


def _move_json_key(text: str, parent: str, key: str, new_key: str, entry_id: str) -> str:
    try:
        document = json.loads(text)
    except ValueError as error:
        # For example YAML flow style ({spec: {...}}), which is not JSON.
        raise NotApplicable(f"plugin.yaml starts with '{{' but is not JSON ({error})") from None
    section = document.get(parent) if isinstance(document, dict) else None
    if not isinstance(section, dict) or not isinstance(section.get(key), dict):
        raise NotApplicable(f"no '{parent}.{key}' object")
    if new_key in section:
        raise NotApplicable(f"'{parent}.{new_key}' already exists")
    rebuilt = {}
    for name, value in section.items():
        if name == key:
            rebuilt[new_key] = {entry_id: value}
        else:
            rebuilt[name] = value
    document[parent] = rebuilt
    return json.dumps(document, indent=2) + "\n"


def migrate_workload_settings(plugin_dir: Path) -> list[str]:
    """Migrate the single ``workload`` settings shape to the ``workloads`` mapping.

    - ``syntara-plugin.yaml``: ``build.workload`` becomes ``build.workloads.<id>``;
      ``imageId`` is dropped because the mapping key now names the build target.
    - ``plugin.yaml``: ``spec.workload`` becomes ``spec.workloads.<id>``.

    ``<id>`` is the plugin directory name. Returns the changed file names.
    """
    entry_id = plugin_dir.name
    if not _ID.fullmatch(entry_id):
        raise NotApplicable(f"plugin directory name '{entry_id}' is not a valid workload ID")
    changed = []
    settings = plugin_dir / "syntara-plugin.yaml"
    if not settings.is_file():
        raise NotApplicable("syntara-plugin.yaml not found")
    settings.write_text(
        rewrite_block(
            settings.read_text(encoding="utf-8"),
            "build",
            "workload",
            "workloads",
            entry_id,
            frozenset({"imageId"}),
        ),
        encoding="utf-8",
    )
    changed.append(settings.name)
    manifest = plugin_dir / "plugin.yaml"
    if manifest.is_file():
        text = manifest.read_text(encoding="utf-8")
        try:
            if text.lstrip().startswith("{"):
                updated = _move_json_key(text, "spec", "workload", "workloads", entry_id)
            else:
                updated = rewrite_block(text, "spec", "workload", "workloads", entry_id, frozenset())
        except NotApplicable:
            updated = None
        if updated is not None:
            manifest.write_text(updated, encoding="utf-8")
            changed.append(manifest.name)
    return changed


TRANSFORMS: dict[str, Transform] = {
    "workload-to-workloads": Transform(
        name="workload-to-workloads",
        title="Rename the single 'workload' settings to the 'workloads' mapping",
        caveat=(
            "This covers the settings part of the migration only. The patch drops `imageId`: in "
            "the `workloads` format the link between a plugin workload and its image build moves "
            "to a RuntimeBinding manifest (`build.runtimeBindings`), which this transform does "
            "not create."
        ),
        apply=migrate_workload_settings,
    ),
}
