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
        error_result={
            "category": "business",
            "code": str(invocation.inputs.get("api_token", "REJECTED")),
            "message": "rejected",
            "retryable": False,
        },
        error_schema_digest="sha256:" + "b" * 64,
    )


def _start_server(
    handlers: dict[str, Any], *, abi: str | None = None
) -> tuple[int, grpc.Server]:
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    revisions = {str(t["identity"]): str(t["step_revision_id"]) for t in _compiled_targets(PYTHON_STEPS)}
    registry = StepRegistry({revisions.get(identity, identity): handler for identity, handler in handlers.items()})
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
    revisions = {str(t["identity"]): str(t["step_revision_id"]) for t in _compiled_targets(PYTHON_STEPS)}
    assert f"does not advertise '{revisions[RECORD_OPENED]}'" in captured.err
    assert f"Advertised steps: {revisions[CREATE_ISSUE]}" in captured.err


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
    ErrorCategory,
    ExecutionContext,
    FailureCode,
    StepFailure,
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
        raise StepFailure(
            1,
            "The tracker rejected the request.",
            error_message="The tracker rejected the request.",
            code=FailureCode.REMOTE_REJECTED.value,
            category=ErrorCategory.DEPENDENCY,
            retryable=True,
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
    from syntara_plugin.runtime.adapter import container_step_for_binding
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
    revisions = {str(t["identity"]): str(t["step_revision_id"]) for t in _compiled_targets(workspace)}
    add_container_runtime_service(server, StepRegistry({
        revisions[identity]: container_step_for_binding(bindings.binding(identity), step_revision_id=revisions[identity])
        for identity in bindings.step_identities
    }))
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


class _RecordingService(ContainerRuntimeService):
    """A real server that records the RPC order and the invocation it receives."""

    def __init__(self, steps: StepRegistry) -> None:
        super().__init__(steps)
        self.rpc_order: list[str] = []
        self.invocations: list[ContainerInvocation] = []

    def Health(self, request, context):
        self.rpc_order.append("Health")
        return super().Health(request, context)

    def Execute(self, request, context):
        self.rpc_order.append("Execute")
        invocation, _cancellation = __import__(
            "syntara_plugin.runtime.container", fromlist=["invocation_from_request"]
        ).invocation_from_request(request)
        self.invocations.append(invocation)
        return super().Execute(request, context)


def _start_recording_server(handlers: dict[str, Any]) -> tuple[_RecordingService, int, grpc.Server]:
    service = _RecordingService(StepRegistry(handlers))
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    container_pb2_grpc.add_ContainerServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    return service, port, server


def test_health_precedes_execute_and_the_invocation_contract_holds(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Health runs first, Execute runs once, and the invocation fields match the docs."""

    workspace, package, plugin_name = _write_real_step_workspace(tmp_path / "ws")
    monkeypatch.syspath_prepend(str(workspace))
    module = importlib.import_module(f"{package}.steps")
    from syntara_plugin.runtime import StepBindings
    from syntara_plugin.runtime.adapter import container_step_for_binding
    from syntara_plugin.testing.bindings_factory import installed_binding

    bindings = StepBindings()
    step = module.OkStep()
    binding = installed_binding(step, identity=f"run.{plugin_name}.ok")
    bindings.bind(binding.descriptor, step)
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    revision = next(str(t["step_revision_id"]) for t in _compiled_targets(workspace) if t["identity"] == f"run.{plugin_name}.ok")
    service = _RecordingService(StepRegistry({revision: container_step_for_binding(binding, step_revision_id=revision)}))
    container_pb2_grpc.add_ContainerServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        status = main(
            [
                "run",
                "--manifest",
                str(workspace / "plugin.yaml"),
                "ok",
                "--mode",
                "image",
                "--address",
                f"127.0.0.1:{port}",
                "--input",
                '{"title": "Hello"}',
            ]
        )
    finally:
        _stop(server)

    assert status == 0
    assert service.rpc_order == ["Health", "Execute"]
    invocation = service.invocations[0]
    assert invocation.step_revision_id == revision
    assert invocation.timeout_seconds == 60
    assert invocation.runtime_context == {}
    assert invocation.credential_file_paths == {}
    assert invocation.inputs == {"title": "Hello"}


_PROGRESS_WS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "api_token": {"type": "string", "writeOnly": True},
    },
    "required": ["title"],
}


def _write_progress_workspace(root: Path) -> Path:
    """One workspace whose target identity matches the progress-leak server."""

    suffix = uuid.uuid4().hex[:8]
    package = f"e2e_pkg_{suffix}"
    plugin_name = f"progress-ws-{suffix}"
    (root / "steps/echo").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / package).mkdir(parents=True, exist_ok=True)
    (root / "plugin.yaml").write_text(
        f"""apiVersion: syntara.io/v1alpha1
kind: Plugin
metadata:
  namespace: run
  name: {plugin_name}
  version: 0.1.0
  displayName: Progress
  description: Progress fixture.
spec:
  documentation:
    path: docs/README.md
  workload:
    image: progress-workload
  targets:
    - steps/echo/manifest.yaml
""",
        encoding="utf-8",
    )
    (root / "steps/echo/manifest.yaml").write_text(
        f"""apiVersion: syntara.io/v1alpha1
kind: Action
metadata:
  name: echo
  displayName: Echo
  description: Fixture.
spec:
  runtime:
    kind: custom-runner
    entryPoint: {package}.steps:EchoStep
  input:
    kind: inline
    value: {_PROGRESS_WS_INPUT_SCHEMA}
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
    (root / "docs/README.md").write_text("# Progress\n", encoding="utf-8")
    (root / f"{package}/__init__.py").write_text("", encoding="utf-8")
    (root / f"{package}/steps.py").write_text(_E2E_STEPS_SOURCE, encoding="utf-8")
    return root


class _ProgressLeakService(ContainerRuntimeService):
    """A real server whose Execute streams a progress event with the secret."""

    def __init__(self) -> None:
        super().__init__(StepRegistry({"run.progress-ws-X.echo": self._handler}))
        self._identity = "run.progress-ws-X.echo"

    def set_identity(self, identity: str) -> None:
        self._identity = identity
        self._steps = StepRegistry({identity: self._handler})

    def _handler(self, invocation: ContainerInvocation) -> ContainerResult:
        from syntara_plugin.runtime.container import ContainerResult as Result

        return Result("sha256:" + "a" * 64, output={"slug": "ok"})


def _start_progress_server(identity: str, monkeypatch: Any) -> tuple[int, grpc.Server]:
    service = _ProgressLeakService()
    service.set_identity(identity)
    server = grpc.server(ThreadPoolExecutor(max_workers=2))

    # Wrap Execute to prepend a progress event carrying the supplied secret.
    original_execute = service.Execute

    def execute_with_progress(request, context):
        from syntara_plugin.runtime.container import invocation_from_request

        invocation, _ = invocation_from_request(request)
        secret = invocation.inputs.get("api_token", "")
        yield container_pb2.ExecutionEvent(
            invocation_id=invocation.invocation_id,
            progress=container_pb2.Progress(
                sequence=1,
                state="progress",
                message=f"working with {secret}",
                percent=10.0,
                has_percent=True,
            ),
        )
        yield from original_execute(request, context)

    monkeypatch.setattr(service, "Execute", execute_with_progress)
    container_pb2_grpc.add_ContainerServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    return port, server


def test_progress_events_are_scrubbed_on_stderr(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """A progress message carrying a supplied secret is masked on stderr."""

    workspace = _write_progress_workspace(tmp_path / "ws")
    str(workspace.name)
    # Image dispatch uses the compiled revision.
    identity = next(
        str(target["step_revision_id"])
        for workspace_path in [workspace]
        for target in _compiled_targets(workspace_path)
    )
    port, server = _start_progress_server(identity, monkeypatch)
    try:
        status = main(
            [
                "run",
                "--manifest",
                str(workspace / "plugin.yaml"),
                "--mode",
                "image",
                "--address",
                f"127.0.0.1:{port}",
                "--verbose",
                "--input",
                '{"title": "x", "api_token": "PROGRESS-SECRET-42"}',
            ]
        )
    finally:
        _stop(server)

    assert status == 0
    captured = capsys.readouterr()
    assert "PROGRESS-SECRET-42" not in captured.err
    assert "==> progress" in captured.err


def _compiled_targets(workspace: Path) -> list[dict[str, Any]]:
    from syntara_plugin.sdk.authoring import BuildRequest, compile_workspace

    result = compile_workspace(BuildRequest(workspace / "plugin.yaml", require_image_bindings=False))
    return list(result.descriptor["spec"]["targets"])
