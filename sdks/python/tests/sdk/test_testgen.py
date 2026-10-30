"""Tests for the test-init scaffolding generator and CLI command."""

from __future__ import annotations

from pathlib import Path

import pytest
from syntara_plugin.sdk.cli import main
from syntara_plugin.sdk.testgen import ScaffoldError, generate_step_tests

STEP_SOURCE = '''"""A custom-runner Action step used by the generated tests."""

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
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "priority": {"type": "string", "default": "low", "enum": ["low", "medium", "high"]},
    },
    "required": ["title"],
}
OUTPUT_SCHEMA = {
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
  workload:
    image: gen-workload
  targets:
    - steps/create/manifest.yaml
""",
        encoding="utf-8",
    )
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
    value: {input_schema or INPUT_SCHEMA}
  output:
    kind: inline
    value: {OUTPUT_SCHEMA}
  error:
    kind: inline
    value:
      type: object
""",
        encoding="utf-8",
    )
    (root / "docs/README.md").write_text("# Gen\n", encoding="utf-8")
    (root / "gen_pkg/__init__.py").write_text("", encoding="utf-8")
    (root / "gen_pkg/steps.py").write_text(STEP_SOURCE, encoding="utf-8")
    return root


def _write_workspace_for_kind(root: Path, kind: str) -> Path:
    """Write the same minimal workspace with a non-Action Step kind."""

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
    assert 'ENTRY_POINT = "gen_pkg.steps:CreateStep"' in content
    assert "'title': 'example-title'" in content
    assert "step_bindings.bind_step" in content


@pytest.mark.parametrize("kind", ["Control", "Trigger"])
def test_generate_covers_every_custom_runner_step_kind(tmp_path: Path, kind: str) -> None:
    workspace = _write_workspace_for_kind(tmp_path / kind.lower(), kind)
    generate_step_tests(workspace / "plugin.yaml")
    content = (workspace / "tests/test_create.py").read_text(encoding="utf-8")
    assert f"{kind} Step type" in content
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
      type: object
  output:
    kind: inline
    value:
      type: object
""",
        encoding="utf-8",
    )
    with pytest.raises(ScaffoldError, match="no custom-runner Step targets"):
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
  workload:
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
      type: object
      properties:
        count:
          type: integer
      required: [count]
  output:
    kind: inline
    value:
      type: object
  error:
    kind: inline
    value:
      type: object
""",
        encoding="utf-8",
    )
    (pytester.path / "docs/README.md").write_text("# Business\n", encoding="utf-8")
    (pytester.path / "count_report/__init__.py").write_text(
        '''"""A step whose business rules reject the scaffolded placeholder."""

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
