# Testing reference

The SDK's testing harness gives step authors a local pytest suite: validate
declared contracts and execute Python Action, Control, and Trigger
implementations through the shared SDK executor, without running AO, EP, or a
cluster.

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

- `invoke_step` runs the direct path: `validate_step_inputs` (kind
  dispatched), the typed `run()` hook, execution-time output-schema
  validation, and `StandardOutputWrapper` construction. No
  `ContainerInvocation`, no gRPC server.
- `invoke_step_in_container` runs the same registered implementation over a
  real in-process gRPC server on `127.0.0.1` through the container adapter.
  Authors never construct a `ContainerInvocation`. Two boundaries apply only
  to this path: a preset `cancelled` context is not transported (the container
  path delivers cancellation through the transport), and business results
  above the container ABI's inline bound (1 MiB) fail with `ABI_VIOLATION`
  because the harness wires no object store. Test pre-cancelled contexts and
  oversized outputs with `invoke_step`.
- Both accept a `StepBinding`, a step class or instance, or a
  `module:Class` entry point, and return a `StepOutcome`.

`StepOutcome` normalizes both paths: `.ok`, `.status`, `.status_message`,
`.output`, `.error` (code/message/retryable), `.run_calls` (business-hook spy),
`.raised` (executor or business exception), `.transport`, and `.raw`.
Executor-level failures (`StepExecutionError`, business exceptions) are
captured in `.raised` so both transports compare uniformly. Assert outcomes
through these accessors only; never through raw wrapper fields.

`assert_outcomes_equivalent(direct, container)` asserts the two SDK paths
agree on success, status, output, and error envelope (digests deliberately
excluded; raised failures compare their failure codes when both carry one).

## Sample payloads and secret redaction

`syntara_plugin.testing.payloads` ships the seven contract categories as
`PayloadCase` data: valid, missing-required, wrong-type, invalid-enum,
schema-invalid output, deliberate failure, and sensitive credential
references, with `ReferenceControlStep`, `FailingReferenceStep`, and
`schema_violating_binding()` as the matching stand-ins.

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

One asymmetry to know: a secret placed in a deliberate-failure message (a
raised `ContainerInvocationError`) is caller-visible on the direct path, but
the container transport deliberately drops untrusted-workload message text
(`ContainerRemoteError` carries only the failure code), so
`assert_no_secrets` can only catch that leak class through `invoke_step`.
Test deliberate-failure messages on the direct path.

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

The check proves the ticket's three properties: invalid input blocks the
business hook, valid input reaches it exactly once, and the direct and
container paths yield equivalent outcomes.

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

[`../examples/existing-step`](../examples/existing-step) is the checked-in
end-to-end prototype. Its suite compiles the real plugin workspace, loads each
manifest entry point, checks invalid inputs, compares direct and in-process
container outcomes, and builds deterministic plugin and catalog artifacts:

```sh
uv run --no-sync pytest examples/existing-step/tests -q
```
