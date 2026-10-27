# Testing reference

The SDK's testing harness gives step authors a local pytest suite: validate
declared contracts and execute Python Action, Control, and Trigger
implementations through the shared SDK execution boundary, without a cluster
or platform services.

Install the SDK with the `testing` extra so pytest and the auto-loaded
fixtures are available:

```sh
pip install "syntara-plugin-sdk[testing]"
```

The function core (`invoke_step`, the factories, conformance, chaining,
redaction) imports and runs without pytest; the extra only adds pytest and
the auto-discovered fixtures.

Everything below is also available as plain importable functions; the pytest
fixtures are thin wrappers over them.

## The four fixtures

Installing the package registers a pytest plugin (the `pytest11` entry point),
so these fixtures are available in any test session with no conftest wiring.
Their names are public API.

| Fixture | Backed by | Use |
| --- | --- | --- |
| `manifest_validator` | `syntara_plugin.contracts` canonical bundle | `manifest_validator(document, kind=...)` raises `ManifestValidationError` on invalid documents; `.check(...)` returns diagnostics; `bundle=` accepts a step-type owner's canonical bundle. |
| `compile_manifest` | `syntara_plugin.sdk` compiler | `compile_manifest(path_or_plugin, targets=None, ...)` compiles a workspace (path to `plugin.yaml` or its directory) or an in-memory `Plugin` model; path compiles default to `require_image_bindings=False` because local tests build no images. |
| `mock_execution_context` | `ExecutionContext` | `mock_execution_context(invocation_id=..., workflow_run_id=..., runtime_context=..., cancelled=False)` builds one locally approved context: valid, free of real credentials, authorized only for local test execution. |
| `step_bindings` | `StepBindings` + contract drift validation | `bind(step)` for any step (a locally installed manifest verified with `check_step_contract`); `bind_step(entry_point, workspace=...)` for custom-runner Step types (compile, install, drift-check); `resolve(identity_or_entry_point)` returns a ready binding. |

Action steps execute only through an installed manifest: the SDK's
`require_executable_contract` provenance gate rejects fabricated descriptors,
so `bind_step` is the only path to an executable custom-runner Step binding. Binding an
Action with `bind` is allowed for drift checks but is not executable.

## Invocation helpers

```python
from syntara_plugin.testing import invoke_step, invoke_step_in_container

outcome = invoke_step(binding, {"title": "Broken build"}, context=ctx)
```

- `invoke_step` runs the direct path: input validation, the typed
  `BaseStep.execute()` operation (currently delegated to the author-defined
  `run()` hook), output-schema and canonical-JSON validation, then
  `StandardOutputWrapper` construction. No `ContainerInvocation` or gRPC
  server is used. The helper calls public `runtime.invoke`.
- `invoke_step_in_container` runs the same registered implementation over a
  real in-process gRPC server on `127.0.0.1` through the container adapter.
  The adapter calls public `runtime.invoke`; authors never construct a
  `ContainerInvocation`. Public direct and gRPC callers share this invocation
  function. The test helper's direct path calls `StepExecutor` itself. Two
  boundaries apply only to the container helper:
  a preset `cancelled` context is not transported (the container path delivers
  cancellation through the transport), and business results above the
  container ABI's inline bound (1 MiB) are externalized only when an object
  store is configured. Test pre-cancelled contexts and oversized outputs with
  the adapter-specific tests.
- Both accept a `StepBinding`, a step class or instance, or a
  `module:Class` entry point, and return a `StepOutcome`.

`StepOutcome` normalizes both paths: `.ok`, `.status`, `.status_message`,
`.output`, `.error` (the complete structured error result), `.run_calls` (business-hook spy),
`.raised` (executor exception or remote failure), `.transport`, and `.raw`.
Invalid output or serialization raises `StepExecutionError` before wrapper
construction; `invoke_step` stores it in `.raised`, while the container
adapter emits `ContainerFailure` because there is no valid wrapper to
serialize. The container helper exposes that event as `ContainerRemoteError`
in `.raised`. A `StepFailure` produces a nonzero wrapper and appears in
`.status` and `.error` on both paths. Assert outcomes through these accessors
only; never through raw wrapper fields.

Unexpected exceptions raised by the business hook are sanitized into
`StepExecutionError` on the direct path and `ContainerFailure` on the container
path; neither produces a wrapper.

Runtime contract tests cover inline and asset-resolved output and error schemas,
trusted schema digest matching, structured error round trips, inline and
object-reference reconstruction, and rejection of invalid error results. Transport and object-store
errors are checked for safe normalization without retaining backend exceptions.

`assert_outcomes_equivalent(direct, container)` asserts the two SDK paths
agree on success, status, output, and error envelope (digests deliberately
excluded; raised failures compare their failure codes when both carry one).

## Sample payloads and secret redaction

`syntara_plugin.testing.payloads` ships the seven contract categories as
`PayloadCase` data: valid, missing-required, wrong-type, invalid-enum,
schema-invalid output, a raised exception, and sensitive credential
references, with `ReferenceControlStep`, `FailingReferenceStep`, and
`schema_violating_binding()` as the matching stand-ins. The failure payload
currently exercises the no-valid-outcome exception path. Use `StepFailure` to
test a valid nonzero wrapper.

```python
import logging

from syntara_plugin.testing import secret_sentinel, assert_no_secrets

token = secret_sentinel()
with caplog.at_level(logging.INFO):
    outcome = invoke_step(binding, {"title": "x", "api_token": token}, context=ctx)
assert_no_secrets(outcome, [token], caplog=caplog)
```

`assert_no_secrets` scans the status message, error message, raised exception,
serialized output, raw envelope, and the pytest log capture. When scanning
logs, wrap the invocation in `caplog.at_level(logging.INFO)` (or higher):
pytest only captures records that pass the root logger's effective level,
which defaults to WARNING, and INFO is exactly where a chatty step leaks
credentials.

Credential values must not appear in outputs, wrapper fields, raised errors,
or logs. `assert_no_secrets` detects leaks in both outcomes and captured logs;
it does not sanitize them. Keep secrets out of exception text and log messages
on both invocation paths.

## Conformance in one call

```python
from syntara_plugin.testing import check_step_conformance

report = check_step_conformance(
    binding,
    valid_cases=[{"title": "Broken build"}],
    invalid_cases=[{}, {"title": "x", "priority": "urgent"}],
    context=ctx,
)
assert report.passed, report.failures
```

The check proves three properties: invalid input blocks the business hook,
valid input reaches it exactly once, and the direct and container paths yield
equivalent outcomes.

## Chaining steps

`chain_steps(bindings, inputs, seams=[...])` executes steps sequentially with
explicit output-to-input seams and fails fast naming the broken link.
`check_seam_compatible(source, target)` statically proves the seam with the
same drift engine used for manifests. This is deliberately not a workflow
engine: no branching, parallelism, fan-in, retries, or persistence. When a
chain needs control flow, implement it as a `ControlStep.run()` and test the
step.

## Generated tests

`syntara-plugin test-init` scaffolds a test module per custom-runner Action
target from the compiled manifest and its schemas: the entry point, one valid
case, derived invalid cases, and the standard property tests. Generated files
refuse silent overwrite (`--force` to regenerate) and are deterministic.

A generated suite never fails on first run: the derived invalid-input test
runs immediately, while the valid-input and equivalence tests are skipped
until you replace the scaffolded placeholder with a business-valid payload
(only the implementation's business rules can make a schema-valid placeholder
business-valid). When the placeholder also violates the declared schema, the
skip marker's TODO quotes the schema errors.

```sh
syntara-plugin test-init path/to/plugin.yaml
pytest tests -q
```

## A runnable example

[`../examples/python-steps`](../examples/python-steps) contains one step per
category and a test suite using everything above. Run it from the
repository root:

```sh
uv run --no-sync pytest examples/python-steps/tests -q
```
