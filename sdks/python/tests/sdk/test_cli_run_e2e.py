"""In-repo end-to-end checks for ``syntara-plugin run`` in both modes.

The image-mode cases follow the SDK's own client-test pattern: a real
in-process gRPC runtime on loopback serving step handlers, never a mock of
gRPC. Source mode executes the real ``examples/python-steps`` workspace
shipped with this repository.
"""

from __future__ import annotations

import importlib
import json
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import grpc
from syntara_plugin.runtime import (
    ContainerInvocation,
    ContainerInvocationError,
    ContainerResult,
    FailureCode,
    StepRegistry,
    add_container_runtime_service,
)
from syntara_plugin.runtime.container import ContainerRuntimeService
from syntara_plugin.runtime.protocol import container_pb2, container_pb2_grpc
from syntara_plugin.sdk.cli import main

_REPO_ROOT = Path(__file__).resolve().parents[4]
PYTHON_STEPS = _REPO_ROOT / "examples" / "python-steps"
OUTPUT_SCHEMA_DIGEST = "sha256:" + "a" * 64
CREATE_ISSUE = "example.python-steps.create-issue"
RECORD_OPENED = "example.python-steps.record-opened"


class _AbiMismatchService(ContainerRuntimeService):
    """A real server whose Health response names a different runtime ABI."""

    def __init__(self, steps: StepRegistry, abi: str) -> None:
        super().__init__(steps)
        self._declared_abi = abi

    def Health(
        self, request: container_pb2.HealthRequest, context: grpc.ServicerContext
    ) -> container_pb2.HealthResponse:
        return container_pb2.HealthResponse(
            ready=True,
            runtime_abi=self._declared_abi,
            step_revision_ids=self._steps.step_revision_ids,
        )


def _create_issue(invocation: ContainerInvocation) -> ContainerResult:
    return ContainerResult(
        OUTPUT_SCHEMA_DIGEST, output={"issue_url": "https://issues.example.test/run-e2e"}
    )


def _rejected(invocation: ContainerInvocation) -> ContainerResult:
    raise ContainerInvocationError(
        FailureCode.REMOTE_REJECTED,
        "The tracker rejected the request.",
        retryable=True,
        outcome="not_completed",
    )


def _crashed(invocation: ContainerInvocation) -> ContainerResult:
    raise RuntimeError("unexpected internal crash: token=SECRET-XYZ")


def _object_reference_leak(invocation: ContainerInvocation) -> ContainerResult:
    return ContainerResult(
        OUTPUT_SCHEMA_DIGEST,
        object_reference=f"ref-{invocation.inputs.get('api_token', '')}",
    )


def _error_code_leak(invocation: ContainerInvocation) -> ContainerResult:
    return ContainerResult(
        OUTPUT_SCHEMA_DIGEST,
        status_code=5,
        error_code=str(invocation.inputs.get("api_token", "REJECTED")),
        error_message="rejected",
        retryable=False,
    )


def _start_server(
    handlers: dict[str, Any], *, abi: str | None = None
) -> tuple[int, grpc.Server]:
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    registry = StepRegistry(handlers)
    if abi is None:
        add_container_runtime_service(server, registry)
    else:
        container_pb2_grpc.add_ContainerServiceServicer_to_server(
            _AbiMismatchService(registry, abi), server
        )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    return port, server


def _stop(server: grpc.Server) -> None:
    stopped = threading.Event()

    def wait() -> None:
        server.stop(grace=0).wait()
        stopped.set()

    threading.Thread(target=wait, daemon=True).start()
    stopped.wait(timeout=5)


def _report_field(stdout: str, label: str) -> str:
    """Extract one aligned report field value from human output."""

    line = next(line for line in stdout.splitlines() if line.startswith(f"{label}:"))
    return line.split(":", 1)[1].strip()


def _output_json(stdout: str) -> Any:
    return json.loads(stdout.split("Output:\n", 1)[1])


def test_source_mode_runs_the_python_steps_example(capsys) -> None:
    """The shipped example runs end to end from source over real gRPC."""

    status = main(
        [
            "run",
            "--manifest",
            str(PYTHON_STEPS / "plugin.yaml"),
            "create-issue",
            "--input",
            '{"title": "Run E2E"}',
        ]
    )

    assert status == 0
    captured = capsys.readouterr()
    assert _report_field(captured.out, "Target") == CREATE_ISSUE
    assert _report_field(captured.out, "Mode") == "source"
    assert _report_field(captured.out, "Status") == "0"
    assert _output_json(captured.out) == {"issue_url": "https://issues.example.test/run-e2e"}


def test_source_mode_runs_a_trigger_target(capsys) -> None:
    """Trigger targets run through the same source-mode path as Actions."""

    status = main(
        [
            "run",
            "--manifest",
            str(PYTHON_STEPS / "plugin.yaml"),
            "record-opened",
            "--input",
            '{"repository": "ansible/syntara"}',
        ]
    )

    assert status == 0
    captured = capsys.readouterr()
    assert _report_field(captured.out, "Target") == RECORD_OPENED
    output = _output_json(captured.out)
    assert output["repository"] == "ansible/syntara"
    assert output["issue_url"]


def test_source_mode_template_guides_the_python_steps_example(capsys) -> None:
    """A multi-target workspace lists its choices instead of guessing."""

    status = main(["run", "--manifest", str(PYTHON_STEPS / "plugin.yaml")])

    assert status == 2
    captured = capsys.readouterr()
    assert "more than one runnable target" in captured.err
    assert CREATE_ISSUE in captured.err
    assert RECORD_OPENED in captured.err


def test_image_mode_runs_against_a_real_runtime_server(capsys) -> None:
    """Image mode reaches an already-running runtime through ContainerClient."""

    port, server = _start_server({CREATE_ISSUE: _create_issue})
    try:
        status = main(
            [
                "run",
                "--manifest",
                str(PYTHON_STEPS / "plugin.yaml"),
                "create-issue",
                "--mode",
                "image",
                "--address",
                f"127.0.0.1:{port}",
                "--input",
                '{"title": "hi"}',
            ]
        )
    finally:
        _stop(server)

    assert status == 0
    captured = capsys.readouterr()
    assert _report_field(captured.out, "Mode") == "image"
    assert _report_field(captured.out, "Input") == '{"title": "hi"}'
    assert _output_json(captured.out) == {"issue_url": "https://issues.example.test/run-e2e"}


def test_image_mode_reports_a_deliberate_failure(capsys) -> None:
    """A workload failure event keeps its code, message, and retryability."""

    port, server = _start_server({CREATE_ISSUE: _rejected})
    try:
        status = main(
            [
                "run",
                "--manifest",
                str(PYTHON_STEPS / "plugin.yaml"),
                "create-issue",
                "--mode",
                "image",
                "--address",
                f"127.0.0.1:{port}",
                "--input",
                '{"title": "hi"}',
            ]
        )
    finally:
        _stop(server)

    assert status == 1
    captured = capsys.readouterr()
    assert _report_field(captured.out, "Error") == "REMOTE_REJECTED (retryable: true)"
    assert _report_field(captured.out, "Message") == "The tracker rejected the request."
    assert _report_field(captured.out, "Outcome") == "not_completed"
    assert "Error detail:" not in captured.out


def test_image_mode_reports_an_unexpected_exception_safely(capsys) -> None:
    """A crashing step maps to OUTCOME_UNKNOWN without leaking its error text."""

    port, server = _start_server({CREATE_ISSUE: _crashed})
    try:
        status = main(
            [
                "run",
                "--manifest",
                str(PYTHON_STEPS / "plugin.yaml"),
                "create-issue",
                "--mode",
                "image",
                "--address",
                f"127.0.0.1:{port}",
                "--input",
                '{"title": "hi"}',
            ]
        )
    finally:
        _stop(server)

    assert status == 1
    captured = capsys.readouterr()
    assert _report_field(captured.out, "Error") == "OUTCOME_UNKNOWN (retryable: false)"
    assert _report_field(captured.out, "Message") == "The plugin step did not complete."
    assert _report_field(captured.out, "Outcome") == "unknown"
    assert "Error detail:" in captured.out
    assert "not captured" in captured.out
    assert "--mode source" in captured.out
    assert "SECRET-XYZ" not in captured.out
    assert "internal crash" not in captured.out


def test_image_mode_rejects_an_unadvertised_identity(capsys) -> None:
    """A runtime built from another workspace lists what it does advertise."""

    port, server = _start_server({CREATE_ISSUE: _create_issue})
    try:
        status = main(
            [
                "run",
                "--manifest",
                str(PYTHON_STEPS / "plugin.yaml"),
                "record-opened",
                "--mode",
                "image",
                "--address",
                f"127.0.0.1:{port}",
                "--input",
                '{"repository": "ansible/syntara"}',
            ]
        )
    finally:
        _stop(server)

    assert status == 1
    captured = capsys.readouterr()
    assert "does not advertise 'example.python-steps.record-opened'" in captured.err
    assert "Advertised steps: example.python-steps.create-issue" in captured.err


def test_image_mode_rejects_an_abi_mismatch(capsys) -> None:
    """A runtime speaking another ABI is reported before any execution."""

    port, server = _start_server({CREATE_ISSUE: _create_issue}, abi="syntara.container/v0")
    try:
        status = main(
            [
                "run",
                "--manifest",
                str(PYTHON_STEPS / "plugin.yaml"),
                "create-issue",
                "--mode",
                "image",
                "--address",
                f"127.0.0.1:{port}",
                "--input",
                '{"title": "hi"}',
            ]
        )
    finally:
        _stop(server)

    assert status == 1
    captured = capsys.readouterr()
    assert "does not implement the supported container ABI" in captured.err
    assert "syntara.container/v1alpha1" in captured.err


def test_image_mode_scrubs_a_remote_object_reference(capsys) -> None:
    """A runtime smuggling the token through object_reference is scrubbed."""

    port, server = _start_server({CREATE_ISSUE: _object_reference_leak})
    try:
        status = main(
            [
                "run",
                "--manifest",
                str(PYTHON_STEPS / "plugin.yaml"),
                "create-issue",
                "--mode",
                "image",
                "--address",
                f"127.0.0.1:{port}",
                "--input",
                '{"title": "hi", "api_token": "GHPTOKEN123XYZ"}',
            ]
        )
    finally:
        _stop(server)

    assert status == 0
    captured = capsys.readouterr()
    assert "Output reference: ref-***" in captured.out
    assert "GHPTOKEN123XYZ" not in captured.out


def test_image_mode_scrubs_a_remote_error_code(capsys) -> None:
    """A runtime smuggling the token through error_code is scrubbed."""

    port, server = _start_server({CREATE_ISSUE: _error_code_leak})
    try:
        status = main(
            [
                "run",
                "--manifest",
                str(PYTHON_STEPS / "plugin.yaml"),
                "create-issue",
                "--mode",
                "image",
                "--address",
                f"127.0.0.1:{port}",
                "--input",
                '{"title": "hi", "api_token": "GHPTOKEN123XYZ"}',
            ]
        )
    finally:
        _stop(server)

    assert status == 1
    captured = capsys.readouterr()
    assert "GHPTOKEN123XYZ" not in captured.out
    assert _report_field(captured.out, "Error").startswith("***")


_E2E_STEPS_SOURCE = '''"""Real step implementations served over gRPC for image-mode e2e."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import (
    ActionStep,
    ContainerInvocationError,
    ExecutionContext,
    FailureCode,
)


class StepInput(BaseModel):
    title: str


class StepOutput(BaseModel):
    slug: str


class OkStep(ActionStep[StepInput, StepOutput]):
    def run(self, inputs: StepInput, context: ExecutionContext) -> StepOutput:
        return StepOutput(slug=inputs.title.lower())


class FailStep(ActionStep[StepInput, StepOutput]):
    def run(self, inputs: StepInput, context: ExecutionContext) -> StepOutput:
        raise ContainerInvocationError(
            FailureCode.REMOTE_REJECTED,
            "The tracker rejected the request.",
            retryable=True,
            outcome="not_completed",
        )


class CrashStep(ActionStep[StepInput, StepOutput]):
    def run(self, inputs: StepInput, context: ExecutionContext) -> StepOutput:
        raise RuntimeError("unexpected crash inside the step")
'''

_E2E_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
}
_E2E_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"slug": {"type": "string"}},
    "required": ["slug"],
}


def _write_real_step_workspace(root: Path) -> tuple[Path, str, str]:
    """One workspace whose ok/fail/crash targets are real step classes."""

    suffix = uuid.uuid4().hex[:8]
    package = f"e2e_pkg_{suffix}"
    plugin_name = f"e2e-steps-{suffix}"
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / package).mkdir(parents=True, exist_ok=True)
    targets = ""
    for name, class_name in (("ok", "OkStep"), ("fail", "FailStep"), ("crash", "CrashStep")):
        (root / f"steps/{name}").mkdir(parents=True, exist_ok=True)
        (root / f"steps/{name}/manifest.yaml").write_text(
            f"""apiVersion: syntara.io/v1alpha1
kind: Action
metadata:
  name: {name}
  displayName: {name}
  description: Real-step e2e fixture.
spec:
  runtime:
    kind: custom-runner
    entryPoint: {package}.steps:{class_name}
  input:
    kind: inline
    value: {_E2E_INPUT_SCHEMA}
  output:
    kind: inline
    value: {_E2E_OUTPUT_SCHEMA}
  error:
    kind: inline
    value:
      type: object
""",
            encoding="utf-8",
        )
        targets += f"    - steps/{name}/manifest.yaml\n"
    (root / "plugin.yaml").write_text(
        f"""apiVersion: syntara.io/v1alpha1
kind: Plugin
metadata:
  namespace: run
  name: {plugin_name}
  version: 0.1.0
  displayName: E2E steps
  description: Real-step e2e fixture.
spec:
  documentation:
    path: docs/README.md
  workload:
    image: e2e-workload
  targets:
{targets}""",
        encoding="utf-8",
    )
    (root / "docs/README.md").write_text("# E2E\n", encoding="utf-8")
    (root / f"{package}/__init__.py").write_text("", encoding="utf-8")
    (root / f"{package}/steps.py").write_text(_E2E_STEPS_SOURCE, encoding="utf-8")
    return root, package, plugin_name


def _serve_real_steps(
    workspace: Path, package: str, plugin_name: str
) -> tuple[int, grpc.Server]:
    """Serve the workspace's real steps through the container adapter."""

    from syntara_plugin.runtime import StepBindings
    from syntara_plugin.runtime.adapter import step_registry_from_bindings
    from syntara_plugin.testing.bindings_factory import installed_binding

    if str(workspace) not in sys.path:
        sys.path.insert(0, str(workspace))
    module = importlib.import_module(f"{package}.steps")
    bindings = StepBindings()
    for class_name, target_name in (("OkStep", "ok"), ("FailStep", "fail"), ("CrashStep", "crash")):
        step = getattr(module, class_name)()
        binding = installed_binding(step, identity=f"run.{plugin_name}.{target_name}")
        bindings.bind(binding.descriptor, binding.step)
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(server, step_registry_from_bindings(bindings))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    return port, server


def _run_real_step(
    workspace: Path, target: str, port: int, capsys
) -> tuple[int, Any]:
    status = main(
        [
            "run",
            "--manifest",
            str(workspace / "plugin.yaml"),
            target,
            "--mode",
            "image",
            "--address",
            f"127.0.0.1:{port}",
            "--input",
            '{"title": "Hello"}',
        ]
    )
    return status, capsys.readouterr()


def test_image_mode_runs_a_real_step_implementation(tmp_path: Path, capsys) -> None:
    """A real step class executes end to end over gRPC in image mode."""

    workspace, package, plugin_name = _write_real_step_workspace(tmp_path / "ws")
    port, server = _serve_real_steps(workspace, package, plugin_name)
    try:
        status, captured = _run_real_step(workspace, "ok", port, capsys)
    finally:
        _stop(server)

    assert status == 0
    assert _report_field(captured.out, "Target") == f"run.{plugin_name}.ok"
    assert _output_json(captured.out) == {"slug": "hello"}


def test_image_mode_reports_a_real_steps_deliberate_failure(
    tmp_path: Path, capsys
) -> None:
    """A real step's deliberate failure keeps its code and retryability."""

    workspace, package, plugin_name = _write_real_step_workspace(tmp_path / "ws")
    port, server = _serve_real_steps(workspace, package, plugin_name)
    try:
        status, captured = _run_real_step(workspace, "fail", port, capsys)
    finally:
        _stop(server)

    assert status == 1
    assert _report_field(captured.out, "Error") == "REMOTE_REJECTED (retryable: true)"
    assert _report_field(captured.out, "Message") == "The tracker rejected the request."
    assert _report_field(captured.out, "Outcome") == "not_completed"
    assert "Error detail:" not in captured.out


def test_image_mode_reports_a_real_steps_unexpected_exception(
    tmp_path: Path, capsys
) -> None:
    """A real step crashing maps to OUTCOME_UNKNOWN with the image-mode hint."""

    workspace, package, plugin_name = _write_real_step_workspace(tmp_path / "ws")
    port, server = _serve_real_steps(workspace, package, plugin_name)
    try:
        status, captured = _run_real_step(workspace, "crash", port, capsys)
    finally:
        _stop(server)

    assert status == 1
    assert _report_field(captured.out, "Error") == "OUTCOME_UNKNOWN (retryable: false)"
    assert _report_field(captured.out, "Message") == "The plugin step did not complete."
    assert "Error detail:" in captured.out
    assert "not captured" in captured.out
    assert "unexpected crash" not in captured.out
