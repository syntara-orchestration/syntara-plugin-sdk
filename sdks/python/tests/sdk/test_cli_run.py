"""End-to-end behavioral checks for the ``syntara-plugin run`` command."""

from __future__ import annotations

import json
import tempfile
import uuid
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from syntara_plugin.sdk.cli import main

_STEP_SOURCE = '''"""A custom-runner Action step used by the run command tests."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, ErrorCategory, ExecutionContext, FailureCode, StepFailure


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
    ErrorCategory,
    ExecutionContext,
    FailureCode,
    StepFailure,
)


class BoomInput(BaseModel):
    title: str


class BoomOutput(BaseModel):
    slug: str


class BoomStep(ActionStep[BoomInput, BoomOutput]):
    def run(self, inputs: BoomInput, context: ExecutionContext) -> BoomOutput:
        raise StepFailure(
            1,
            "The tracker rejected the request.",
            error_message="The tracker rejected the request.",
            code=FailureCode.INVALID_INPUT.value,
            category=ErrorCategory.VALIDATION,
            retryable=False,
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
    implementation: syntara.http.request@1.0.0
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


def test_template_guidance_in_image_mode_keeps_mode_image(tmp_path: Path, capsys) -> None:
    """An author in image mode is guided into an image-mode rerun."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(["run", "--manifest", str(workspace / "plugin.yaml"), "--mode", "image"])

    assert status == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"title": "", "priority": "low"}
    suggested = next(
        line for line in captured.err.splitlines() if "--input-file input.json" in line
    )
    assert suggested.strip().startswith("syntara-plugin run")
    assert " --mode image " in suggested


def test_template_guidance_with_json_keeps_json(tmp_path: Path, capsys) -> None:
    """A --json consumer is guided into a machine-readable rerun."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(["run", "--manifest", str(workspace / "plugin.yaml"), "--json"])

    assert status == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"title": "", "priority": "low"}
    suggested = next(
        line for line in captured.err.splitlines() if "--input-file input.json" in line
    )
    assert suggested.strip().endswith("--json --input-file input.json")


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

_PREFIX_ITEMS_SOURCE = '''"""A step that reports whether its secret row arrived masked."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, ExecutionContext


class MaskProbeInput(BaseModel):
    rows: tuple[str, str]


class MaskProbeOutput(BaseModel):
    first_is_mask: bool


class MaskProbeStep(ActionStep[MaskProbeInput, MaskProbeOutput]):
    def run(self, inputs: MaskProbeInput, context: ExecutionContext) -> MaskProbeOutput:
        return MaskProbeOutput(first_is_mask=inputs.rows[0] == "***")
'''

_MASK_PROBE_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {"first_is_mask": {"type": "boolean"}},
    "required": ["first_is_mask"],
}

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


def test_prefix_items_secret_masks_the_echo_without_corrupting_the_invocation(
    tmp_path: Path, capsys
) -> None:
    """A prefixItems-declared secret masks the echo; the Step gets the real value."""

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source=_PREFIX_ITEMS_SOURCE,
        class_name="MaskProbeStep",
        input_schema={
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 2,
                    "prefixItems": [
                        {"type": "string", "writeOnly": True},
                        {"type": "string"},
                    ],
                },
            },
            "required": ["rows"],
        },
        output_schema=_MASK_PROBE_OUTPUT,
    )

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"rows": ["aaaaSECRETaaaa", "public"]}',
        ]
    )

    assert status == 0
    captured = capsys.readouterr()
    assert _report_field(captured.out, "Input") == '{"rows": ["***", "public"]}'
    assert json.loads(captured.out.split("Output:\n", 1)[1]) == {"first_is_mask": False}
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
    assert "inputs/values/0: maxLength constraint is not satisfied" in captured.err
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
    assert "inputs/note:" in captured.err
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
    """Running outside a workspace names the missing plugin.yaml and its directory."""

    monkeypatch.chdir(tmp_path)

    status = main(["run"])

    assert status == 2
    captured = capsys.readouterr()
    assert "no plugin.yaml" in captured.err
    # The location names the directory, never the bare default file name.
    assert f"no plugin.yaml in {tmp_path}" in captured.err
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
    assert "inputs/title: required property is missing" in captured.err
    assert "inputs/priority: enum constraint is not satisfied" in captured.err
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
    assert "inputs/api_token" in captured.err


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
    assert "inputs/options/value: maxLength constraint is not satisfied" in captured.err


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
    assert "inputs/title" in captured.err
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
        "syntara_plugin.sdk.container_engine.detect_engines", lambda: []
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
        "syntara_plugin.sdk.container_engine.detect_engines",
        lambda: [ContainerEngine("podman" if shutil.which("podman") else "docker")],
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
    assert f"no usable container engine has the workload image '{missing}' locally" in captured.err
    assert f"build and tag the image as '{missing}'" in captured.err
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
    assert "inputs/settings/note: maxLength constraint is not satisfied" in captured.err
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


_COMPOSITION_SOURCE = '''"""A step whose input is only schema-governed extra keys."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, ExecutionContext


class CompInput(BaseModel):
    pass


class CompOutput(BaseModel):
    slug: str


class CompStep(ActionStep[CompInput, CompOutput]):
    def run(self, inputs: CompInput, context: ExecutionContext) -> CompOutput:
        return CompOutput(slug="ok")
'''


def test_composition_declared_secrets_are_masked(tmp_path: Path, capsys) -> None:
    """A writeOnly declaration inside anyOf masks and scrubs the value.

    jsonschema nests branch errors in the composition error's context, so a
    top-level marker scan alone misses them.
    """

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source=_COMPOSITION_SOURCE,
        class_name="CompStep",
        input_schema={
            "type": "object",
            "additionalProperties": {
                "anyOf": [{"type": "string", "writeOnly": True}, {"type": "null"}]
            },
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
            '{"db": "hunter22"}',
        ]
    )

    assert status == 0
    captured = capsys.readouterr()
    assert json.loads(_report_field(captured.out, "Input")) == {"db": "***"}
    assert "hunter22" not in captured.out


def test_template_guidance_preserves_transport_options(
    tmp_path: Path, capsys
) -> None:
    """The Next command reaches the same runtime the author addressed."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--mode",
            "image",
            "--address",
            "10.0.0.5:50051",
            "--tls-ca",
            "ca crt with spaces.crt",
            "--tls-cert",
            "client.crt",
            "--tls-key",
            "client.key",
        ]
    )

    assert status == 2
    captured = capsys.readouterr()
    assert "--address 10.0.0.5:50051" in captured.err
    assert "'ca crt with spaces.crt'" in captured.err
    assert "--tls-ca 'ca crt with spaces.crt' --tls-cert client.crt --tls-key client.key" in (
        captured.err
    )


def test_json_failures_emit_one_document_on_stdout(tmp_path: Path, capsys) -> None:
    """Every failing exit path parses as one JSON document when --json is set."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    # Input validation failure (exit 2).
    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--json",
            "--input",
            "{}",
        ]
    )
    assert status == 2
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is False
    assert document["error"]["kind"] == "InvalidInput"
    assert document["error"]["detail"]

    # Usage failure from a bad flag combination (exit 2).
    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--json",
            "--mode",
            "source",
            "--address",
            "10.0.0.5:50051",
        ]
    )
    assert status == 2
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is False
    assert document["error"]["kind"] == "SettingsError"

    # Execution failure: unreachable image-mode address (exit 1).
    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--json",
            "--mode",
            "image",
            "--address",
            "127.0.0.1:59999",
            "--input",
            '{"title": "x"}',
        ]
    )
    assert status == 1
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is False
    assert document["error"]["kind"] == "RunExecutionError"

    # Missing manifest (exit 2).
    status = main(["run", "--manifest", str(tmp_path / "nowhere"), "--json"])
    assert status == 2
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is False
    assert document["error"]["kind"] == "RunUsageError"


_SHORT_SECRET_SOURCE = '''"""A step echoing a very short declared secret."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, ExecutionContext


class KeyInput(BaseModel):
    title: str
    api_key: str = ""


class KeyOutput(BaseModel):
    echo: str


class KeyStep(ActionStep[KeyInput, KeyOutput]):
    def run(self, inputs: KeyInput, context: ExecutionContext) -> KeyOutput:
        return KeyOutput(echo=f"processed key {inputs.api_key}")
'''


_KEY_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "api_key": {"type": "string", "writeOnly": True},
    },
    "required": ["title"],
}


def _write_key_workspace(root: Path, source: str) -> Path:
    """Write one workspace around a KeyStep variant with a declared api_key."""

    return _write_custom_workspace(
        root,
        source=source,
        class_name="KeyStep",
        input_schema=_KEY_INPUT_SCHEMA,
        output_schema=_ECHO_STRING_OUTPUT,
    )


def _run_input(workspace: Path, payload: str) -> int:
    """Run a workspace's target with one inline JSON input."""

    return main(["run", "--manifest", str(workspace / "plugin.yaml"), "--input", payload])


def test_short_declared_secrets_are_scrubbed_everywhere(tmp_path: Path, capsys) -> None:
    """A one-character declared secret is scrubbed from output and tracebacks."""

    workspace = _write_key_workspace(tmp_path / "ws", _SHORT_SECRET_SOURCE)

    status = _run_input(workspace, '{"title": "x", "api_key": "k3"}')

    assert status == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out.split("Output:\n", 1)[1]) == {"echo": "processed key ***"}
    assert "k3" not in captured.out


_ROOT_COMPOSITION_SOURCE = '''"""A step for root-composition schema tests."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, ExecutionContext


class AnyInput(BaseModel):
    model_config = {"extra": "allow"}
    title: str = ""


class AnyOutput(BaseModel):
    slug: str


class AnyStep(ActionStep[AnyInput, AnyOutput]):
    def run(self, inputs: AnyInput, context: ExecutionContext) -> AnyOutput:
        return AnyOutput(slug=inputs.title)
'''


@pytest.mark.parametrize("keyword", ["oneOf", "anyOf"])
def test_root_composition_errors_do_not_embed_the_instance(
    tmp_path: Path, capsys, keyword: str
) -> None:
    """Root-level oneOf/anyOf failures render a constraint-only message."""

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source=_ROOT_COMPOSITION_SOURCE,
        class_name="AnyStep",
        input_schema={
            keyword: [
                {
                    "type": "object",
                    "properties": {"payload": {"type": "string"}},
                    "required": ["payload"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {"note": {"type": "string"}},
                    "required": ["note"],
                    "additionalProperties": False,
                },
            ]
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
            '{"payload": "S3CR3T-value", "extra": true}',
        ]
    )

    assert status == 2
    captured = capsys.readouterr()
    assert f"{keyword} constraint is not satisfied" in captured.err
    assert "S3CR3T-value" not in captured.err


def test_root_not_does_not_leak_a_declared_secret(tmp_path: Path, capsys) -> None:
    """A writeOnly secret under a root `not` never reaches the message."""

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source=_ROOT_COMPOSITION_SOURCE,
        class_name="AnyStep",
        input_schema={
            "type": "object",
            "not": {
                "type": "object",
                "properties": {"blob": {"type": "string", "writeOnly": True}},
                "required": ["blob"],
            },
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
            '{"blob": "S3CR3T-value"}',
        ]
    )

    assert status == 2
    captured = capsys.readouterr()
    assert "S3CR3T-value" not in captured.err


def test_pointer_rendering_is_rfc6901(tmp_path: Path, capsys) -> None:
    """Literal dots, slashes, and tildes in property names stay unambiguous."""

    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source=_ROOT_COMPOSITION_SOURCE,
        class_name="AnyStep",
        input_schema={
            "type": "object",
            "properties": {
                "foo.bar": {"type": "integer"},
                "a/b": {"type": "integer"},
                "c~d": {"type": "integer"},
                "foo": {
                    "type": "object",
                    "properties": {"bar": {"type": "integer"}},
                },
            },
        },
        output_schema=_ECHO_STRING_OUTPUT,
    )

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"foo.bar": "x", "a/b": "x", "c~d": "x", "foo": {"bar": "x"}}',
        ]
    )

    assert status == 2
    captured = capsys.readouterr()
    lines = [line.strip() for line in captured.err.splitlines()]
    assert "inputs/a~1b: 'x' is not of type 'integer'" in lines
    assert "inputs/c~0d: 'x' is not of type 'integer'" in lines
    assert "inputs/foo.bar: 'x' is not of type 'integer'" in lines
    assert "inputs/foo/bar: 'x' is not of type 'integer'" in lines
    assert lines.count("inputs/foo.bar: 'x' is not of type 'integer'") == 1


def test_invalid_utf8_input_file_exits_two_without_a_traceback(
    tmp_path: Path, capsys
) -> None:
    """Undecodable bytes become one usage error, not a raw traceback."""

    workspace = _write_echo_workspace(tmp_path / "ws")
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'{"title": "x"}\xff')

    status = main(
        ["run", "--manifest", str(workspace / "plugin.yaml"), "--input-file", str(bad)]
    )

    assert status == 2
    captured = capsys.readouterr()
    assert "not valid UTF-8" in captured.err
    assert "Traceback" not in captured.err


def test_bom_prefixed_input_file_is_accepted(tmp_path: Path, capsys) -> None:
    """A byte-order mark is stripped instead of breaking JSON parsing."""

    workspace = _write_echo_workspace(tmp_path / "ws")
    bom_file = tmp_path / "bom.json"
    bom_file.write_bytes("﻿{\"title\": \"bom ok\"}".encode())

    status = main(
        ["run", "--manifest", str(workspace / "plugin.yaml"), "--input-file", str(bom_file)]
    )

    assert status == 0
    assert '"slug": "bom-ok"' in capsys.readouterr().out


def test_lone_surrogate_input_is_rejected(tmp_path: Path, capsys) -> None:
    """A lone surrogate cannot crash the report later; it is rejected up front."""

    workspace = _write_echo_workspace(tmp_path / "ws")

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--input",
            '{"title": "\\ud800"}',
        ]
    )

    assert status == 2
    captured = capsys.readouterr()
    assert "UTF-8 encodable" in captured.err
    assert "Traceback" not in captured.err


def test_step_stdout_does_not_pollute_the_json_contract(
    tmp_path: Path, capsys
) -> None:
    """A Step calling print() keeps stdout parseable under --json."""

    printing_source = _STEP_SOURCE.replace(
        "        return EchoOutput(slug=inputs.title.lower().replace(\" \", \"-\"))",
        "        print(\"DEBUG: step print\")\n"
        "        return EchoOutput(slug=inputs.title.lower().replace(\" \", \"-\"))",
    )
    workspace = _write_custom_workspace(
        tmp_path / "ws",
        source=printing_source,
        class_name="EchoStep",
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
            "--json",
            "--input",
            '{"title": "t"}',
        ]
    )

    assert status == 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["ok"] is True
    assert "DEBUG: step print" in captured.err


def test_mode_spies_source_image_and_address(monkeypatch, tmp_path: Path, capsys) -> None:
    """The CLI provably takes the source, managed-image, and address paths."""

    import syntara_plugin.sdk.runner as runner_module
    from syntara_plugin.sdk import container_engine

    workspace = _write_echo_workspace(tmp_path / "ws")
    manifest = str(workspace / "plugin.yaml")
    arguments_address_mode = False
    calls: dict[str, list[dict[str, Any]]] = {"source": [], "image": [], "managed": []}
    real_source = runner_module.run_source
    real_image = runner_module.run_image

    def spy_source(*args, **kwargs):
        calls["source"].append(kwargs)
        return real_source(*args, **kwargs)

    def spy_image(*args, **kwargs):
        calls["image"].append(kwargs)
        if arguments_address_mode:
            from syntara_plugin.sdk.runner import RunExecutionError

            raise RunExecutionError("spy: address path taken")
        return real_image(*args, **kwargs)

    def spy_managed(image):
        calls["managed"].append({"image": image})

    monkeypatch.setattr(runner_module, "run_source", spy_source, raising=False)
    monkeypatch.setattr(runner_module, "run_image", spy_image, raising=False)
    monkeypatch.setattr(container_engine, "managed_runtime", spy_managed, raising=False)

    # Source mode (the default): only run_source, never the image machinery.
    main(["run", "--manifest", manifest, "--input", '{"title": "x"}'])
    assert len(calls["source"]) == 1
    assert calls["image"] == []
    assert calls["managed"] == []

    # Image mode with --address: run_image directly, no managed container.
    arguments_address_mode = True
    calls.update(source=[], image=[], managed=[])
    main(
        [
            "run",
            "--manifest",
            manifest,
            "--mode",
            "image",
            "--address",
            "127.0.0.1:59999",
            "--input",
            '{"title": "x"}',
        ]
    )
    assert calls["source"] == []
    assert len(calls["image"]) == 1
    assert calls["image"][0].get("startup_seconds") is not None
    assert calls["managed"] == []


def test_invalid_input_never_invokes_the_execution_machinery(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """Invalid input stops before any execution path is entered."""

    import syntara_plugin.sdk.runner as runner_module

    workspace = _write_echo_workspace(tmp_path / "ws")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("the execution machinery must not run on invalid input")

    monkeypatch.setattr(runner_module, "run_source", fail_if_called, raising=False)
    monkeypatch.setattr(runner_module, "run_image", fail_if_called, raising=False)

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


def test_selector_actually_selects_the_target(tmp_path: Path, capsys) -> None:
    """Each selector form resolves to the target it names, not just any."""

    workspace = _write_echo_workspace(tmp_path / "ws", second_target="second")
    package = next(path for path in workspace.iterdir() if path.name.startswith("run_pkg_"))
    plugin_name = "run-fixture-" + package.name.removeprefix("run_pkg_")

    for selector, expected in (
        (f"run.{plugin_name}.echo", "echo"),
        (f"run.{plugin_name}.second", "second"),
        ("echo", "echo"),
        ("second", "second"),
    ):
        status = main(
            [
                "run",
                "--manifest",
                str(workspace / "plugin.yaml"),
                selector,
                "--input",
                '{"title": "x"}',
            ]
        )
        assert status == 0, selector
        assert _report_field(capsys.readouterr().out, "Target") == f"run.{plugin_name}.{expected}"


def test_suggested_next_commands_run_from_the_same_directory(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Every Next command the multi-target error prints actually runs."""

    workspace = _write_echo_workspace(tmp_path / "ws", second_target="second")
    monkeypatch.chdir(workspace)

    status = main(["run"])

    assert status == 2
    captured = capsys.readouterr()
    next_commands = [
        line.strip()
        for line in captured.err.splitlines()
        if line.strip().startswith("syntara-plugin run ")
    ]
    assert next_commands
    for command in next_commands:
        replay = main(command.split()[1:])
        assert replay == 2, command  # template mode from the same directory


def test_manifest_directory_uses_adjacent_settings(tmp_path: Path, capsys) -> None:
    """--manifest DIR and --manifest DIR/plugin.yaml read the same settings."""

    workspace = _write_echo_workspace(tmp_path / "ws")
    settings = workspace / "syntara-plugin.yaml"
    settings.write_text(
        """apiVersion: syntara.io/v1alpha1
kind: PluginBuildSettings
output:
  json: true
""",
        encoding="utf-8",
    )

    status = main(["run", "--manifest", str(workspace), "--input", '{"title": "x"}'])

    assert status == 0
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is True


def test_short_declared_secrets_scrub_case_insensitively(tmp_path: Path, capsys) -> None:
    """An upper-cased short declared secret is still scrubbed."""

    upper_source = _SHORT_SECRET_SOURCE.replace(
        'return KeyOutput(echo=f"processed key {inputs.api_key}")',
        'return KeyOutput(echo=f"processed key {inputs.api_key.upper()}")',
    )
    workspace = _write_key_workspace(tmp_path / "ws", upper_source)

    status = _run_input(workspace, '{"title": "x", "api_key": "k3"}')

    assert status == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out.split("Output:\n", 1)[1]) == {"echo": "processed key ***"}
    assert "K3" not in captured.out


def test_step_stdout_is_scrubbed_of_supplied_secrets(tmp_path: Path, capsys) -> None:
    """A Step printing its own secret to stdout cannot leak it to the terminal."""

    printing_source = _SHORT_SECRET_SOURCE.replace(
        'return KeyOutput(echo=f"processed key {inputs.api_key}")',
        'print(f"DEBUG: using key {inputs.api_key}")\n'
        '        return KeyOutput(echo="done")',
    )
    workspace = _write_key_workspace(tmp_path / "ws", printing_source)

    status = _run_input(workspace, '{"title": "x", "api_key": "SECRET-KEY-99"}')

    assert status == 0
    captured = capsys.readouterr()
    assert "DEBUG: using key ***" in captured.err
    assert "SECRET-KEY-99" not in captured.err
    assert "SECRET-KEY-99" not in captured.out


def test_ambiguous_target_guidance_keeps_the_tls_options(
    tmp_path: Path, capsys
) -> None:
    """A suggested run command keeps the transport options, quoted for pasting."""

    workspace = _write_echo_workspace(tmp_path / "ws", second_target="second")
    # The material must be readable so the run reaches the connection attempt;
    # the content is never validated against the unreachable host.
    material = tmp_path / "my certs"
    material.mkdir()
    for name in ("ca.crt", "client.crt", "client.key"):
        (material / name).write_text("dummy material", encoding="utf-8")

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "--mode",
            "image",
            "--address",
            "10.0.0.5:50051",
            "--tls-ca",
            str(material / "ca.crt"),
            "--tls-cert",
            str(material / "client.crt"),
            "--tls-key",
            str(material / "client.key"),
        ]
    )

    assert status == 2
    captured = capsys.readouterr()
    assert "more than one runnable target" in captured.err
    # The suggested command must name the same runtime with the same material,
    # shell-quoted so paths with spaces paste correctly.
    assert "--address 10.0.0.5:50051" in captured.err
    assert f"--tls-ca '{material}/ca.crt'" in captured.err
    assert (
        f"--tls-ca '{material}/ca.crt' --tls-cert '{material}/client.crt' "
        f"--tls-key '{material}/client.key'" in captured.err
    )


_CREDENTIAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["token"],
    "properties": {"token": {"type": "string", "minLength": 8}},
}
_PAIR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["username", "password"],
    "properties": {"username": {"type": "string"}, "password": {"type": "string"}},
}
_TOKEN = "tok-abcdef123456"
_RECIPE = "run.tracker.api-token"
_INTEGRATION = "run.tracker.connection"


def _write_credential_workspace(
    root: Path,
    *,
    integration_type: str | None = _INTEGRATION,
    credential_schema: dict[str, Any] | None = None,
    leaking: bool = False,
) -> Path:
    """Write a workspace whose Action reaches one credential recipe.

    No example plugin declares an integration type, so the run command's
    credential paths need a workspace built here.
    """

    package = f"cred_pkg_{uuid.uuid4().hex[:8]}"
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / package).mkdir(parents=True, exist_ok=True)
    (root / "plugin.yaml").write_text(
        f"""apiVersion: syntara.io/v1alpha1
kind: Plugin
metadata:
  namespace: run
  name: cred-fixture-{uuid.uuid4().hex[:8]}
  version: 0.1.0
  displayName: Credential tests
  description: Credential fixtures.
spec:
  documentation:
    path: docs/README.md
  workload:
    image: run-workload
  targets:
    - steps/echo/manifest.yaml
    - steps/connection/manifest.yaml
    - steps/recipe/manifest.yaml
""",
        encoding="utf-8",
    )
    declared = (
        f"  integrationType: {integration_type}\n" if integration_type is not None else ""
    )
    source, class_name = (
        (_LEAKING_SOURCE, "LeakStep") if leaking else (_STEP_SOURCE, "EchoStep")
    )
    output_schema = _LEAK_OUTPUT_SCHEMA if leaking else _ECHO_OUTPUT_SCHEMA
    (root / "steps/echo").mkdir(parents=True, exist_ok=True)
    (root / "steps/echo/manifest.yaml").write_text(
        f"""apiVersion: syntara.io/v1alpha1
kind: Action
metadata:
  name: echo
  displayName: Echo
  description: Fixture target.
spec:
{declared}  runtime:
    kind: custom-runner
    entryPoint: {package}.steps:{class_name}
  input:
    kind: inline
    value: {_ECHO_SCHEMA}
  output:
    kind: inline
    value: {output_schema}
  error:
    kind: inline
    value:
      type: object
""",
        encoding="utf-8",
    )
    (root / "steps/connection").mkdir(parents=True, exist_ok=True)
    (root / "steps/connection/manifest.yaml").write_text(
        f"""apiVersion: syntara.io/v1alpha1
kind: IntegrationType
metadata:
  name: connection
  displayName: Tracker connection
  description: Connects a tracker.
spec:
  id: {_INTEGRATION}
  endpointKinds: [tracker-cloud]
  credentialTypes: [{_RECIPE}]
  configuration:
    kind: inline
    value: {{type: object}}
""",
        encoding="utf-8",
    )
    (root / "steps/recipe").mkdir(parents=True, exist_ok=True)
    (root / "steps/recipe/manifest.yaml").write_text(
        f"""apiVersion: syntara.io/v1alpha1
kind: CredentialRecipe
metadata:
  name: api-token
  displayName: API token
  description: A tracker API token.
spec:
  id: {_RECIPE}
  integrationType: {_INTEGRATION}
  credentialSchema:
    kind: inline
    value: {credential_schema or _CREDENTIAL_SCHEMA}
  secretFields: [token, password]
  issuance:
    kind: static-bearer.v1
""",
        encoding="utf-8",
    )
    (root / "docs/README.md").write_text("# Credential tests\n", encoding="utf-8")
    (root / f"{package}/__init__.py").write_text("", encoding="utf-8")
    (root / f"{package}/steps.py").write_text(source, encoding="utf-8")
    return root


def _credential_run(
    workspace: Path, *source: str, extra: list[str] | None = None
) -> list[str]:
    """Build one run invocation supplying inputs and zero or more credentials."""

    arguments = [
        "run",
        "--manifest",
        str(workspace / "plugin.yaml"),
        "echo",
        "--input",
        '{"title": "Broken build"}',
    ]
    for item in source:
        arguments.extend(["--credential", item])
    return arguments + (extra or [])


def test_a_credential_from_stdin_is_delivered_and_reported_by_name(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """The primary path: the value never touches the command line or disk."""

    workspace = _write_credential_workspace(tmp_path / "ws")
    monkeypatch.setattr("sys.stdin", StringIO(f"{_TOKEN}\n"))

    status = main(_credential_run(workspace, f"{_RECIPE}=-"))

    assert status == 0
    captured = capsys.readouterr()
    assert _report_field(captured.out, "Credentials") == _RECIPE
    assert _TOKEN not in captured.out
    assert _TOKEN not in captured.err


def test_a_credential_from_an_environment_variable_never_reaches_the_step(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """env: is a convenience for the CLI, not a delivery mechanism."""

    workspace = _write_credential_workspace(tmp_path / "ws")
    monkeypatch.setenv("TRACKER_TOKEN", _TOKEN)

    status = main(_credential_run(workspace, f"{_RECIPE}=env:TRACKER_TOKEN"))

    assert status == 0
    captured = capsys.readouterr()
    assert _report_field(captured.out, "Credentials") == _RECIPE
    assert _TOKEN not in captured.out


def test_a_credential_from_a_file_outside_the_workspace_is_accepted(
    tmp_path: Path, capsys
) -> None:
    secret = tmp_path / "token"
    secret.write_text(f"{_TOKEN}\n", encoding="utf-8")
    secret.chmod(0o600)
    workspace = _write_credential_workspace(tmp_path / "ws")

    status = main(_credential_run(workspace, f"{_RECIPE}=file:{secret}"))

    assert status == 0
    captured = capsys.readouterr()
    assert _report_field(captured.out, "Credentials") == _RECIPE
    assert "readable by other local users" not in captured.err


def test_a_world_readable_credential_file_warns_without_refusing(
    tmp_path: Path, capsys
) -> None:
    """Permissions are the author's call; the risk is still worth naming."""

    secret = tmp_path / "token"
    secret.write_text(_TOKEN, encoding="utf-8")
    secret.chmod(0o644)
    workspace = _write_credential_workspace(tmp_path / "ws")

    status = main(_credential_run(workspace, f"{_RECIPE}=file:{secret}"))

    assert status == 0
    captured = capsys.readouterr()
    assert f"warning: {secret} is readable by other local users" in captured.err
    assert _TOKEN not in captured.err


def test_a_credential_file_inside_the_workspace_is_refused(tmp_path: Path, capsys) -> None:
    """A secret in the workspace can reach the artifact or version control."""

    workspace = _write_credential_workspace(tmp_path / "ws")
    secret = workspace / "token"
    secret.write_text(_TOKEN, encoding="utf-8")

    status = main(_credential_run(workspace, f"{_RECIPE}=file:{secret}"))

    assert status == 2
    captured = capsys.readouterr()
    assert "inside the plugin workspace" in captured.err
    assert _TOKEN not in captured.err


def test_an_inline_credential_value_is_refused_with_the_three_sources(
    tmp_path: Path, capsys
) -> None:
    """The same reason --input-file exists: history and /proc/<pid>/cmdline."""

    workspace = _write_credential_workspace(tmp_path / "ws")

    status = main(_credential_run(workspace, f"{_RECIPE}={_TOKEN}"))

    assert status == 2
    captured = capsys.readouterr()
    assert "does not accept an inline value" in captured.err
    assert "--credential NAME=-" in captured.err
    assert "--credential NAME=file:PATH" in captured.err
    assert "--credential NAME=env:VAR" in captured.err
    # The refused value is exactly the text that must not be echoed back.
    assert _TOKEN not in captured.err


def test_a_credential_name_outside_the_integration_type_is_refused(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    workspace = _write_credential_workspace(tmp_path / "ws")
    monkeypatch.setattr("sys.stdin", StringIO(_TOKEN))

    status = main(_credential_run(workspace, "run.tracker.other=-"))

    assert status == 2
    captured = capsys.readouterr()
    assert "is not a credential recipe reachable from run." in captured.err
    assert f"Reachable through {_INTEGRATION}: {_RECIPE}" in captured.err


def test_a_target_without_an_integration_type_reaches_no_credential(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    workspace = _write_credential_workspace(tmp_path / "ws", integration_type=None)
    monkeypatch.setattr("sys.stdin", StringIO(_TOKEN))

    status = main(_credential_run(workspace, f"{_RECIPE}=-"))

    assert status == 2
    captured = capsys.readouterr()
    assert "declares no integrationType" in captured.err


def test_a_credential_violating_its_schema_names_the_field_never_the_value(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    workspace = _write_credential_workspace(tmp_path / "ws")
    monkeypatch.setattr("sys.stdin", StringIO("short"))

    status = main(_credential_run(workspace, f"{_RECIPE}=-"))

    assert status == 2
    captured = capsys.readouterr()
    assert f"{_RECIPE}/token: minLength constraint is not satisfied" in captured.err
    assert "short" not in captured.err


def test_a_raw_value_is_wrapped_into_the_recipes_only_field(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """A single-field recipe accepts the bare token authors actually hold."""

    workspace = _write_credential_workspace(tmp_path / "ws")
    monkeypatch.setattr("sys.stdin", StringIO(_TOKEN))

    status = main(_credential_run(workspace, f"{_RECIPE}=-"))

    assert status == 0


def test_a_multi_field_recipe_requires_the_complete_document(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """There is no unambiguous field to wrap a raw value into."""

    workspace = _write_credential_workspace(tmp_path / "ws", credential_schema=_PAIR_SCHEMA)
    monkeypatch.setattr("sys.stdin", StringIO(_TOKEN))

    status = main(_credential_run(workspace, f"{_RECIPE}=-"))

    assert status == 2
    captured = capsys.readouterr()
    assert "more than one field, so a raw value cannot be wrapped" in captured.err
    assert "Declared fields: password, username" in captured.err
    assert _TOKEN not in captured.err


def test_a_multi_field_recipe_accepts_a_json_document(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    workspace = _write_credential_workspace(tmp_path / "ws", credential_schema=_PAIR_SCHEMA)
    monkeypatch.setattr(
        "sys.stdin", StringIO(json.dumps({"username": "ana", "password": _TOKEN}))
    )

    status = main(_credential_run(workspace, f"{_RECIPE}=-"))

    assert status == 0
    captured = capsys.readouterr()
    assert _report_field(captured.out, "Credentials") == _RECIPE
    assert _TOKEN not in captured.out


def test_an_unset_environment_variable_is_a_usage_error(tmp_path: Path, capsys, monkeypatch) -> None:
    workspace = _write_credential_workspace(tmp_path / "ws")
    monkeypatch.delenv("TRACKER_TOKEN", raising=False)

    status = main(_credential_run(workspace, f"{_RECIPE}=env:TRACKER_TOKEN"))

    assert status == 2
    assert "the environment variable TRACKER_TOKEN is not set" in capsys.readouterr().err


def test_an_unreadable_credential_file_is_a_usage_error(tmp_path: Path, capsys) -> None:
    workspace = _write_credential_workspace(tmp_path / "ws")
    missing = tmp_path / "absent"

    status = main(_credential_run(workspace, f"{_RECIPE}=file:{missing}"))

    assert status == 2
    assert f"the credential file {missing} could not be read" in capsys.readouterr().err


def test_an_empty_credential_source_names_no_file(tmp_path: Path, capsys) -> None:
    workspace = _write_credential_workspace(tmp_path / "ws")

    status = main(_credential_run(workspace, f"{_RECIPE}=file:"))

    assert status == 2
    assert "names no file" in capsys.readouterr().err


def test_a_credential_without_a_source_is_a_usage_error(tmp_path: Path, capsys) -> None:
    workspace = _write_credential_workspace(tmp_path / "ws")

    status = main(_credential_run(workspace, _RECIPE))

    assert status == 2
    assert "--credential expects NAME=SOURCE" in capsys.readouterr().err


def test_the_same_credential_cannot_be_supplied_twice(tmp_path: Path, capsys) -> None:
    workspace = _write_credential_workspace(tmp_path / "ws")

    status = main(
        _credential_run(workspace, f"{_RECIPE}=env:A", f"{_RECIPE}=env:B")
    )

    assert status == 2
    assert f"--credential {_RECIPE} was supplied more than once" in capsys.readouterr().err


def test_only_one_credential_can_read_standard_input(tmp_path: Path, capsys) -> None:
    """Standard input is one stream, so it can carry exactly one value."""

    workspace = _write_credential_workspace(tmp_path / "ws")

    status = main(_credential_run(workspace, f"{_RECIPE}=-", "run.tracker.other=-"))

    assert status == 2
    assert "only one credential can be read from standard input" in capsys.readouterr().err


def test_a_credential_and_the_input_cannot_both_read_standard_input(
    tmp_path: Path, capsys
) -> None:
    workspace = _write_credential_workspace(tmp_path / "ws")

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "echo",
            "--input-file",
            "-",
            "--credential",
            f"{_RECIPE}=-",
        ]
    )

    assert status == 2
    captured = capsys.readouterr()
    assert "only one of --input-file - and --credential NAME=- can read" in captured.err


def test_a_credential_is_refused_for_a_runtime_the_author_manages(
    tmp_path: Path, capsys
) -> None:
    """The CLI cannot mount anything into a container it did not start."""

    workspace = _write_credential_workspace(tmp_path / "ws")

    status = main(
        _credential_run(
            workspace,
            f"{_RECIPE}=env:TRACKER_TOKEN",
            extra=["--mode", "image", "--address", "127.0.0.1:50051"],
        )
    )

    assert status == 2
    assert "--credential cannot be used with --address" in capsys.readouterr().err


def test_template_mode_never_reads_a_credential(tmp_path: Path, capsys) -> None:
    """Nothing executes, so --credential NAME=- must not block on stdin."""

    workspace = _write_credential_workspace(tmp_path / "ws")

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "echo",
            "--credential",
            f"{_RECIPE}=-",
        ]
    )

    assert status == 2
    assert json.loads(capsys.readouterr().out) == {"title": "", "priority": "low"}


def test_a_malformed_credential_option_is_refused_in_template_mode(
    tmp_path: Path, capsys
) -> None:
    """A usage error must not be swallowed by the template path."""

    workspace = _write_credential_workspace(tmp_path / "ws")

    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            "echo",
            "--credential",
            f"{_RECIPE}={_TOKEN}",
        ]
    )

    assert status == 2
    captured = capsys.readouterr()
    assert "does not accept an inline value" in captured.err
    assert _TOKEN not in captured.err


def test_the_json_document_lists_credential_names_only(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    workspace = _write_credential_workspace(tmp_path / "ws")
    monkeypatch.setattr("sys.stdin", StringIO(_TOKEN))

    status = main(_credential_run(workspace, f"{_RECIPE}=-", extra=["--json"]))

    assert status == 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["credentials"] == [_RECIPE]
    assert _TOKEN not in captured.out


def test_a_run_without_credentials_reports_none(tmp_path: Path, capsys) -> None:
    workspace = _write_credential_workspace(tmp_path / "ws")

    status = main(_credential_run(workspace, extra=["--json"]))

    assert status == 0
    assert json.loads(capsys.readouterr().out)["credentials"] == []


def test_a_credential_a_step_leaks_is_scrubbed_everywhere(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Delivery joins the scrub set, so a leaking Step cannot print the value."""

    workspace = _write_credential_workspace(tmp_path / "ws", leaking=True)
    monkeypatch.setattr("sys.stdin", StringIO(_TOKEN))

    status = main(
        _credential_run(
            workspace,
            f"{_RECIPE}=-",
            extra=["--input", json.dumps({"title": _TOKEN, "api_token": _TOKEN * 2})],
        )
    )

    captured = capsys.readouterr()
    assert status in (0, 1)
    assert _TOKEN not in captured.out
    assert _TOKEN not in captured.err


def test_the_credential_directory_is_removed_after_the_run(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Nothing outlives the invocation: the delivery file is not a cache."""

    workspace = _write_credential_workspace(tmp_path / "ws")
    monkeypatch.setattr("sys.stdin", StringIO(_TOKEN))
    created: list[Path] = []
    original = tempfile.mkdtemp

    def recording_mkdtemp(*args: Any, **kwargs: Any) -> str:
        directory = original(*args, **kwargs)
        if str(kwargs.get("prefix", "")).startswith("syntara-run-secrets-"):
            created.append(Path(directory))
        return directory

    monkeypatch.setattr(tempfile, "mkdtemp", recording_mkdtemp)

    status = main(_credential_run(workspace, f"{_RECIPE}=-"))

    assert status == 0
    assert created and not any(directory.exists() for directory in created)


def test_a_run_without_credentials_creates_no_secret_directory(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """No credentials means no local path exception is granted at all."""

    workspace = _write_credential_workspace(tmp_path / "ws")
    created: list[str] = []
    original = tempfile.mkdtemp

    def recording_mkdtemp(*args: Any, **kwargs: Any) -> str:
        created.append(str(kwargs.get("prefix", "")))
        return original(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkdtemp", recording_mkdtemp)

    status = main(_credential_run(workspace))

    assert status == 0
    assert not [prefix for prefix in created if prefix.startswith("syntara-run-secrets-")]


def test_a_credential_file_warning_reaches_stderr_under_json(
    tmp_path: Path, capsys
) -> None:
    """stdout owes a --json consumer one document; the warning belongs on stderr."""

    secret = tmp_path / "token"
    secret.write_text(_TOKEN, encoding="utf-8")
    secret.chmod(0o644)
    workspace = _write_credential_workspace(tmp_path / "ws")

    status = main(
        _credential_run(workspace, f"{_RECIPE}=file:{secret}", extra=["--json"])
    )

    assert status == 0
    captured = capsys.readouterr()
    assert "readable by other local users" in captured.err
    # stdout stays exactly one parseable document.
    assert json.loads(captured.out)["credentials"] == [_RECIPE]
