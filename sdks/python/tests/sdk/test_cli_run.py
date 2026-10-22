"""End-to-end behavioral checks for the ``syntara-plugin run`` command."""

from __future__ import annotations

import json
import uuid
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from syntara_plugin.sdk.cli import main

_STEP_SOURCE = '''"""A custom-runner Action step used by the run command tests."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, ExecutionContext


class EchoInput(BaseModel):
    title: str
    priority: str = "low"
    api_token: str = ""


class EchoOutput(BaseModel):
    slug: str


class EchoStep(ActionStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
        return EchoOutput(slug=inputs.title.lower().replace(" ", "-"))
'''

_FAILING_SOURCE = '''"""A custom-runner Action step that fails deliberately."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import (
    ActionStep,
    ContainerInvocationError,
    ExecutionContext,
    FailureCode,
)


class BoomInput(BaseModel):
    title: str


class BoomOutput(BaseModel):
    slug: str


class BoomStep(ActionStep[BoomInput, BoomOutput]):
    def run(self, inputs: BoomInput, context: ExecutionContext) -> BoomOutput:
        raise ContainerInvocationError(
            FailureCode.INVALID_INPUT,
            "The tracker rejected the request.",
            retryable=False,
            outcome="not_started",
        )
'''

_ECHO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "priority": {"type": "string", "default": "low", "enum": ["low", "medium", "high"]},
        "api_token": {"type": "string", "writeOnly": True, "minLength": 20},
    },
    "required": ["title"],
}
_ECHO_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"slug": {"type": "string"}},
    "required": ["slug"],
}


def _write_echo_workspace(
    root: Path,
    *,
    second_target: str | None = None,
    shared_entry_point: bool = False,
    failing: bool = False,
    raising: bool = False,
    leaking: bool = False,
    input_schema: dict[str, Any] | None = None,
) -> Path:
    """Write one compilable workspace with an importable custom-runner Step."""

    suffix = uuid.uuid4().hex[:8]
    package = f"run_pkg_{suffix}"
    plugin_name = f"run-fixture-{suffix}"
    output_schema: dict[str, Any] | None = None
    if raising:
        source, class_name, schema = _RAISING_SOURCE, "CrashStep", _ECHO_SCHEMA
    elif leaking:
        source, class_name, schema = _LEAKING_SOURCE, "LeakStep", _ECHO_SCHEMA
        output_schema = _LEAK_OUTPUT_SCHEMA
    elif failing:
        source, class_name, schema = _FAILING_SOURCE, "BoomStep", _FAILING_SCHEMA
    else:
        source, class_name, schema = _STEP_SOURCE, "EchoStep", _ECHO_SCHEMA
    if input_schema is not None:
        schema = input_schema
    second_entry_point = (
        f"{package}.steps:{class_name}"
        if shared_entry_point
        else f"{package}.more:{class_name}"
    )
    (root / "steps/echo").mkdir(parents=True, exist_ok=True)
    (root / "steps/second").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(exist_ok=True)
    (root / package).mkdir(exist_ok=True)
    targets = "    - steps/echo/manifest.yaml"
    if second_target is not None:
        targets += "\n    - steps/second/manifest.yaml"
    (root / "plugin.yaml").write_text(
        f"""apiVersion: syntara.io/v1alpha1
kind: Plugin
metadata:
  namespace: run
  name: {plugin_name}
  version: 0.1.0
  displayName: Run tests
  description: Run command fixtures.
spec:
  documentation:
    path: docs/README.md
  workload:
    image: run-workload
  targets:
{targets}
""",
        encoding="utf-8",
    )
    if second_target is not None:
        _write_target(
            root,
            "steps/second/manifest.yaml",
            name="second",
            entry_point=second_entry_point,
            input_schema=schema,
            output_schema=output_schema,
        )
        if not shared_entry_point:
            (root / f"{package}/more.py").write_text(source, encoding="utf-8")
    _write_target(
        root,
        "steps/echo/manifest.yaml",
        name="echo",
        entry_point=f"{package}.steps:{class_name}",
        input_schema=schema,
        output_schema=output_schema,
    )
    (root / "docs/README.md").write_text("# Run tests\n", encoding="utf-8")
    (root / f"{package}/__init__.py").write_text("", encoding="utf-8")
    (root / f"{package}/steps.py").write_text(source, encoding="utf-8")
    return root


_FAILING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
}


def _write_target(
    root: Path,
    relative: str,
    *,
    name: str,
    entry_point: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any] | None = None,
) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""apiVersion: syntara.io/v1alpha1
kind: Action
metadata:
  name: {name}
  displayName: {name.title()}
  description: Fixture target.
spec:
  runtime:
    kind: custom-runner
    entryPoint: {entry_point}
  input:
    kind: inline
    value: {input_schema}
  output:
    kind: inline
    value: {output_schema or _ECHO_OUTPUT_SCHEMA}
  error:
    kind: inline
    value:
      type: object
""",
        encoding="utf-8",
    )


def _write_platform_runner_workspace(root: Path) -> Path:
    """Write one workspace whose only target is declarative."""

    (root / "steps/request").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(exist_ok=True)
    (root / "plugin.yaml").write_text(
        """apiVersion: syntara.io/v1alpha1
kind: Plugin
metadata:
  namespace: run
  name: platform
  version: 0.1.0
  displayName: Platform
  description: Platform-runner fixture.
spec:
  documentation:
    path: docs/README.md
  targets:
    - steps/request/manifest.yaml
""",
        encoding="utf-8",
    )
    (root / "steps/request/manifest.yaml").write_text(
        """apiVersion: syntara.io/v1alpha1
kind: Action
metadata:
  name: request
  displayName: Request
  description: Fixture target.
spec:
  runtime:
    kind: platform-runner
    driver: http.v1
    operation:
      method: GET
      path: /request
  input:
    kind: inline
    value:
      type: object
      properties:
        name:
          type: string
      required: [name]
  output:
    kind: inline
    value:
      type: object
""",
        encoding="utf-8",
    )
    (root / "docs/README.md").write_text("# Platform fixture\n", encoding="utf-8")
    return root


def test_template_mode_prints_template_summary_and_guidance(tmp_path: Path, capsys) -> None:
    """Without input the command guides instead of executing."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(["run", "--manifest", str(workspace / "plugin.yaml")])

    assert status == 2
    captured = capsys.readouterr()
    template = json.loads(captured.out)
    assert template == {"title": "", "priority": "low"}
    assert "Inputs for run." in captured.err
    assert "api_token" in captured.err
    assert "sensitive" in captured.err
    assert "not business-valid" in captured.err
    assert "Nothing was executed" in captured.err
    assert "--input-file input.json" in captured.err


def test_template_is_deterministic_and_redirected_purely(
    tmp_path: Path, capsys
) -> None:
    """The same schema yields the identical template on stdout only."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    main(["run", "--manifest", str(workspace / "plugin.yaml")])
    first = capsys.readouterr().out
    main(["run", "--manifest", str(workspace / "plugin.yaml")])
    second = capsys.readouterr().out

    assert first == second
    assert json.loads(first) == {"title": "", "priority": "low"}


def test_template_mode_is_unchanged_by_json_flag(tmp_path: Path, capsys) -> None:
    """--json must not break `run TARGET > input.json` redirection."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(["run", "--manifest", str(workspace / "plugin.yaml"), "--json"])

    assert status == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"title": "", "priority": "low"}


def _report_field(stdout: str, label: str) -> str:
    """Extract one aligned report field value from human output."""

    line = next(line for line in stdout.splitlines() if line.startswith(f"{label}:"))
    return line.split(":", 1)[1].strip()


_RAISING_SOURCE = '''"""A custom-runner Action step that raises an unexpected exception."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, ExecutionContext


class CrashInput(BaseModel):
    title: str
    priority: str = "low"
    api_token: str = ""


class CrashOutput(BaseModel):
    slug: str


class CrashStep(ActionStep[CrashInput, CrashOutput]):
    def run(self, inputs: CrashInput, context: ExecutionContext) -> CrashOutput:
        raise RuntimeError(f"unexpected kaboom with {inputs.api_token}")
'''

_LEAKING_SOURCE = '''"""A custom-runner Action step that echoes its token into the output."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, ExecutionContext


class LeakInput(BaseModel):
    title: str
    priority: str = "low"
    api_token: str = ""


class LeakOutput(BaseModel):
    echo: str


class LeakStep(ActionStep[LeakInput, LeakOutput]):
    def run(self, inputs: LeakInput, context: ExecutionContext) -> LeakOutput:
        return LeakOutput(echo=f"processed token {inputs.api_token}")
'''

_LEAK_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"echo": {"type": "string"}},
    "required": ["echo"],
}


def _write_custom_workspace(
    root: Path,
    *,
    source: str,
    class_name: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
) -> Path:
    """Write one compilable workspace around a fully custom step module."""

    suffix = uuid.uuid4().hex[:8]
    package = f"run_pkg_{suffix}"
    plugin_name = f"run-fixture-{suffix}"
    (root / "steps/echo").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(exist_ok=True)
    (root / package).mkdir(exist_ok=True)
    (root / "plugin.yaml").write_text(
        f"""apiVersion: syntara.io/v1alpha1
kind: Plugin
metadata:
  namespace: run
  name: {plugin_name}
  version: 0.1.0
  displayName: Run tests
  description: Run command fixtures.
spec:
  documentation:
    path: docs/README.md
  workload:
    image: run-workload
  targets:
    - steps/echo/manifest.yaml
""",
        encoding="utf-8",
    )
    _write_target(
        root,
        "steps/echo/manifest.yaml",
        name="echo",
        entry_point=f"{package}.steps:{class_name}",
        input_schema=input_schema,
        output_schema=output_schema,
    )
    (root / "docs/README.md").write_text("# Run tests\n", encoding="utf-8")
    (root / f"{package}/__init__.py").write_text("", encoding="utf-8")
    (root / f"{package}/steps.py").write_text(source, encoding="utf-8")
    return root


_ECHO_STRING_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {"echo": {"type": "string"}},
    "required": ["echo"],
}

_ITEMS_SOURCE = '''"""A step that echoes the first item of a sensitive array."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, ExecutionContext


class ItemsInput(BaseModel):
    title: str
    values: list[str] = []


class ItemsOutput(BaseModel):
    echo: str


class ItemsStep(ActionStep[ItemsInput, ItemsOutput]):
    def run(self, inputs: ItemsInput, context: ExecutionContext) -> ItemsOutput:
        return ItemsOutput(echo=f"item={inputs.values[0]}")
'''

_NOTE_SOURCE = '''"""A step with a plain note field and a token field."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, ExecutionContext


class NoteInput(BaseModel):
    title: str
    note: str = ""
    api_token: str = ""


class NoteOutput(BaseModel):
    echo: str


class NoteStep(ActionStep[NoteInput, NoteOutput]):
    def run(self, inputs: NoteInput, context: ExecutionContext) -> NoteOutput:
        return NoteOutput(echo=inputs.title)
'''

_OVERLAP_SOURCE = '''"""A step that echoes two overlapping secrets."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, ExecutionContext


class OverlapInput(BaseModel):
    short: str
    long: str


class OverlapOutput(BaseModel):
    echo: str


class OverlapStep(ActionStep[OverlapInput, OverlapOutput]):
    def run(self, inputs: OverlapInput, context: ExecutionContext) -> OverlapOutput:
        return OverlapOutput(echo=f"{inputs.short}-{inputs.long}")
'''

_OUTPUT_KEY_SOURCE = '''"""A step that uses the token as an output mapping key."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, ExecutionContext


class KeyInput(BaseModel):
    api_token: str


class KeyOutput(BaseModel):
    payload: dict[str, str]


class KeyStep(ActionStep[KeyInput, KeyOutput]):
    def run(self, inputs: KeyInput, context: ExecutionContext) -> KeyOutput:
        return KeyOutput(payload={inputs.api_token: "v"})
'''


def test_sensitive_array_items_are_masked_and_scrubbed(tmp_path: Path, capsys) -> None:
    """items schemas declared writeOnly mask the echo and scrub the output."""

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source=_ITEMS_SOURCE,
        class_name="ItemsStep",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "values": {
                    "type": "array",
                    "items": {"type": "string", "writeOnly": True},
                },
            },
            "required": ["title"],
        },
        output_schema=_ECHO_STRING_OUTPUT,
    )

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"title": "x", "values": ["aaaaSECRETaaaa"]}',
        ]
    )

    assert status == 0
    captured = capsys.readouterr()
    assert _report_field(captured.out, "Input") == '{"title": "x", "values": ["***"]}'
    assert json.loads(captured.out.split("Output:\n", 1)[1]) == {"echo": "item=***"}
    assert "aaaaSECRETaaaa" not in captured.out


def test_validation_errors_for_sensitive_array_items_name_the_constraint(
    tmp_path: Path, capsys
) -> None:
    """An error at an array index still resolves the sensitive items schema."""

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source=_ITEMS_SOURCE,
        class_name="ItemsStep",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "values": {
                    "type": "array",
                    "items": {"type": "string", "writeOnly": True, "maxLength": 8},
                },
            },
            "required": ["title"],
        },
        output_schema=_ECHO_STRING_OUTPUT,
    )

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"title": "x", "values": ["hhhhSECREThhhh"]}',
        ]
    )

    assert status == 2
    captured = capsys.readouterr()
    assert "inputs.values[0]: maxLength constraint is not satisfied" in captured.err
    assert "hhhhSECREThhhh" not in captured.err


def test_undeclared_nested_objects_are_still_name_checked(tmp_path: Path, capsys) -> None:
    """A credential-like key inside an undeclared object is masked."""

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source=_NOTE_SOURCE,
        class_name="NoteStep",
        input_schema={
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
        output_schema=_ECHO_STRING_OUTPUT,
    )

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"title": "x", "config": {"api_key": "ddddSECRETdddd", "host": "visible"}}',
        ]
    )

    assert status == 0
    captured = capsys.readouterr()
    assert (
        _report_field(captured.out, "Input")
        == '{"title": "x", "config": {"api_key": "***", "host": "visible"}}'
    )


def test_secret_pasted_into_a_plain_field_is_scrubbed_from_validation_errors(
    tmp_path: Path, capsys
) -> None:
    """A token value in a non-sensitive field's error is scrubbed."""

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source=_NOTE_SOURCE,
        class_name="NoteStep",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "note": {"type": "string", "maxLength": 5},
                "api_token": {"type": "string", "writeOnly": True},
            },
            "required": ["title"],
        },
        output_schema=_ECHO_STRING_OUTPUT,
    )

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"title": "x", "note": "ghp_TooLongSecret88", "api_token": "ghp_TooLongSecret88"}',
        ]
    )

    assert status == 2
    captured = capsys.readouterr()
    assert "inputs.note:" in captured.err
    assert "'***' is too long" in captured.err
    assert "ghp_TooLongSecret88" not in captured.err


def test_secret_reused_in_a_plain_field_is_masked_in_the_echo(
    tmp_path: Path, capsys
) -> None:
    """The same value supplied as a secret is masked wherever it appears."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"title": "ghp_SameValue123XYZQQ", "api_token": "ghp_SameValue123XYZQQ"}',
        ]
    )

    assert status == 0
    captured = capsys.readouterr()
    assert json.loads(_report_field(captured.out, "Input")) == {
        "title": "***",
        "api_token": "***",
    }
    assert "ghp_SameValue123XYZQQ" not in captured.out


def test_overlapping_secrets_are_scrubbed_longest_first(tmp_path: Path, capsys) -> None:
    """A secret that is a prefix of another leaves no suffix behind."""

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source=_OVERLAP_SOURCE,
        class_name="OverlapStep",
        input_schema={
            "type": "object",
            "properties": {
                "short": {"type": "string", "writeOnly": True},
                "long": {"type": "string", "writeOnly": True},
            },
            "required": ["short", "long"],
        },
        output_schema=_ECHO_STRING_OUTPUT,
    )

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"short": "abcd", "long": "abcdefgh"}',
        ]
    )

    assert status == 0
    assert json.loads(capsys.readouterr().out.split("Output:\n", 1)[1]) == {
        "echo": "***-***"
    }


def test_output_mapping_keys_are_scrubbed(tmp_path: Path, capsys) -> None:
    """A step using the token as an output key gets the key masked."""

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source=_OUTPUT_KEY_SOURCE,
        class_name="KeyStep",
        input_schema={
            "type": "object",
            "properties": {"api_token": {"type": "string", "writeOnly": True}},
            "required": ["api_token"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "payload": {"type": "object", "additionalProperties": {"type": "string"}}
            },
            "required": ["payload"],
        },
    )

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"api_token": "ghp_OutputKeyToken99"}',
        ]
    )

    assert status == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out.split("Output:\n", 1)[1]) == {"payload": {"***": "v"}}
    assert "ghp_OutputKeyToken99" not in captured.out


def test_valid_input_executes_and_reports_the_result(tmp_path: Path, capsys) -> None:
    """Valid input reaches the Step over gRPC and prints the output."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        ["run", "--manifest", str(workspace / "plugin.yaml"), "--input", '{"title": "Broken Build"}']
    )

    assert status == 0
    captured = capsys.readouterr()
    assert _report_field(captured.out, "Target").startswith("run.")
    assert _report_field(captured.out, "Target").endswith(".echo")
    assert _report_field(captured.out, "Mode") == "source"
    assert _report_field(captured.out, "Status") == "0"
    assert json.loads(captured.out.split("Output:\n", 1)[1]) == {"slug": "broken-build"}


def test_target_selection_by_identity_entry_point_and_short_name(
    tmp_path: Path, capsys
) -> None:
    """Every documented selector resolves the same single target."""

    workspace = _write_echo_workspace(tmp_path / "ws")
    package = next(path for path in workspace.iterdir() if path.name.startswith("run_pkg_"))
    plugin_name = "run-fixture-" + package.name.removeprefix("run_pkg_")
    selectors = (
        f"run.{plugin_name}.echo",
        f"{package.name}.steps:EchoStep",
        "echo",
    )
    for selector in selectors:
        status = main(
            ["run", "--manifest", str(workspace / "plugin.yaml"), selector, "--input", '{"title": "x"}']
        )
        assert status == 0, selector


def test_target_is_inferred_when_exactly_one_runnable_target_exists(
    tmp_path: Path, capsys
) -> None:
    """Omitting the target uses the only runnable target."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        ["run", "--manifest", str(workspace / "plugin.yaml"), "--input", '{"title": "x"}']
    )

    assert status == 0
    assert _report_field(capsys.readouterr().out, "Status") == "0"


def test_multiple_runnable_targets_list_the_choices(tmp_path: Path, capsys) -> None:
    """Without a target and with several targets the command lists them."""

    workspace = _write_echo_workspace(tmp_path / "ws", second_target="second")

    status = main(["run", "--manifest", str(workspace / "plugin.yaml")])

    assert status == 2
    captured = capsys.readouterr()
    assert "more than one runnable target" in captured.err
    assert "echo" in captured.err
    assert "second" in captured.err
    assert "Nothing was executed" not in captured.err


def test_ambiguous_entry_point_lists_the_matches(tmp_path: Path, capsys) -> None:
    """Two targets sharing one entry point are listed instead of guessed."""

    workspace = _write_echo_workspace(tmp_path / "ws", second_target="second", shared_entry_point=True)
    package = next(path for path in workspace.iterdir() if path.name.startswith("run_pkg_"))

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            f"{package.name}.steps:EchoStep",
            "--input",
            '{"title": "x"}',
        ]
    )

    assert status == 2
    captured = capsys.readouterr()
    assert "matches more than one runnable target" in captured.err


def test_unknown_target_lists_the_runnable_targets(tmp_path: Path, capsys) -> None:
    """An unmatched target lists what the workspace does declare."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(["run", "--manifest", str(workspace / "plugin.yaml"), "bogus"])

    assert status == 2
    captured = capsys.readouterr()
    assert "no runnable target matches 'bogus'" in captured.err
    assert "echo" in captured.err


def test_platform_runner_target_is_rejected_with_guidance(tmp_path: Path, capsys) -> None:
    """Declarative targets explain that they cannot run locally."""

    workspace = _write_platform_runner_workspace(tmp_path / "ws")

    status = main(["run", "--manifest", str(workspace / "plugin.yaml"), "request"])

    assert status == 2
    captured = capsys.readouterr()
    assert "uses the platform-runner runtime" in captured.err
    assert "cannot run locally" in captured.err


def test_workspace_without_runnable_targets_is_rejected(tmp_path: Path, capsys) -> None:
    """A declarative-only workspace has nothing to infer."""

    workspace = _write_platform_runner_workspace(tmp_path / "ws")

    status = main(["run", "--manifest", str(workspace / "plugin.yaml")])

    assert status == 2
    assert "no custom-runner Step target" in capsys.readouterr().err


def test_missing_manifest_guidance(tmp_path: Path, capsys, monkeypatch) -> None:
    """Running outside a workspace names the missing plugin.yaml."""

    monkeypatch.chdir(tmp_path)

    status = main(["run"])

    assert status == 2
    captured = capsys.readouterr()
    assert "no plugin.yaml" in captured.err
    assert "--manifest PATH" in captured.err


def test_manifest_directory_is_accepted(tmp_path: Path, capsys) -> None:
    """--manifest accepts the workspace directory like the other commands."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        ["run", "--manifest", str(workspace), "--input", '{"title": "x"}']
    )

    assert status == 0


def test_compilation_failure_prints_diagnostics_and_next_command(
    tmp_path: Path, capsys
) -> None:
    """A broken workspace reports compiler diagnostics and suggests validate."""

    workspace = _write_echo_workspace(tmp_path / "ws")
    (workspace / "steps/echo/manifest.yaml").write_text("kind: Action\n", encoding="utf-8")

    status = main(["run", "--manifest", str(workspace / "plugin.yaml")])

    assert status == 1
    captured = capsys.readouterr()
    assert "manifest.yaml:1:1" in captured.err
    assert "Next:" in captured.err
    assert "syntara-plugin validate" in captured.err


def test_invalid_input_reports_every_pointer_error_without_executing(
    tmp_path: Path, capsys
) -> None:
    """All schema violations are listed before any server starts."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"priority": "urgent"}',
        ]
    )

    assert status == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "inputs.title: required property is missing" in captured.err
    assert "inputs.priority" in captured.err
    assert "not one of" in captured.err
    assert "Nothing was executed" in captured.err


def test_sensitive_values_are_never_echoed_in_validation_errors(
    tmp_path: Path, capsys
) -> None:
    """A sensitive field's value stays out of the error text."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"title": "x", "api_token": "short-secret-value"}',
        ]
    )

    assert status == 2
    captured = capsys.readouterr()
    assert "short-secret-value" not in captured.err
    assert "inputs.api_token" in captured.err


def test_nested_write_only_fields_are_redacted(tmp_path: Path, capsys) -> None:
    """A writeOnly field nested under a plain name is still redacted."""

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "options": {
                "type": "object",
                "properties": {"value": {"type": "string", "writeOnly": True, "maxLength": 4}},
            },
        },
        "required": ["title", "options"],
    }
    workspace = _write_echo_workspace(tmp_path / "ws", input_schema=schema)

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"title": "x", "options": {"value": "hunter2-secret"}}',
        ]
    )

    assert status == 2
    captured = capsys.readouterr()
    assert "hunter2-secret" not in captured.err
    assert "inputs.options.value: maxLength constraint is not satisfied" in captured.err


def test_aggregate_validator_messages_never_embed_input_values(
    tmp_path: Path, capsys
) -> None:
    """minProperties and friends embed the instance, so they stay sanitized."""

    schema: dict[str, Any] = {
        "type": "object",
        "minProperties": 3,
        "properties": {"title": {"type": "string"}},
    }
    workspace = _write_echo_workspace(tmp_path / "ws", input_schema=schema)

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"title": "x", "api_token": "SECRET-XYZ"}',
        ]
    )

    assert status == 2
    captured = capsys.readouterr()
    assert "SECRET-XYZ" not in captured.err
    assert "minProperties constraint is not satisfied" in captured.err


def test_deeply_nested_input_is_rejected_cleanly(tmp_path: Path, capsys) -> None:
    """A recursion-bomb JSON file exits 2 with guidance, not a traceback."""

    workspace = _write_echo_workspace(tmp_path / "ws")
    payload = '{"title": ' + "[" * 150_000 + "]" * 150_000 + "}"

    status = main(
        ["run", "--manifest", str(workspace / "plugin.yaml"), "--input", payload]
    )

    assert status == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_validation_error_lines_are_bounded(tmp_path: Path, capsys) -> None:
    """A huge embedded value cannot flood the error output."""

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source=_STEP_SOURCE,
        class_name="EchoStep",
        input_schema={
            "type": "object",
            "properties": {"title": {"type": "string", "maxLength": 5}},
            "required": ["title"],
        },
        output_schema={
            "type": "object",
            "properties": {"slug": {"type": "string"}},
            "required": ["slug"],
        },
    )
    payload = '{"title": "' + "x" * 100_000 + '"}'

    status = main(
        ["run", "--manifest", str(workspace / "plugin.yaml"), "--input", payload]
    )

    assert status == 2
    captured = capsys.readouterr()
    assert "inputs.title" in captured.err
    # The embedded 100k-character value is truncated with an ellipsis marker.
    assert captured.err.count("x") < 1_000
    assert "..." in captured.err
    assert len(captured.err) < 2_000


def test_non_object_input_is_rejected(tmp_path: Path, capsys) -> None:
    """Arrays and scalars are refused before validation."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        ["run", "--manifest", str(workspace / "plugin.yaml"), "--input", "[1, 2]"]
    )

    assert status == 2
    assert "must be a JSON object" in capsys.readouterr().err


def test_malformed_json_input_is_rejected(tmp_path: Path, capsys) -> None:
    """A parse failure names the source."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        ["run", "--manifest", str(workspace / "plugin.yaml"), "--input", "{oops"]
    )

    assert status == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_json_constants_are_rejected(tmp_path: Path, capsys) -> None:
    """NaN and Infinity never reach the container ABI encoder."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        ["run", "--manifest", str(workspace / "plugin.yaml"), "--input", '{"title": NaN}']
    )

    assert status == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_oversized_input_is_rejected(tmp_path: Path, capsys) -> None:
    """The ABI's 1 MiB bound is enforced before any execution."""

    workspace = _write_echo_workspace(tmp_path / "ws")
    payload = json.dumps({"title": "x" * 1_100_000})

    status = main(
        ["run", "--manifest", str(workspace / "plugin.yaml"), "--input", payload]
    )

    assert status == 2
    assert "too large" in capsys.readouterr().err


def test_missing_input_file_is_reported(tmp_path: Path, capsys) -> None:
    """An unreadable input file names the file."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        ["run", "--manifest", str(workspace / "plugin.yaml"), "--input-file", "nope.json"]
    )

    assert status == 2
    assert "could not read the input file" in capsys.readouterr().err


def test_input_file_reads_stdin(tmp_path: Path, capsys, monkeypatch) -> None:
    """`--input-file -` reads standard input."""

    workspace = _write_echo_workspace(tmp_path / "ws")
    monkeypatch.setattr("sys.stdin", StringIO('{"title": "Stdin Title"}'))

    status = main(
        ["run", "--manifest", str(workspace / "plugin.yaml"), "--input-file", "-"]
    )

    assert status == 0
    captured = capsys.readouterr()
    assert _report_field(captured.out, "Input") == '{"title": "Stdin Title"}'
    assert "stdin-title" in captured.out


def test_both_input_sources_are_a_usage_error(tmp_path: Path) -> None:
    """argparse enforces the documented mutual exclusion."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "run",
                "--manifest",
                str(workspace / "plugin.yaml"),
                "--input",
                "{}",
                "--input-file",
                "x.json",
            ]
        )

    assert raised.value.code == 2


def test_source_mode_rejects_transport_options(tmp_path: Path, capsys) -> None:
    """--address and --tls-* are image-mode-only."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--address",
            "127.0.0.1:1",
        ]
    )

    assert status == 2
    assert "require --mode image" in capsys.readouterr().err


def test_partial_tls_options_are_a_usage_error(tmp_path: Path, capsys) -> None:
    """TLS material is all-or-nothing, mirroring the runtime server."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--mode",
            "image",
            "--tls-ca",
            "ca.crt",
        ]
    )

    assert status == 2
    assert "must be supplied together" in capsys.readouterr().err


def test_empty_address_is_a_usage_error(tmp_path: Path, capsys) -> None:
    """An empty --address never silently dials the default endpoint."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--mode",
            "image",
            "--address",
            "",
            "--input",
            '{"title": "x"}',
        ]
    )

    assert status == 2
    assert "HOST:PORT" in capsys.readouterr().err


def test_address_with_a_scheme_is_a_usage_error(tmp_path: Path, capsys) -> None:
    """--address takes a bare HOST:PORT, not a URL."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--mode",
            "image",
            "--address",
            "http://127.0.0.1:50051",
            "--input",
            '{"title": "x"}',
        ]
    )

    assert status == 2
    assert "HOST:PORT" in capsys.readouterr().err


def test_image_mode_unreachable_address_exits_one(tmp_path: Path, capsys) -> None:
    """An unreachable runtime reports the address and the fix."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--mode",
            "image",
            "--address",
            "127.0.0.1:59999",
            "--input",
            '{"title": "x"}',
        ]
    )

    assert status == 1
    captured = capsys.readouterr()
    assert "cannot reach a container runtime at 127.0.0.1:59999" in captured.err
    assert "Next:" in captured.err


def test_deliberate_step_failure_exits_one_with_structured_error(
    tmp_path: Path, capsys
) -> None:
    """A terminal workload failure keeps its code, message, and retryability."""

    workspace = _write_echo_workspace(tmp_path / "ws", failing=True)

    status = main(
        ["run", "--manifest", str(workspace / "plugin.yaml"), "--input", '{"title": "x"}']
    )

    assert status == 1
    captured = capsys.readouterr()
    assert _report_field(captured.out, "Error") == "INVALID_INPUT (retryable: false)"
    assert _report_field(captured.out, "Message") == "The tracker rejected the request."
    assert "Error detail:" not in captured.out


def test_unexpected_exception_attaches_the_local_traceback(
    tmp_path: Path, capsys
) -> None:
    """Source mode captures the author's own traceback as the error detail."""

    workspace = _write_echo_workspace(tmp_path / "ws", raising=True)

    status = main(
        ["run", "--manifest", str(workspace / "plugin.yaml"), "--input", '{"title": "kaboom"}']
    )

    assert status == 1
    captured = capsys.readouterr()
    assert _report_field(captured.out, "Error") == "OUTCOME_UNKNOWN (retryable: false)"
    assert _report_field(captured.out, "Message") == "The plugin step did not complete."
    detail = captured.out.split("Error detail:\n", 1)[1]
    assert "Traceback (most recent call last):" in detail
    assert "RuntimeError: unexpected kaboom with" in detail


def test_secret_values_are_scrubbed_from_the_error_detail(
    tmp_path: Path, capsys
) -> None:
    """A token interpolated into an exception message never reaches the screen."""

    workspace = _write_echo_workspace(tmp_path / "ws", raising=True)

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"title": "x", "api_token": "aaaaaaaaaaaaaaaaaaaaaaaa"}',
        ]
    )

    assert status == 1
    captured = capsys.readouterr()
    detail = captured.out.split("Error detail:\n", 1)[1]
    assert "RuntimeError: unexpected kaboom with ***" in detail
    assert "aaaaaaaaaaaaaaaaaaaaaaaa" not in captured.out


def test_secret_values_are_scrubbed_from_the_output(
    tmp_path: Path, capsys
) -> None:
    """A token a step echoes into its output is masked in the result."""

    workspace = _write_echo_workspace(tmp_path / "ws", leaking=True)

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"title": "x", "api_token": "aaaaaaaaaaaaaaaaaaaaaaaa"}',
        ]
    )

    assert status == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out.split("Output:\n", 1)[1]) == {
        "echo": "processed token ***"
    }
    assert "aaaaaaaaaaaaaaaaaaaaaaaa" not in captured.out


def test_json_output_emits_one_document(tmp_path: Path, capsys) -> None:
    """--json prints exactly one machine-readable result document."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--json",
            "--input",
            '{"title": "Json Title"}',
        ]
    )

    assert status == 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["ok"] is True
    assert document["mode"] == "source"
    assert document["status"] == 0
    assert document["output"] == {"slug": "json-title"}
    assert document["error"] is None
    assert document["inputSource"] == "--input"
    assert document["input"] == {"title": "Json Title"}
    assert document["target"].endswith(".echo")
    assert document["invocationId"].startswith("run-")
    assert document["workflowRunId"].startswith("run-")


def test_input_values_are_echoed_with_sensitive_fields_masked(
    tmp_path: Path, capsys
) -> None:
    """The run echoes what executed, masking credential-like fields."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"title": "Visible Title", "api_token": "aaaaaaaaaaaaaaaaaaaaaaaa"}',
        ]
    )

    assert status == 0
    captured = capsys.readouterr()
    assert json.loads(_report_field(captured.out, "Input")) == {
        "title": "Visible Title",
        "api_token": "***",
    }
    assert "aaaaaaaaaaaaaaaaaaaaaaaa" not in captured.out


def test_json_failure_output_carries_the_error_detail(tmp_path: Path, capsys) -> None:
    """A failed run still emits one JSON document with its error."""

    workspace = _write_echo_workspace(tmp_path / "ws", failing=True)

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--json",
            "--input",
            '{"title": "x"}',
        ]
    )

    assert status == 1
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is False
    assert document["error"]["code"] == "INVALID_INPUT"
    assert document["error"]["retryable"] is False
    assert document["error"]["message"] == "The tracker rejected the request."


def test_settings_supply_the_manifest_and_json_output(tmp_path: Path, capsys) -> None:
    """run honors the documented settings precedence like every command."""

    workspace = _write_echo_workspace(tmp_path / "ws")
    settings = workspace / "syntara-plugin.yaml"
    settings.write_text(
        """apiVersion: syntara.io/v1alpha1
kind: PluginBuildSettings
manifest: plugin.yaml
output:
  json: true
""",
        encoding="utf-8",
    )

    status = main(
        ["run", "--settings", str(settings), "--input", '{"title": "Settings Title"}']
    )

    assert status == 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["output"] == {"slug": "settings-title"}


def test_json_compilation_failure_includes_the_next_command(tmp_path: Path, capsys) -> None:
    """The --json compilation payload carries diagnostics and guidance."""

    workspace = _write_echo_workspace(tmp_path / "ws")
    (workspace / "steps/echo/manifest.yaml").write_text("kind: Action\n", encoding="utf-8")

    status = main(["run", "--manifest", str(workspace / "plugin.yaml"), "--json"])

    assert status == 1
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is False
    assert document["diagnostics"]
    assert "syntara-plugin validate" in document["next"]


def test_progress_printer_is_verbose_only_and_formats_events(capsys) -> None:
    """Progress prints only in verbose human mode, one stderr line per event."""

    from types import SimpleNamespace

    from syntara_plugin.sdk.cli import _progress_printer

    quiet = _progress_printer(SimpleNamespace(json=False, verbose=False))
    machine = _progress_printer(SimpleNamespace(json=True, verbose=True))
    assert quiet is None
    assert machine is None

    printer = _progress_printer(SimpleNamespace(json=False, verbose=True))
    assert printer is not None
    printer(SimpleNamespace(state="progress", percent=42.0, message="working"))
    printer(SimpleNamespace(state="heartbeat", percent=None, message=""))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "==> progress 42% working\n==> heartbeat\n"


def test_run_quiets_grpc_core_logs_unless_configured(
    tmp_path: Path, monkeypatch
) -> None:
    """The CLI keeps the gRPC C core's INFO shutdown noise off stderr."""

    import os

    workspace = _write_echo_workspace(tmp_path / "ws")
    monkeypatch.delenv("GRPC_VERBOSITY", raising=False)

    main(["run", "--manifest", str(workspace / "plugin.yaml")])

    assert os.environ["GRPC_VERBOSITY"] == "ERROR"
    monkeypatch.setenv("GRPC_VERBOSITY", "DEBUG")
    main(["run", "--manifest", str(workspace / "plugin.yaml")])
    assert os.environ["GRPC_VERBOSITY"] == "DEBUG"


def test_report_labels_are_bold_only_on_an_interactive_terminal(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Labels bold on a TTY, plain when piped or NO_COLOR is set."""

    import sys

    workspace = _write_echo_workspace(tmp_path / "ws")
    arguments = ["run", "--manifest", str(workspace / "plugin.yaml"), "--input", '{"title": "x"}']

    main(arguments)
    assert "\033[1m" not in capsys.readouterr().out

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    main(arguments)
    bolded = capsys.readouterr().out
    assert bolded.startswith("\033[1mTarget:")
    assert "\033[0mrun." in bolded

    monkeypatch.setenv("NO_COLOR", "1")
    main(arguments)
    assert "\033[1m" not in capsys.readouterr().out


def test_json_output_is_pretty_printed(tmp_path: Path, capsys) -> None:
    """--json prints one indented document instead of a single line."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--json",
            "--input",
            '{"title": "x"}',
        ]
    )

    assert status == 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["ok"] is True
    assert captured.out.count("\n") > 5
    assert '\n  "ok": true,' in captured.out


def test_image_mode_without_an_engine_reports_both_unavailable(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """No podman and no docker produces one clear message, not a traceback."""

    workspace = _write_echo_workspace(tmp_path / "ws")
    monkeypatch.setattr(
        "syntara_plugin.sdk.container_engine.detect_engine", lambda: None
    )

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--mode",
            "image",
            "--input",
            '{"title": "x"}',
        ]
    )

    assert status == 1
    captured = capsys.readouterr()
    assert "neither podman nor docker is available" in captured.err
    assert "--address" in captured.err


def test_image_mode_with_a_missing_workload_image_reports_guidance(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """A workload image that was never built is reported with the fix."""

    import shutil

    if shutil.which("podman") is None and shutil.which("docker") is None:
        pytest.skip("a container engine is required for the missing-image check")
    from syntara_plugin.sdk.container_engine import ContainerEngine

    workspace = _write_echo_workspace(tmp_path / "ws")
    missing = f"missing-workload-{uuid.uuid4().hex[:8]}"
    manifest = workspace / "plugin.yaml"
    manifest.write_text(
        manifest.read_text().replace("image: run-workload", f"image: {missing}")
    )
    monkeypatch.setattr(
        "syntara_plugin.sdk.container_engine.detect_engine",
        lambda: ContainerEngine("podman" if shutil.which("podman") else "docker"),
    )

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--mode",
            "image",
            "--input",
            '{"title": "x"}',
        ]
    )

    assert status == 1
    captured = capsys.readouterr()
    assert f"the workload image '{missing}' is not available locally" in captured.err
    assert "--address" in captured.err


def test_image_mode_without_a_declared_workload_image_reports_guidance(
    tmp_path: Path, capsys
) -> None:
    """The compiler rejects a custom-runner workspace with no workload image."""

    workspace = _write_echo_workspace(tmp_path / "ws")
    manifest = workspace / "plugin.yaml"
    manifest.write_text(
        manifest.read_text().replace("  workload:\n    image: run-workload\n", "")
    )

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--mode",
            "image",
            "--input",
            '{"title": "x"}',
        ]
    )

    assert status == 1
    captured = capsys.readouterr()
    assert "WORKLOAD_MISSING" in captured.err
    assert "spec.workload.image" in captured.err
    assert "syntara-plugin validate" in captured.err


_ESCAPING_SOURCE = '''"""A step whose failure message carries terminal escapes."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import (
    ActionStep,
    ContainerInvocationError,
    ExecutionContext,
    FailureCode,
)


class EscInput(BaseModel):
    title: str


class EscOutput(BaseModel):
    slug: str


class EscStep(ActionStep[EscInput, EscOutput]):
    def run(self, inputs: EscInput, context: ExecutionContext) -> EscOutput:
        raise ContainerInvocationError(
            FailureCode.INVALID_INPUT,
            "tracker \\x1b]0;PWNED\\x07 rejected",
            retryable=False,
            outcome="not_started",
        )
'''


def test_terminal_control_characters_are_stripped_from_schema_text(
    tmp_path: Path, capsys
) -> None:
    """A manifest cannot inject ANSI/OSC sequences through free-form text."""

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source=_STEP_SOURCE,
        class_name="EchoStep",
        input_schema={"type": "object", "properties": {"title": {"type": "string"}}},
        output_schema=_ECHO_STRING_OUTPUT,
    )
    package = next(path for path in workspace.iterdir() if path.name.startswith("run_pkg_"))
    # YAML double-quoted scalars decode \x1b into a real escape byte, which is
    # what a malicious manifest would carry.
    (workspace / "steps/echo/manifest.yaml").write_text(
        f"""apiVersion: syntara.io/v1alpha1
kind: Action
metadata:
  name: echo
  displayName: Echo
  description: Fixture target.
spec:
  runtime:
    kind: custom-runner
    entryPoint: {package.name}.steps:EchoStep
  input:
    kind: inline
    value:
      type: object
      properties:
        title:
          type: string
          description: "see \\x1b[31mRED\\x1b[0m and \\x1b]0;PWNED\\x07 osc"
      required: [title]
  output:
    kind: inline
    value:
      type: object
      properties:
        slug:
          type: string
      required: [slug]
  error:
    kind: inline
    value:
      type: object
""",
        encoding="utf-8",
    )

    status = main(["run", "--manifest", str(workspace / "plugin.yaml")])

    assert status == 2
    captured = capsys.readouterr()
    assert "\x1b" not in captured.err
    assert "\x1b" not in captured.out
    assert "\x07" not in captured.err
    # The escape bytes are gone; the surrounding text remains readable.
    assert "see [31mRED[0m and" in captured.err
    assert "PWNED" in captured.err


def test_terminal_control_characters_are_stripped_from_failure_messages(
    tmp_path: Path, capsys
) -> None:
    """A runtime failure message cannot inject terminal escapes either."""

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source=_ESCAPING_SOURCE,
        class_name="EscStep",
        input_schema={
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
        output_schema={
            "type": "object",
            "properties": {"slug": {"type": "string"}},
            "required": ["slug"],
        },
    )

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"title": "x"}',
        ]
    )

    assert status == 1
    captured = capsys.readouterr()
    assert "\x1b" not in captured.out
    assert "\x07" not in captured.out
    assert "PWNED" in captured.out
    assert "rejected" in captured.out


def test_broken_step_module_produces_guidance_not_a_traceback(
    tmp_path: Path, capsys
) -> None:
    """A syntax error in the step module becomes guidance, not a traceback."""

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source="def broken(:\n",
        class_name="BrokenStep",
        input_schema={"type": "object", "properties": {"title": {"type": "string"}}},
        output_schema=_ECHO_STRING_OUTPUT,
    )

    status = main(
        ["run", "--manifest", str(workspace / "plugin.yaml"), "--input", '{"title": "x"}']
    )

    assert status == 1
    captured = capsys.readouterr()
    assert "could not be loaded: SyntaxError" in captured.err
    assert "steps.py" in captured.err
    assert "Traceback" not in captured.err


def test_step_module_import_time_raise_reports_a_clean_error(
    tmp_path: Path, capsys
) -> None:
    """A module-level exception becomes guidance with its file and line."""

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source='raise RuntimeError("ISSUE_TRACKER_URL is not set")\n',
        class_name="BrokenStep",
        input_schema={"type": "object", "properties": {"title": {"type": "string"}}},
        output_schema=_ECHO_STRING_OUTPUT,
    )

    status = main(
        ["run", "--manifest", str(workspace / "plugin.yaml"), "--input", '{"title": "x"}']
    )

    assert status == 1
    captured = capsys.readouterr()
    assert "could not be loaded: RuntimeError: ISSUE_TRACKER_URL is not set" in captured.err
    assert "steps.py" in captured.err
    assert "Traceback" not in captured.err


def test_manifest_path_may_reference_a_parent_directory(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """A parent-relative --manifest works like it does for validate and build."""

    _write_echo_workspace(tmp_path / "ws")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    status = main(["run", "--manifest", "../ws/plugin.yaml", "--input", '{"title": "x"}'])

    assert status == 0
    assert _report_field(capsys.readouterr().out, "Status") == "0"


_SETTINGS_SOURCE = '''"""A step that echoes one value from a settings map."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, ExecutionContext


class SettingsInput(BaseModel):
    title: str
    settings: dict[str, str] = {}


class SettingsOutput(BaseModel):
    echo: str


class SettingsStep(ActionStep[SettingsInput, SettingsOutput]):
    def run(self, inputs: SettingsInput, context: ExecutionContext) -> SettingsOutput:
        return SettingsOutput(echo=inputs.settings.get("note", ""))
'''


def test_additional_properties_secrets_are_masked_and_scrubbed(
    tmp_path: Path, capsys
) -> None:
    """A map declared secret through additionalProperties is redacted."""

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source=_SETTINGS_SOURCE,
        class_name="SettingsStep",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "settings": {
                    "type": "object",
                    "additionalProperties": {"type": "string", "writeOnly": True},
                },
            },
            "required": ["title"],
        },
        output_schema=_ECHO_STRING_OUTPUT,
    )

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"title": "x", "settings": {"note": "SUPERSECRETVALUE1"}}',
        ]
    )

    assert status == 0
    captured = capsys.readouterr()
    assert json.loads(_report_field(captured.out, "Input")) == {
        "title": "x",
        "settings": {"note": "***"},
    }
    assert json.loads(captured.out.split("Output:\n", 1)[1]) == {"echo": "***"}
    assert "SUPERSECRETVALUE1" not in captured.out


def test_pattern_properties_and_ref_declared_secrets_are_masked(
    tmp_path: Path, capsys
) -> None:
    """patternProperties and $ref secret declarations are honored too."""

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source=_SETTINGS_SOURCE,
        class_name="SettingsStep",
        input_schema={
            "$defs": {"secretText": {"type": "string", "writeOnly": True}},
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "settings": {
                    "type": "object",
                    "additionalProperties": {"$ref": "#/$defs/secretText"},
                },
            },
            "required": ["title"],
        },
        output_schema=_ECHO_STRING_OUTPUT,
    )

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"title": "x", "settings": {"note": "REFSECRETVALUE2"}}',
        ]
    )

    assert status == 0
    captured = capsys.readouterr()
    assert json.loads(_report_field(captured.out, "Input")) == {
        "title": "x",
        "settings": {"note": "***"},
    }
    assert json.loads(captured.out.split("Output:\n", 1)[1]) == {"echo": "***"}
    assert "REFSECRETVALUE2" not in captured.out


def test_validation_errors_under_declared_secret_maps_name_the_constraint(
    tmp_path: Path, capsys
) -> None:
    """An error inside a secret map does not echo the supplied value."""

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source=_SETTINGS_SOURCE,
        class_name="SettingsStep",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "settings": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "string",
                        "writeOnly": True,
                        "maxLength": 8,
                    },
                },
            },
            "required": ["title"],
        },
        output_schema=_ECHO_STRING_OUTPUT,
    )

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"title": "x", "settings": {"note": "LONGSECRETVALUE3"}}',
        ]
    )

    assert status == 2
    captured = capsys.readouterr()
    assert "inputs.settings.note: maxLength constraint is not satisfied" in captured.err
    assert "LONGSECRETVALUE3" not in captured.err


def test_root_level_declared_secret_masks_the_whole_input(
    tmp_path: Path, capsys
) -> None:
    """A writeOnly declaration on the schema root marks the entire input secret.

    The natural way to declare the whole input secret must not crash the run
    (an earlier marker-path bug raised IndexError on the empty path) and must
    mask every top-level value in the echo while the Step still receives the
    real values.
    """

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source=_STEP_SOURCE,
        class_name="EchoStep",
        input_schema={
            "type": "object",
            "writeOnly": True,
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
        output_schema={
            "type": "object",
            "properties": {"slug": {"type": "string"}},
            "required": ["slug"],
        },
    )

    status = main(
        ["run", "--manifest", str(workspace / "plugin.yaml"), "--input", '{"title": "Secret Ish"}']
    )

    assert status == 0
    captured = capsys.readouterr()
    assert _report_field(captured.out, "Input") == '{"title": "***"}'
    # The Step received the real value (the slug is derived from it); only the
    # exact secret value itself would be scrubbed from the output.
    assert json.loads(captured.out.split("Output:\n", 1)[1]) == {"slug": "secret-ish"}
