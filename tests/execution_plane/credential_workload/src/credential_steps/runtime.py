"""Credential HTTP and script step shapes for the compatibility workload.

Test-only fixture for the Execution Plane compatibility harness. The steps
mirror the HTTP Request, Script Bash, and Script Python step-type shapes and
prove, per execution, that the credential delivered through the invocation
callback actually reached the step:

* the script steps append ``COMPAT_CREDENTIAL_SHA256=<hex>`` to stdout when
  ``environment.COMPAT_USE_CREDENTIAL`` is ``"1"`` — the worker, not the
  submitted code, reads the credential, so the token never enters the
  subprocess environment or its streams;
* the HTTP step sends the token as an ``Authorization: Bearer`` header to a
  loopback server started by the step itself; the server accepts only that
  token and answers with its digest of the delivered document.

The token value never appears in any output; only its digest does.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict
from syntara_plugin.contracts import canonical_json_bytes
from syntara_plugin.runtime import (
    ActionStep,
    ErrorCategory,
    ExecutionContext,
    StepBindings,
    StepFailure,
    load_plugin_bindings,
)

INTEGRATION_TYPE = "compat.credentials.connection"
CREDENTIAL_RECIPE_ID = "compat.credentials.bearer-token"
USE_CREDENTIAL_ENV = "COMPAT_USE_CREDENTIAL"
CREDENTIAL_DIGEST_PREFIX = "COMPAT_CREDENTIAL_SHA256="
HTTP_LOOPBACK_PORT = 18080
_COMMUNICATE_INTERVAL_SECONDS = 0.1


class ScriptInput(BaseModel):
    """Validated inputs for the script step shapes."""

    model_config = ConfigDict(extra="forbid")

    code: str
    environment: dict[str, str] = {}
    timeout_seconds: int = 60


class ScriptBashOutput(BaseModel):
    """Output of the Bash script shape."""

    model_config = ConfigDict(extra="forbid")

    return_code: int
    stdout: str
    stderr: str


class ScriptPythonOutput(BaseModel):
    """Output of the Python script shape."""

    model_config = ConfigDict(extra="forbid")

    return_code: int
    stdout: str
    stderr: str
    stdout_json: Any | None = None


class HttpRequestInput(BaseModel):
    """Validated inputs for the HTTP request shape."""

    model_config = ConfigDict(extra="forbid")

    url: str
    method: str = "GET"
    timeout_seconds: int = 15


class HttpRequestOutput(BaseModel):
    """Output of the HTTP request shape, matching the real output freeze."""

    model_config = ConfigDict(extra="forbid")

    status_code: int
    body: Any
    headers: dict[str, str]
    elapsed: float


def _read_credential(context: ExecutionContext) -> dict[str, Any]:
    """Read the delivered credential document."""
    # Both delivery paths (local file provider and container callback)
    # validate the document against the recipe's schema before it arrives,
    # so it is always a JSON object here.
    return dict(context.credential(CREDENTIAL_RECIPE_ID).read())


def _credential_digest(document: Mapping[str, Any]) -> str:
    """Return the canonical-JSON sha256 of the delivered document."""
    return hashlib.sha256(canonical_json_bytes(dict(document))).hexdigest()


def _run_subprocess(
    command: list[str], inputs: ScriptInput, context: ExecutionContext
) -> tuple[int, str, str]:
    """Run the submitted code under a deadline, honouring cancellation."""
    environment = dict(os.environ)
    environment.update(inputs.environment)
    deadline = time.monotonic() + inputs.timeout_seconds
    with subprocess.Popen(
        [*command, inputs.code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=True,
    ) as process:
        while True:
            if context.cancelled or time.monotonic() >= deadline:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            try:
                stdout_bytes, stderr_bytes = process.communicate(
                    timeout=_COMMUNICATE_INTERVAL_SECONDS
                )
                break
            except subprocess.TimeoutExpired:
                continue
        # Submitted code can emit arbitrary bytes; capture instead of crashing.
        stdout = (stdout_bytes or b"").decode("utf-8", errors="replace")
        stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")
        return process.returncode, stdout, stderr


def _stdout_json(stdout: str) -> Any | None:
    """Parse the final non-empty stdout line as JSON when it is valid."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return None


def _script_result(
    command: list[str], inputs: ScriptInput, context: ExecutionContext
) -> tuple[int, str, str, Any | None]:
    """Run one script step and append the credential digest when requested."""
    return_code, stdout, stderr = _run_subprocess(command, inputs, context)
    # The JSON line is parsed from the submitted code's output only; the
    # digest line is appended afterwards and never parsed as output data.
    parsed = _stdout_json(stdout)
    if inputs.environment.get(USE_CREDENTIAL_ENV) == "1":
        document = _read_credential(context)
        stdout += f"{CREDENTIAL_DIGEST_PREFIX}{_credential_digest(document)}\n"
    return return_code, stdout, stderr, parsed


class ScriptBashStep(ActionStep[ScriptInput, ScriptBashOutput]):
    """Bash script shape for credential compatibility dispatch."""

    def run(self, inputs: ScriptInput, context: ExecutionContext) -> ScriptBashOutput:
        return_code, stdout, stderr, _ = _script_result(["/bin/bash", "-c"], inputs, context)
        return ScriptBashOutput(return_code=return_code, stdout=stdout, stderr=stderr)


class ScriptPythonStep(ActionStep[ScriptInput, ScriptPythonOutput]):
    """Python script shape for credential compatibility dispatch."""

    def run(self, inputs: ScriptInput, context: ExecutionContext) -> ScriptPythonOutput:
        return_code, stdout, stderr, parsed = _script_result(
            [sys.executable, "-c"], inputs, context
        )
        return ScriptPythonOutput(
            return_code=return_code, stdout=stdout, stderr=stderr, stdout_json=parsed
        )


class _VerificationHandler(BaseHTTPRequestHandler):
    """Accept only the delivered bearer token; never log request details."""

    expected_authorization = ""
    token_digest = ""

    def _verify(self) -> None:
        authorized = self.headers.get("Authorization") == self.expected_authorization
        payload = json.dumps(
            {
                "authenticated": authorized,
                "token_sha256": self.token_digest if authorized else None,
            }
        ).encode("utf-8")
        self.send_response(200 if authorized else 401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._verify()

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self._verify()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        """Suppress the default request log; it would echo the path only."""


def _verification_server(authorization: str, token_digest: str) -> ThreadingHTTPServer:
    """Bind the loopback verification server on its fixed port."""
    handler = type("_BoundVerificationHandler", (_VerificationHandler,), {})
    handler.expected_authorization = authorization
    handler.token_digest = token_digest
    return ThreadingHTTPServer(("127.0.0.1", HTTP_LOOPBACK_PORT), handler)


def _parse_body(raw: str) -> Any:
    """Parse the response body as JSON when valid, else return the text."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


class HttpRequestStep(ActionStep[HttpRequestInput, HttpRequestOutput]):
    """HTTP request shape for credential compatibility dispatch."""

    def run(self, inputs: HttpRequestInput, context: ExecutionContext) -> HttpRequestOutput:
        document = _read_credential(context)
        token = document.get("bearer_token")
        if not isinstance(token, str) or not token:
            raise StepFailure(
                1,
                "The delivered credential document has no token field.",
                "The delivered credential document has no token field.",
                code="AUTHENTICATION_FAILED",
                category=ErrorCategory.AUTHENTICATION,
            )
        authorization = f"Bearer {token}"
        server = _verification_server(authorization, _credential_digest(document))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            request = Request(inputs.url, method=inputs.method, headers={"Authorization": authorization})
            try:
                with urlopen(request, timeout=inputs.timeout_seconds) as response:  # noqa: S310 - loopback fixture URL
                    status_code = response.status
                    raw_body = response.read().decode("utf-8", errors="replace")
                    headers = {key: value for key, value in response.headers.items()}
            except HTTPError as error:
                status_code = error.code
                raw_body = error.read().decode("utf-8", errors="replace")
                headers = {key: value for key, value in error.headers.items()}
            except URLError:
                # The schema pins the URL to the port this step binds, so an
                # unreachable server is an internal failure; report it as a
                # structured step failure rather than an unhandled exception.
                raise StepFailure(
                    1,
                    "The verification server did not answer the request.",
                    "The verification server did not answer the request.",
                    code="DEPENDENCY_FAILED",
                    category=ErrorCategory.DEPENDENCY,
                ) from None
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        return HttpRequestOutput(
            status_code=status_code,
            body=_parse_body(raw_body),
            headers=headers,
            elapsed=time.monotonic() - started,
        )


def create_bindings() -> StepBindings:
    """Bind every packaged custom-runner step this fixture declares."""

    return load_plugin_bindings("credential_steps")
