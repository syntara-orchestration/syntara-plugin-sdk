"""Tests for the test-init scaffolding generator and CLI command."""

from __future__ import annotations

from pathlib import Path

import pytest
from syntara_plugin.sdk.cli import main
from syntara_plugin.sdk.testgen import ScaffoldError, generate_step_tests

STEP_SOURCE = '''"""A custom-workload Action step used by the generated tests."""

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


def _write_workspace(root: Path) -> Path:
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
    kind: custom-workload
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
    (root / "docs/README.md").write_text("# Gen\n", encoding="utf-8")
    (root / "gen_pkg/__init__.py").write_text("", encoding="utf-8")
    (root / "gen_pkg/steps.py").write_text(STEP_SOURCE, encoding="utf-8")
    return root


def test_generate_creates_a_test_module_and_import_shim(tmp_path: Path) -> None:
    workspace = _write_workspace(tmp_path / "gen")
    written = generate_step_tests(workspace / "plugin.yaml")
    names = {path.name for path in written}
    assert names == {"test_create.py", "conftest.py"}
    content = (workspace / "tests/test_create.py").read_text(encoding="utf-8")
    assert 'ENTRY_POINT = "gen_pkg.steps:CreateStep"' in content
    assert "'title': 'example-title'" in content
    assert "step_bindings.bind_action" in content


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


def test_generate_rejects_workspaces_without_custom_workload_actions(
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
    driver: http.v1
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
    with pytest.raises(ScaffoldError, match="no custom-workload Action targets"):
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


def test_generated_tests_pass_in_a_fresh_project(pytester: pytest.Pytester) -> None:
    _write_workspace(pytester.path)
    generate_step_tests(pytester.path / "plugin.yaml")
    result = pytester.runpytest_subprocess("tests")
    result.assert_outcomes(passed=3)
