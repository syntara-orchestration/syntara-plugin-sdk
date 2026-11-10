# Testing reference

The SDK's testing harness gives step type authors a local pytest suite: validate
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

For a single ad-hoc invocation without a pytest suite, use
`syntara-plugin run`; see [Running locally](running-locally.md). Source mode
runs the same compile, install, drift-check, and container-adapter path this
harness uses, from the CLI.

## The four fixtures

Installing the package registers a pytest plugin (the `pytest11` entry point),
so these fixtures are available in any test session with no conftest wiring.
Their names are public API.

| Fixture | Backed by | Use |
| --- | --- | --- |
| `manifest_validator` | `syntara_plugin.contracts` canonical bundle | `manifest_validator(document, kind=...)` raises `ManifestValidationError` on invalid documents; `.check(...)` returns diagnostics; `bundle=` accepts a step-type owner's canonical bundle. |
| `compile_manifest` | `syntara_plugin.sdk` compiler | `compile_manifest(path_or_plugin, targets=None, ...)` compiles a workspace (path to `plugin.yaml` or its directory) or an in-memory `Plugin` model; path compiles default to `require_image_bindings=False` because local tests build no images. |
| `mock_execution_context` | `ExecutionContext` | `mock_execution_context(invocation_id=..., workflow_run_id=..., runtime_context=..., cancelled=False)` builds one locally approved context: valid, free of real credentials, authorized only for local test execution. |
| `step_bindings` | `StepBindings` + contract drift validation | `bind(step)` for any step type (a locally installed manifest verified with `check_step_contract`); `bind_step(entry_point, workspace=...)` for custom-runner step types (compile, install, drift-check); `resolve(identity_or_entry_point)` returns a ready binding. |

Custom-runner step types execute only through an installed manifest: the SDK's
`require_executable_contract` provenance gate rejects fabricated descriptors,
so `bind_step` is the only path to an executable custom-runner step type binding. Binding an
Action, Control, or Trigger with `bind` is allowed for drift checks but is not executable.

## Testing credentials

`build_execution_context` and `mock_execution_context` accept
`credential_file_paths={recipe_id: str(path)}` and `credential_directory=path`.
Use a private temporary directory and fake JSON credential documents. The path
must be directly inside that directory. Bind the step type with `bind_step` so its
compiled integration and recipe declarations authorize the identifier. The
same context works with `invoke_step` and `invoke_step_in_container`; the latter
sends a fake document over its loopback callback. Custom in-process providers
must implement the restricted provider contract described in
[credential access](runtime-and-oci.md#credential-access).

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
- Both accept a `StepBinding`, a step type class or instance, or a
  `module:Class` entry point, and return a `StepOutcome`.

`StepOutcome` normalizes both paths: `.ok`, `.status`, `.status_message`,
`.output`, `.error` (the complete structured error result), `.run_calls` (business-hook spy),
`.raised` (executor exception, terminal failure, or remote failure), `.transport`, and `.raw`.
Invalid output or serialization raises `StepExecutionError` before wrapper
construction; `invoke_step` stores it in `.raised`, while the container
adapter emits `ContainerFailure` because there is no valid wrapper to
serialize. The container helper exposes that event as `ContainerRemoteError`
in `.raised`. A `StepFailure` produces a nonzero wrapper and appears in
`.status` and `.error` on both paths. A `ContainerInvocationError` raised
from `run()` is a deliberate terminal failure: `invoke_step` stores the
exception itself in `.raised`, and the container helper exposes the workload
failure as `ContainerRemoteError` in `.raised`; both keep its code, message,
retryability, outcome, and provider code. Assert outcomes through these accessors
only; never through raw wrapper fields.

Unexpected exceptions raised by the business hook are sanitized into
`StepExecutionError` on the direct path and `ContainerFailure` on the container
path; neither produces a wrapper.

Runtime contract tests cover inline and asset-resolved output and error schemas,
trusted schema digest matching, structured error round trips, inline and
object-reference reconstruction, and rejection of invalid error results.
Transport-error normalization is covered in
[`test_container_protocol.py`](../sdks/python/tests/runtime/test_container_protocol.py);
object-store failure normalization is covered in
[`test_adapter.py`](../sdks/python/tests/runtime/test_adapter.py).

`assert_outcomes_equivalent(direct, container)` asserts the two SDK paths
agree on success, status, status message, output, and error envelope (digests
are excluded; raised failures compare their failure codes). The reference
suite checks input and output schema validation, the typed hook and its
short-circuiting, and valid wrappers through the SDK. Its container assertions
check that the ABI carries the same wrapper fields and the applied output
schema digest, and maps failures without a wrapper to `ContainerFailure`.

## Sample payloads and secret redaction

`syntara_plugin.testing.payloads` provides valid, missing-required, wrong-type,
invalid-enum, schema-invalid output, and deliberate-failure `PayloadCase` data.
`ReferenceActionStep`, `ReferenceControlStep`, and `ReferenceTriggerStep` use
the same Pydantic models and are importable through `module:Class` entry points.
`FailingReferenceStep` raises `StepFailure` to produce a valid nonzero wrapper;
`schema_violating_binding()` exercises invalid output without a valid wrapper.
The container-invocation tests also cover safe terminal failures and unexpected
exceptions that produce a container failure.

`VALID_INPUT_CASES` retains the historical `valid_credential_reference` entry
for compatibility. Despite its legacy name, it passes a fixed synthetic value
through the ordinary `api_token` input field; it does not test credential
delivery or access. `CREDENTIAL_REFERENCE_CASES` separately holds only an
opaque credential binding name and non-secret step inputs. The reference suite
installs that binding with a recipe, supplies a synthetic credential through
the approved local file provider, and compares direct and container outcomes,
including status, status message, output, and error envelope.

```python
import logging
from unittest.mock import Mock

import pytest
from syntara_plugin.runtime import ControlStep, ExecutionContext
from syntara_plugin.testing import assert_no_secrets, invoke_step, secret_sentinel
from syntara_plugin.testing.payloads import ReferenceInput, ReferenceOutput


class ExampleStep(ControlStep[ReferenceInput, ReferenceOutput]):
    def __init__(self, synthetic_value: str, dependency: Mock) -> None:
        self.synthetic_value = synthetic_value
        self.dependency = dependency

    def run(self, inputs: ReferenceInput, context: ExecutionContext) -> ReferenceOutput:
        logging.getLogger(__name__).info("Calling dependency")
        return ReferenceOutput(
            summary=self.dependency(self.synthetic_value), priority=inputs.priority
        )


def test_no_secret_leaks(caplog: pytest.LogCaptureFixture) -> None:
    synthetic_value = secret_sentinel()
    dependency = Mock(return_value="accepted")
    step = ExampleStep(synthetic_value, dependency)
    with caplog.at_level(logging.INFO):
        outcome = invoke_step(step, {"title": "test"})

    assert outcome.ok
    dependency.assert_called_once_with(synthetic_value)
    assert_no_secrets(outcome, [synthetic_value], caplog=caplog)
```

The test injects a synthetic value and a mocked dependency solely to exercise
leak detection; it does not model credential delivery.

`assert_no_secrets` scans the status message, error message, raised exception,
serialized output, raw envelope, and the pytest log capture. When scanning
logs, wrap the invocation in `caplog.at_level(logging.INFO)` (or higher):
pytest only captures records that pass the root logger's effective level,
which defaults to WARNING, and INFO is exactly where a chatty step type leaks
credentials.

Credential values must not appear in outputs, wrapper fields, raised errors,
or logs. `assert_no_secrets` detects leaks in both outcomes and captured logs;
it does not sanitize them. SDK-generated diagnostics avoid credential values, but
the SDK does not redact records emitted by step code. Authors must keep returned
credentials out of their exception text and log messages on both invocation paths.

Use `secret_sentinel()` to create a synthetic value for a leak assertion. This
is separate from credential delivery. `ExecutionContext.credential(name).read()`
uses declared recipe names and the container harness exercises the callback. Do not put credential values in inputs or runtime context.

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
step type.

`schema_without_descriptions(schema)` returns a copy of a JSON Schema with
every `description` *annotation* dropped, so two schemas that must agree — a
parent Control's declared input and a child Trigger's event payload, for
example — can be compared for their full shape (types, formats, nested
schemas, defaults, enums, `required`) rather than just property names.
Descriptions differ by perspective, so the annotation is the one part
excluded; a property *named* `description` is kept and compared like any
other.

`assert_schemas_agree(source, target, source_label=..., target_label=...)`
turns that comparison into an opt-in authoring check: it raises
`SchemaAgreementError` naming every diverging field — location plus both
values — instead of a bare inequality. Booleans are distinguished from
numbers (`const: true` and `const: 1` disagree), and the unordered keywords
`required`, `enum`, and list-form `type` compare members without regard to
order, including object and array members of `enum`. Use it for the composition seams the
contracts do not enforce, such as a parent Control's declared input against a
child Trigger's event payload, or a child workflow's terminal result against
the parent's declared output. `schema_agreement_issues(source, target)`
returns the same diagnostics as a tuple when a test needs to assert on them.

## Generated tests

`syntara-plugin test-init` scaffolds a test module per custom-runner step type
target of every kind (Action, Control, and Trigger) from the compiled
manifest and its schemas: the entry point, one valid case, derived invalid
cases, and the standard property tests. It also writes a shared
`tests/conftest.py` import shim, only when absent, so generated tests can
import the workspace and author-owned conftest content is never overwritten.
Generated files refuse silent overwrite
(`--force` to regenerate) and are deterministic.

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

[`../examples/python-steps`](../examples/python-steps) contains one step type per
category and a test suite using everything above. Run it from the
repository root:

```sh
uv run --no-sync pytest examples/python-steps/tests -q
```

[`../examples/existing-step`](../examples/existing-step) is the checked-in
end-to-end prototype. Its suite in
[`sdks/python/tests/integration/test_existing_step_e2e.py`](../sdks/python/tests/integration/test_existing_step_e2e.py)
compiles the real plugin workspace, loads each manifest entry point, checks
invalid inputs, compares direct and in-process container outcomes, and builds
deterministic plugin and catalog artifacts:

```sh
uv run --no-sync pytest sdks/python/tests/integration/test_existing_step_e2e.py -q
```
