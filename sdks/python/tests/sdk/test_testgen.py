"""Tests for the test-init scaffolding generator and CLI command."""

from __future__ import annotations

from pathlib import Path

import pytest
from syntara_plugin.sdk.cli import main
from syntara_plugin.sdk.testgen import (
    _TEMPLATE,
    ScaffoldError,
    _render_cases,
    generate_step_tests,
)

STEP_SOURCE = '''"""A custom-runner Action step type used by the generated tests."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, ExecutionContext


class CreateInput(BaseModel):
    title: str
    priority: str = "low"


class CreateOutput(BaseModel):
    slug: str


class CreateStep(ActionStep[CreateInput, CreateOutput]):
    def run(self, inputs: CreateInput, context: ExecutionContext) -> CreateOutput:
        return CreateOutput(slug=inputs.title.lower().replace(" ", "-"))
'''

INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "priority": {"type": "string", "default": "low", "enum": ["low", "medium", "high"]},
    },
    "required": ["title"],
}
OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"slug": {"type": "string"}},
    "required": ["slug"],
}


def _write_workspace(root: Path, *, input_schema: dict | None = None) -> Path:
    """Write one compilable workspace whose Action has a real importable step."""

    (root / "steps/create").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(exist_ok=True)
    (root / "gen_pkg").mkdir(exist_ok=True)
    (root / "plugin.yaml").write_text(
        """apiVersion: syntara.io/v1alpha1
kind: Plugin
metadata:
  namespace: gen
  name: gen-plugin
  version: 0.1.0
  displayName: Gen
  description: Generated tests.
spec:
  documentation:
    path: docs/README.md
  workloads:
    gen-workload:
      image: gen-workload
  targets:
    - steps/create/manifest.yaml
""",
        encoding="utf-8",
    )
    effective_input_schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", **(input_schema or INPUT_SCHEMA)}
    (root / "steps/create/manifest.yaml").write_text(
        f"""apiVersion: syntara.io/v1alpha1
kind: Action
metadata:
  name: create
  displayName: Create
  description: Creates.
spec:
  runtime:
    kind: custom-runner
    entryPoint: gen_pkg.steps:CreateStep
  input:
    kind: inline
    value: {effective_input_schema}
  output:
    kind: inline
    value: {OUTPUT_SCHEMA}
  error:
    kind: inline
    value:
      $schema: https://json-schema.org/draft/2020-12/schema
      type: object
""",
        encoding="utf-8",
    )
    (root / "docs/README.md").write_text("# Gen\n", encoding="utf-8")
    (root / "gen_pkg/__init__.py").write_text("", encoding="utf-8")
    (root / "gen_pkg/steps.py").write_text(STEP_SOURCE, encoding="utf-8")
    return root


def _write_workspace_for_kind(root: Path, kind: str) -> Path:
    """Write the same minimal workspace with a non-Action step type kind."""

    workspace = _write_workspace(root)
    manifest_path = workspace / "steps/create/manifest.yaml"
    manifest = manifest_path.read_text(encoding="utf-8").replace(
        "kind: Action\n", f"kind: {kind}\n"
    )
    if kind == "Control":
        manifest = manifest.replace(
            "spec:\n  runtime:", "spec:\n  invocation:\n    kind: orchestration\n  runtime:"
        )
    else:
        manifest = manifest.replace(
            "spec:\n  runtime:",
            "spec:\n  driver: webhook.v1\n"
            "  configuration:\n"
            "    kind: inline\n"
            "    value:\n"
            "      $schema: https://json-schema.org/draft/2020-12/schema\n"
            "      type: object\n"
            "  runtime:",
        )
    manifest_path.write_text(manifest, encoding="utf-8")
    (workspace / "gen_pkg/steps.py").write_text(
        STEP_SOURCE.replace("ActionStep", f"{kind}Step"), encoding="utf-8"
    )
    return workspace


def test_generate_creates_a_test_module_and_import_shim(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path / "gen")
    written = generate_step_tests(workspace / "plugin.yaml")
    names = {path.name for path in written}
    assert names == {"test_create.py", "conftest.py"}
    content = (workspace / "tests/test_create.py").read_text(encoding="utf-8")
    assert "ENTRY_POINT = 'gen_pkg.steps:CreateStep'" in content
    assert "'title': 'example-title'" in content
    assert "step_bindings.bind_step" in content


@pytest.mark.parametrize("kind", ["Control", "Trigger"])
def test_generate_covers_every_custom_runner_step_kind(tmp_path: Path, kind: str) -> None:
    workspace = _write_workspace_for_kind(tmp_path / kind.lower(), kind)
    generate_step_tests(workspace / "plugin.yaml")
    content = (workspace / "tests/test_create.py").read_text(encoding="utf-8")
    assert f"{kind} step type" in content
    assert "step_bindings.bind_step" in content


def test_generation_is_deterministic(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path / "gen")
    generate_step_tests(workspace / "plugin.yaml")
    before = (workspace / "tests/test_create.py").read_bytes()
    generate_step_tests(workspace / "plugin.yaml", force=True)
    assert (workspace / "tests/test_create.py").read_bytes() == before


def test_generate_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path / "gen")
    generate_step_tests(workspace / "plugin.yaml")
    with pytest.raises(ScaffoldError, match="refusing to overwrite"):
        generate_step_tests(workspace / "plugin.yaml")


def test_generate_rejects_a_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(ScaffoldError, match="plugin manifest not found"):
        generate_step_tests(tmp_path / "nowhere")


def test_generate_rejects_workspaces_without_custom_runner_steps(
    tmp_path: Path,
) -> None:
    workspace = _write_workspace(tmp_path / "gen")
    (workspace / "steps/create/manifest.yaml").write_text(
        """apiVersion: syntara.io/v1alpha1
kind: Action
metadata:
  name: create
  displayName: Create
  description: Creates.
spec:
  runtime:
    kind: platform-runner
    implementation: syntara.http.request@1.0.0
    operation:
      method: GET
      path: /items
      responseMap:
        kind: inline
        value:
          id:
            from: /id
  input:
    kind: inline
    value:
      $schema: https://json-schema.org/draft/2020-12/schema
      type: object
  output:
    kind: inline
    value:
      $schema: https://json-schema.org/draft/2020-12/schema
      type: object
""",
        encoding="utf-8",
    )
    with pytest.raises(ScaffoldError, match="no custom-runner step type targets"):
        generate_step_tests(workspace / "plugin.yaml")


def test_cli_test_init_creates_and_reports_tests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _write_workspace(tmp_path / "gen")
    assert main(["test-init", str(workspace)]) == 0
    captured = capsys.readouterr().out
    assert "Created" in captured
    assert "test_create.py" in captured
    assert (workspace / "tests/test_create.py").is_file()


def _add_go_target(workspace: Path) -> None:
    """Add a second custom-runner target implemented outside Python."""

    _add_target_with_entry_point(workspace, "github.com/acme/gen/handlers.Normalize")


def _add_target_with_entry_point(workspace: Path, entry_point: str) -> None:
    """Add a second custom-runner target declaring the given entry point."""

    plugin = workspace / "plugin.yaml"
    plugin.write_text(
        plugin.read_text(encoding="utf-8").replace(
            "    - steps/create/manifest.yaml",
            "    - steps/create/manifest.yaml\n    - steps/normalize/manifest.yaml",
        ),
        encoding="utf-8",
    )
    source = (workspace / "steps/create/manifest.yaml").read_text(encoding="utf-8")
    (workspace / "steps/normalize").mkdir()
    (workspace / "steps/normalize/manifest.yaml").write_text(
        source.replace("name: create", "name: normalize").replace(
            "gen_pkg.steps:CreateStep", entry_point
        ),
        encoding="utf-8",
    )


def test_generate_selects_only_targets_that_load_as_python(tmp_path: Path) -> None:
    """Selection is by load attempt, never by syntax: a target whose entry
    point does not resolve to a Python step type gets no module and is
    reported with its reason."""
    workspace = _write_workspace(tmp_path / "gen")
    _add_go_target(workspace)
    skipped: list[tuple[str, str, str]] = []

    written = generate_step_tests(workspace / "plugin.yaml", on_skip=lambda *item: skipped.append(item))

    assert workspace / "tests/test_create.py" in written
    assert not (workspace / "tests/test_normalize.py").exists()
    assert len(skipped) == 1
    identity, entry_point, reason = skipped[0]
    assert identity == "gen.gen-plugin.normalize"
    assert entry_point == "github.com/acme/gen/handlers.Normalize"
    assert "is not a Python step type" in reason


def test_generate_reports_a_module_class_shaped_collision_without_a_module(
    tmp_path: Path,
) -> None:
    """A non-Python module:Class-shaped id whose module is absent is reported
    and generates nothing, instead of erroring at collection."""
    workspace = _write_workspace(tmp_path / "gen")
    _add_target_with_entry_point(workspace, "handlers:NormalizeRecords")
    skipped: list[tuple[str, str, str]] = []

    written = generate_step_tests(workspace / "plugin.yaml", on_skip=lambda *item: skipped.append(item))

    assert workspace / "tests/test_create.py" in written
    assert not (workspace / "tests/test_normalize.py").exists()
    assert skipped[0][1] == "handlers:NormalizeRecords"
    assert "is not a Python step type in this workspace" in skipped[0][2]


def test_generate_reports_a_stdlib_colliding_entry_point_without_a_module(
    tmp_path: Path,
) -> None:
    """A non-Python id whose module happens to be importable (the stdlib
    collision) is reported for its real failure, the missing class, and
    generates nothing, so test collection can never fail on it."""
    workspace = _write_workspace(tmp_path / "gen")
    _add_target_with_entry_point(workspace, "json:NoSuchStep")
    skipped: list[tuple[str, str, str]] = []

    generate_step_tests(workspace / "plugin.yaml", on_skip=lambda *item: skipped.append(item))

    assert (workspace / "tests/test_create.py").is_file()
    assert not (workspace / "tests/test_normalize.py").exists()
    assert "has no class" in skipped[0][2]


def test_generate_reports_a_missing_python_implementation_loudly(tmp_path: Path) -> None:
    """A Python target whose module is missing (a typo) is reported with the
    import error and generates nothing: the failure is visible at generation,
    never a silent skip."""
    workspace = _write_workspace(tmp_path / "gen")
    _add_target_with_entry_point(workspace, "gen_pkg.stpes:CreateStep")
    skipped: list[tuple[str, str, str]] = []

    generate_step_tests(workspace / "plugin.yaml", on_skip=lambda *item: skipped.append(item))

    assert (workspace / "tests/test_create.py").is_file()
    assert not (workspace / "tests/test_normalize.py").exists()
    assert "No module named" in skipped[0][2]


def test_generate_writes_no_guard_for_an_in_workspace_python_target(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path / "gen")

    generate_step_tests(workspace / "plugin.yaml")

    content = (workspace / "tests/test_create.py").read_text(encoding="utf-8")
    assert "NotPythonStepTypeError" not in content
    assert "load_step_class" not in content


def test_generated_module_fails_when_a_python_module_is_deleted_after_generation(
    pytester: pytest.Pytester,
) -> None:
    _write_workspace(pytester.path)
    generate_step_tests(pytester.path / "plugin.yaml")
    # The module loaded at generation time, so no guard exists: deleting it
    # afterwards is Python breakage and must fail.
    (pytester.path / "gen_pkg/steps.py").unlink()

    result = pytester.runpytest_subprocess("tests")

    outcomes = result.parseoutcomes()
    # The invalid-input test must fail on the missing module, not skip: the
    # two placeholder-dependent skips are unrelated.
    assert outcomes.get("failed", 0) + outcomes.get("errors", 0) > 0
    assert outcomes.get("skipped", 0) < 3


def test_generated_entry_point_round_trips_opaque_characters() -> None:
    """The widened schema admits quotes and backslashes; the rendered template
    must compile and carry the exact entry-point value (a bare interpolated
    string literal would mangle it or break collection)."""
    import ast

    for entry_point in ['say"hi"', "bin\\nhandlers.js#run", "back\\slash"]:
        source = _TEMPLATE.format(
            identity="example.plugin.echo",
            kind="Action",
            manifest="plugin.yaml",
            entry_point=entry_point,
            workspace_relative="..",
            valid_cases=_render_cases([{"message": "hi"}]),
            invalid_cases=_render_cases([{"message": ""}]),
            valid_guard="",
            invalid_guard="",
        )
        compile(source, "test_echo.py", "exec")  # must not raise
        tree = ast.parse(source)
        (assign,) = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign) and node.targets[0].id == "ENTRY_POINT"
        ]
        assert ast.literal_eval(assign.value) == entry_point


def test_cli_test_init_reports_targets_that_do_not_load_as_python(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _write_workspace(tmp_path / "gen")
    _add_go_target(workspace)

    assert main(["test-init", str(workspace)]) == 0
    captured = capsys.readouterr()
    assert "test_create.py" in captured.out
    assert not (workspace / "tests/test_normalize.py").exists()
    assert (
        "Skipped gen.gen-plugin.normalize: cannot load entry point"
        " 'github.com/acme/gen/handlers.Normalize' as a Python step type"
    ) in captured.err
    assert "test it with that language's tooling" in captured.err


def test_cli_test_init_refuses_a_workspace_where_no_target_loads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _write_workspace(tmp_path / "gen")
    manifest = workspace / "steps/create/manifest.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("gen_pkg.steps:CreateStep", "cmd/create#run"),
        encoding="utf-8",
    )

    assert main(["test-init", str(workspace)]) == 2
    captured = capsys.readouterr()
    assert "cannot load entry point 'cmd/create#run' as a Python step type" in captured.err
    assert "no custom-runner step type target that loads as a Python step type" in captured.err
    assert not (workspace / "tests").exists()


def test_cli_test_init_refuses_to_overwrite(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path / "gen")
    assert main(["test-init", str(workspace)]) == 0
    assert main(["test-init", str(workspace)]) == 2


def test_cli_test_init_force_overwrites(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path / "gen")
    assert main(["test-init", str(workspace)]) == 0
    assert main(["test-init", str(workspace), "--force"]) == 0


def test_cli_test_init_rejects_a_missing_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["test-init", str(tmp_path / "nowhere")]) == 2
    assert "error:" in capsys.readouterr().err


def test_traversal_in_supplied_paths_is_rejected(tmp_path: Path) -> None:
    """Caller-supplied paths are sanitized before any file is written."""

    workspace = _write_workspace(tmp_path / "gen")
    with pytest.raises(ScaffoldError, match="path traversal"):
        generate_step_tests(workspace / ".." / "gen" / "plugin.yaml")
    with pytest.raises(ScaffoldError, match="path traversal"):
        generate_step_tests(
            workspace / "plugin.yaml", tests_dir=Path("../outside")
        )


def test_relative_manifest_paths_do_not_double_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative manifest path must not prepend the workspace twice."""

    workspace = _write_workspace(tmp_path / "gen")
    monkeypatch.chdir(workspace)
    generate_step_tests(Path("plugin.yaml"))
    assert (workspace / "tests/test_create.py").is_file()
    assert not (workspace / "gen/tests/test_create.py").exists()
    assert not (workspace / "plugin.yaml/tests").exists()


def test_hyphenated_target_names_slug_to_underscores(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path / "gen")
    action = workspace / "steps/create/manifest.yaml"
    action.write_text(
        action.read_text(encoding="utf-8").replace("name: create", "name: send-note"),
        encoding="utf-8",
    )
    written = generate_step_tests(workspace)
    assert any(path.name == "test_send_note.py" for path in written)


def test_generated_tests_pass_in_a_fresh_project(pytester: pytest.Pytester) -> None:
    _write_workspace(pytester.path)
    generate_step_tests(pytester.path / "plugin.yaml")
    result = pytester.runpytest_subprocess("tests")
    # The invalid-input test runs immediately; the placeholder-dependent
    # valid-input and equivalence tests skip until the author supplies a
    # business-valid payload, so a generated suite never fails on first run.
    result.assert_outcomes(passed=1, skipped=2)


def test_generated_tests_pass_from_a_nested_tests_dir(
    pytester: pytest.Pytester,
) -> None:
    _write_workspace(pytester.path)
    generate_step_tests(pytester.path / "plugin.yaml", tests_dir=pytester.path / "tests/unit")
    result = pytester.runpytest_subprocess("tests/unit")
    result.assert_outcomes(passed=1, skipped=2)


def test_pattern_constrained_fields_skip_with_a_todo(tmp_path: Path) -> None:
    workspace = _write_workspace(
        tmp_path / "gen",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "pattern": "^[A-Z]{3}-[0-9]{4}$"},
            },
            "required": ["title"],
        },
    )
    generate_step_tests(workspace / "plugin.yaml")
    content = (workspace / "tests/test_create.py").read_text(encoding="utf-8")
    assert "@pytest.mark.skip" in content
    assert "TODO: replace the scaffolded VALID_CASES" in content
    assert "does not match" in content


def test_format_constrained_fields_get_valid_placeholders(tmp_path: Path) -> None:
    workspace = _write_workspace(
        tmp_path / "gen",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "format": "date-time"},
            },
            "required": ["title"],
        },
    )
    generate_step_tests(workspace / "plugin.yaml")
    content = (workspace / "tests/test_create.py").read_text(encoding="utf-8")
    # The skip marker is always present (business validity is the author's to
    # confirm), but a format-satisfying placeholder carries no schema errors.
    assert "@pytest.mark.skip" in content
    assert "'title': '2026-01-01T12:00:00Z'" in content
    assert "schema validation reported" not in content


def test_union_typed_fields_skip_valid_and_drop_accepted_wrong_types(
    tmp_path: Path,
) -> None:
    workspace = _write_workspace(
        tmp_path / "gen",
        input_schema={
            "type": "object",
            "properties": {"count": {"type": ["integer", "null"]}},
            "required": ["count"],
        },
    )
    generate_step_tests(workspace / "plugin.yaml")
    content = (workspace / "tests/test_create.py").read_text(encoding="utf-8")
    # The placeholder cannot satisfy the union, so the valid tests skip.
    assert "@pytest.mark.skip" in content
    # A wrong-typed value the union actually accepts is dropped rather than
    # generating an invalid case that fails the generated test.
    assert "'count': 12345" not in content


def test_propertyless_schemas_mark_the_invalid_test_as_underived(
    tmp_path: Path,
) -> None:
    workspace = _write_workspace(
        tmp_path / "gen", input_schema={"type": "object"}
    )
    generate_step_tests(workspace / "plugin.yaml")
    content = (workspace / "tests/test_create.py").read_text(encoding="utf-8")
    assert "no invalid case could be derived" in content
    assert "INVALID_CASES = [\n    {},\n]" in content


def test_mid_run_refusal_leaves_no_partial_output(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path / "gen")
    (workspace / "steps/wave").mkdir()
    (workspace / "steps/wave/manifest.yaml").write_text(
        f"""apiVersion: syntara.io/v1alpha1
kind: Action
metadata:
  name: wave
  displayName: Wave
  description: Waves.
spec:
  runtime:
    kind: custom-runner
    entryPoint: gen_pkg.steps:CreateStep
  input:
    kind: inline
    value: {INPUT_SCHEMA}
  output:
    kind: inline
    value: {OUTPUT_SCHEMA}
  error:
    kind: inline
    value:
      $schema: https://json-schema.org/draft/2020-12/schema
      type: object
""",
        encoding="utf-8",
    )
    plugin = workspace / "plugin.yaml"
    plugin.write_text(
        plugin.read_text(encoding="utf-8").replace(
            "    - steps/create/manifest.yaml\n",
            "    - steps/create/manifest.yaml\n    - steps/wave/manifest.yaml\n",
        ),
        encoding="utf-8",
    )
    # The second target's file already exists; the refusal must fire before
    # the first target's file is written.
    (workspace / "tests").mkdir()
    (workspace / "tests/test_wave.py").write_text("# existing\n", encoding="utf-8")
    with pytest.raises(ScaffoldError, match="refusing to overwrite"):
        generate_step_tests(workspace / "plugin.yaml")
    assert not (workspace / "tests/test_create.py").exists()


def test_relative_tests_dirs_resolve_against_the_workspace(tmp_path: Path) -> None:
    import os

    original = os.getcwd()
    os.chdir(tmp_path)
    try:
        workspace = _write_workspace(tmp_path / "gen")
        written = generate_step_tests(
            workspace / "plugin.yaml", tests_dir=Path("nested/unit")
        )
    finally:
        os.chdir(original)
    assert any(path.name == "test_create.py" for path in written)
    module = (workspace / "nested/unit/test_create.py").read_text(encoding="utf-8")
    conftest = (workspace / "nested/unit/conftest.py").read_text(encoding="utf-8")
    # The generated paths must climb from the nested directory to the workspace.
    assert repr("../..") in module
    assert repr("../..") in conftest


def test_business_invalid_placeholders_skip_instead_of_failing(
    pytester: pytest.Pytester,
) -> None:
    """A schema-valid placeholder that violates a business invariant skips.

    The generator cannot know business rules; the generated valid-input test
    must not fail on first run when the scaffolded placeholder is
    business-invalid (here: an implementation requiring count >= 10 while the
    schema-valid placeholder is count = 1).
    """

    (pytester.path / "steps/count-report").mkdir(parents=True)
    (pytester.path / "docs").mkdir()
    (pytester.path / "count_report").mkdir()
    (pytester.path / "plugin.yaml").write_text(
        """apiVersion: syntara.io/v1alpha1
kind: Plugin
metadata:
  namespace: gen
  name: business-plugin
  version: 0.1.0
  displayName: Business
  description: Business invariant.
spec:
  documentation:
    path: docs/README.md
  workloads:
    business-workload:
      image: business-workload
  targets:
    - steps/count-report/manifest.yaml
""",
        encoding="utf-8",
    )
    (pytester.path / "steps/count-report/manifest.yaml").write_text(
        """apiVersion: syntara.io/v1alpha1
kind: Action
metadata:
  name: count-report
  displayName: Count report
  description: Reports a count.
spec:
  runtime:
    kind: custom-runner
    entryPoint: count_report:CountReportStep
  input:
    kind: inline
    value:
      $schema: https://json-schema.org/draft/2020-12/schema
      type: object
      properties:
        count:
          type: integer
      required: [count]
  output:
    kind: inline
    value:
      $schema: https://json-schema.org/draft/2020-12/schema
      type: object
  error:
    kind: inline
    value:
      $schema: https://json-schema.org/draft/2020-12/schema
      type: object
""",
        encoding="utf-8",
    )
    (pytester.path / "docs/README.md").write_text("# Business\n", encoding="utf-8")
    (pytester.path / "count_report/__init__.py").write_text(
        '''"""A step type whose business rules reject the scaffolded placeholder."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, ExecutionContext


class CountInput(BaseModel):
    count: int


class CountOutput(BaseModel):
    report: str


class CountReportStep(ActionStep[CountInput, CountOutput]):
    def run(self, inputs: CountInput, context: ExecutionContext) -> CountOutput:
        if inputs.count < 10:
            message = f"count must be at least 10, got {inputs.count}"
            raise ValueError(message)
        return CountOutput(report=f"counted {inputs.count}")
''',
        encoding="utf-8",
    )
    generate_step_tests(pytester.path / "plugin.yaml")
    result = pytester.runpytest_subprocess("tests")
    result.assert_outcomes(passed=1, skipped=2, failed=0)


def test_generated_assertion_messages_never_echo_payloads(tmp_path: Path) -> None:
    """Assertion messages use stable text, so secrets in cases cannot leak."""

    workspace = _write_workspace(tmp_path / "gen")
    generate_step_tests(workspace / "plugin.yaml")
    content = (workspace / "tests/test_create.py").read_text(encoding="utf-8")
    assert "assert not outcome.ok, inputs" not in content
    assert 'assert not outcome.ok, "an invalid case was accepted"' in content
    assert 'assert outcome.run_calls == 0, "the business hook ran for an invalid case"' in content
    assert 'assert outcome.ok, f"valid case failed (status={outcome.status})"' in content
