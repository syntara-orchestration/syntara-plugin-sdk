"""Unit-test the credential compatibility fixture steps without a cluster."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

if TYPE_CHECKING:
    from syntara_plugin.runtime import ExecutionContext, StepBinding

# The fixture workload uses a src layout; make its package importable so the
# manifest entry points resolve exactly as they do inside the image.
_WORKLOAD_SRC = Path(__file__).resolve().parent / "credential_workload" / "src"
if str(_WORKLOAD_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKLOAD_SRC))

from credential_steps.runtime import (  # noqa: E402
    CREDENTIAL_DIGEST_PREFIX,
    CREDENTIAL_RECIPE_ID,
    HTTP_LOOPBACK_PORT,
    USE_CREDENTIAL_ENV,
    _verification_server,
)
from syntara_plugin.contracts import canonical_json_bytes  # noqa: E402
from syntara_plugin.testing import (  # noqa: E402
    StepBindingsFactory,
    assert_no_secrets,
    build_execution_context,
    invoke_step,
)

_WORKSPACE = _WORKLOAD_SRC / "credential_steps" / "manifests" / "plugin.yaml"
# Markers must not contain any value that appears in a delivered credential
# document: the SDK scans step outputs for those values after a read.
_BASH_MARKER = "compat-unit-bash"
_PYTHON_MARKER = "compat-unit-python"


@pytest.fixture(scope="module")
def bash_binding() -> StepBinding:
    """Bind the Bash script shape from the fixture workspace."""
    return StepBindingsFactory().bind_step(
        "credential_steps.runtime:ScriptBashStep", workspace=_WORKSPACE
    )


@pytest.fixture(scope="module")
def python_binding() -> StepBinding:
    """Bind the Python script shape from the fixture workspace."""
    return StepBindingsFactory().bind_step(
        "credential_steps.runtime:ScriptPythonStep", workspace=_WORKSPACE
    )


@pytest.fixture(scope="module")
def http_binding() -> StepBinding:
    """Bind the HTTP request shape from the fixture workspace."""
    return StepBindingsFactory().bind_step(
        "credential_steps.runtime:HttpRequestStep", workspace=_WORKSPACE
    )


@pytest.fixture
def credential_context(tmp_path: Path) -> Callable[[dict[str, str]], ExecutionContext]:
    """Deliver one synthetic bearer document through the local file provider."""

    def _build(document: dict[str, str]) -> ExecutionContext:
        path = tmp_path / "credential.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return build_execution_context(
            invocation_id=f"compat-unit-{uuid.uuid4().hex[:8]}",
            credential_file_paths={CREDENTIAL_RECIPE_ID: str(path)},
            credential_directory=tmp_path,
        )

    return _build


def _document() -> dict[str, str]:
    """Return one synthetic credential document with a unique token."""
    return {"auth_type": "bearer", "bearer_token": f"unit-{uuid.uuid4().hex}"}


def _digest(document: dict[str, str]) -> str:
    """Return the canonical-JSON sha256 the steps are expected to emit."""
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def test_script_steps_declare_the_credential_recipe(bash_binding: StepBinding) -> None:
    """The compiled fixture must authorize the recipe the steps read."""
    assert bash_binding.descriptor.credential_names == (CREDENTIAL_RECIPE_ID,)


def test_script_bash_step_proves_credential_use_by_digest(
    bash_binding: StepBinding, credential_context: Callable[[dict[str, str]], ExecutionContext]
) -> None:
    document = _document()
    outcome = invoke_step(
        bash_binding,
        {"code": f"echo {_BASH_MARKER}", "environment": {USE_CREDENTIAL_ENV: "1"}},
        context=credential_context(document),
    )
    assert outcome.ok, outcome.error
    assert outcome.output["return_code"] == 0
    assert outcome.output["stderr"] == ""
    assert outcome.output["stdout"] == (
        f"{_BASH_MARKER}\n{CREDENTIAL_DIGEST_PREFIX}{_digest(document)}\n"
    )
    assert_no_secrets(outcome, [document["bearer_token"]])


def test_script_python_step_parses_user_json_before_the_digest_line(
    python_binding: StepBinding, credential_context: Callable[[dict[str, str]], ExecutionContext]
) -> None:
    document = _document()
    code = f'import json; print(json.dumps({{"marker": "{_PYTHON_MARKER}"}}))'
    outcome = invoke_step(
        python_binding,
        {"code": code, "environment": {USE_CREDENTIAL_ENV: "1"}},
        context=credential_context(document),
    )
    assert outcome.ok, outcome.error
    assert outcome.output["return_code"] == 0
    # stdout_json comes from the submitted code's final line, never from the
    # digest line the worker appends afterwards.
    assert outcome.output["stdout_json"] == {"marker": _PYTHON_MARKER}
    assert outcome.output["stdout"].endswith(
        f"{CREDENTIAL_DIGEST_PREFIX}{_digest(document)}\n"
    )
    assert_no_secrets(outcome, [document["bearer_token"]])


def test_script_steps_read_credentials_only_when_requested(bash_binding: StepBinding) -> None:
    """Without the opt-in flag the step never asks for a credential."""
    context = build_execution_context(invocation_id="compat-unit-opt-out")
    outcome = invoke_step(bash_binding, {"code": f"echo {_BASH_MARKER}"}, context=context)
    assert outcome.ok, outcome.error
    assert outcome.output["stdout"] == f"{_BASH_MARKER}\n"
    assert CREDENTIAL_DIGEST_PREFIX not in outcome.output["stdout"]


def test_script_step_enforces_its_deadline(bash_binding: StepBinding) -> None:
    context = build_execution_context(invocation_id="compat-unit-deadline")
    outcome = invoke_step(
        bash_binding, {"code": "sleep 30", "timeout_seconds": 1}, context=context
    )
    assert outcome.ok, outcome.error
    assert outcome.output["return_code"] != 0


def test_http_request_step_sends_the_credential_over_loopback(
    http_binding: StepBinding, credential_context: Callable[[dict[str, str]], ExecutionContext]
) -> None:
    document = _document()
    outcome = invoke_step(
        http_binding,
        {"url": "http://127.0.0.1:18080/verify", "method": "GET"},
        context=credential_context(document),
    )
    assert outcome.ok, outcome.error
    assert outcome.output["status_code"] == 200
    assert outcome.output["body"] == {
        "authenticated": True,
        "token_sha256": _digest(document),
    }
    assert outcome.output["headers"]["Content-Type"] == "application/json"
    assert outcome.output["elapsed"] >= 0
    assert_no_secrets(outcome, [document["bearer_token"]])


def test_http_request_step_rejects_documents_without_a_token(
    http_binding: StepBinding, credential_context: Callable[[dict[str, str]], ExecutionContext]
) -> None:
    """A malformed document fails closed as a deliberate step failure."""
    outcome = invoke_step(
        http_binding,
        {"url": "http://127.0.0.1:18080/verify"},
        context=credential_context({"auth_type": "bearer"}),
    )
    assert not outcome.ok
    assert outcome.status == 1
    assert outcome.error is not None
    assert outcome.error.code == "AUTHENTICATION_FAILED"
    assert outcome.raised is None


def test_http_request_step_rejects_urls_off_the_verification_port(
    http_binding: StepBinding,
) -> None:
    """The schema pins the URL to the port the step's server binds."""
    context = build_execution_context(invocation_id="compat-unit-port")
    outcome = invoke_step(
        http_binding, {"url": "http://127.0.0.1:9999/verify"}, context=context
    )
    assert not outcome.ok
    assert outcome.status == 1
    assert outcome.error is not None
    assert outcome.error.code == "INVALID_INPUT"
    assert outcome.raised is None


def test_http_request_step_fails_closed_when_the_server_is_unreachable(
    http_binding: StepBinding,
    credential_context: Callable[[dict[str, str]], ExecutionContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable server is a structured failure, not a crash."""

    def unreachable(*args: object, **kwargs: object) -> object:
        raise URLError("connection refused")

    monkeypatch.setattr("credential_steps.runtime.urlopen", unreachable)
    outcome = invoke_step(
        http_binding,
        {"url": "http://127.0.0.1:18080/verify"},
        context=credential_context(_document()),
    )
    assert not outcome.ok
    assert outcome.status == 1
    assert outcome.error is not None
    assert outcome.error.code == "DEPENDENCY_FAILED"
    assert outcome.raised is None


def test_verification_server_rejects_wrong_authorization() -> None:
    """The loopback server answers 401 for any token but the delivered one."""
    server = _verification_server("Bearer unit-approved-token-0123456789", "digest-placeholder")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{HTTP_LOOPBACK_PORT}/verify",
            headers={"Authorization": "Bearer unit-wrong-token-9876543210"},
        )
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=5)
        assert error.value.code == 401
        body = json.loads(error.value.read().decode("utf-8"))
        assert body == {"authenticated": False, "token_sha256": None}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_script_step_captures_non_utf8_output(bash_binding: StepBinding) -> None:
    """Arbitrary bytes in subprocess output are captured, not fatal."""
    context = build_execution_context(invocation_id="compat-unit-bytes")
    outcome = invoke_step(
        bash_binding, {"code": "printf 'raw \\xff\\xfe bytes'"}, context=context
    )
    assert outcome.ok, outcome.error
    assert outcome.output["return_code"] == 0
    assert "raw" in outcome.output["stdout"]
    assert "bytes" in outcome.output["stdout"]
