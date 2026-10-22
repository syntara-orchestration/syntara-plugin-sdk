# Run a Step locally

This page explains the `syntara-plugin run` command: how to validate an
invocation's inputs and execute one custom-runner Step on your own machine,
either from your workspace source or against a running workload runtime,
without a cluster or a full end-to-end test suite.

## Objective

After this page you can:

- print the input contract of one Step and a schema-shaped template for it;
- validate inputs before anything executes;
- run the Step from source and read its result;
- run the same Step against an already-running workload runtime.

## Prerequisites

- A plugin workspace with a `plugin.yaml` that declares at least one
  custom-runner Step target. The
  [python-steps example](../examples/python-steps) is used throughout; clone
  this repository to follow along.
- The CLI installed: see [Installation](installation.md).

## Quickstart

From the repository root, run one of the example's targets without inputs
(the workspace declares two, so the target is named):

```sh
syntara-plugin run --manifest examples/python-steps/plugin.yaml create-issue
```

The command does not execute anything. It prints a template on stdout
(sensitive fields are omitted rather than filled with their declared
defaults):

```json
{
  "priority": "low",
  "title": ""
}
```

and a field summary plus guidance on stderr:

```text
Inputs for example.python-steps.create-issue (Action):
  api_token  string   optional, default ***, sensitive
                      Secret token; never echoed into outputs or errors.
  priority   string   optional, default "low", one of: low, medium, high
  title      string   required

The template is schema-shaped, not business-valid. Fill in real values.

Nothing was executed. Next:
  syntara-plugin run --manifest examples/python-steps/plugin.yaml create-issue --input-file input.json
```

Save the template, fill in real values, and run again:

```sh
syntara-plugin run --manifest examples/python-steps/plugin.yaml create-issue > input.json
$EDITOR input.json
syntara-plugin run --manifest examples/python-steps/plugin.yaml create-issue --input-file input.json
```

```text
Target:         example.python-steps.create-issue
Mode:           source
Input:          {"api_token": "***", "priority": "low", "title": "Broken build"}
Invocation:     run-e5f3a6bc796e40e28e2cc298d7863c1f
Status:         0
Status message: Completed

Output:
{
  "issue_url": "https://issues.example.test/broken-build"
}
```

Labels align into a column and are bold on an interactive terminal; output is
plain when piped or redirected, and `NO_COLOR` disables the bolding.

The exit status is `0` when the Step completes with status `0`.

## Selecting the target

The command resolves the target to run in this order:

1. the full Step identity, for example `example.python-steps.create-issue`;
2. the `module:Class` entry point, for example `python_steps.create_issue:CreateIssueStep`;
3. a unique short name, for example `create-issue`.

When you omit the target, the command infers it only when the workspace
declares exactly one runnable target. Otherwise it lists the choices and exits
`2` without executing:

```text
error: this workspace declares more than one runnable target
  example.python-steps.create-issue    Action   python_steps.create_issue:CreateIssueStep
  example.python-steps.record-opened   Trigger  python_steps.record_opened:RecordOpenedStep

Next:
  syntara-plugin run example.python-steps.create-issue
```

Only custom-runner targets can run locally. A platform-runner target is a
declarative manifest executed by Syntara's managed runner; the command
explains this and suggests `syntara-plugin inspect` instead of executing it.

The workspace defaults to `./plugin.yaml` in the current directory. Pass
`--manifest PATH` (a file or its directory) to name another one, or set
`manifest` in `syntara-plugin.yaml`; see
[Build settings reference](build-settings.md). There is no upward search: the
command never selects a workspace you did not name.

## Supplying inputs

Inputs arrive in one of three ways:

| Path | Command | Notes |
| --- | --- | --- |
| Template | no `--input`/`--input-file` | Prints the template and exits `2`; nothing executes. |
| Inline JSON | `--input '{"title": "Broken build"}'` | Convenient for quick, non-sensitive values. |
| File or stdin | `--input-file input.json` / `--input-file -` | Preferred for larger payloads and secrets. |

Before anything executes, the parsed input is validated against the compiled
manifest's input schema (JSON Schema Draft 2020-12; format annotations are
checked for the formats the installed `jsonschema` build validates). Every
violation is reported with its RFC 6901 JSON Pointer, and no server is started:

```text
error: input does not satisfy the declared schema for example.python-steps.create-issue
  inputs.title: required property is missing
  inputs.priority: 'urgent' is not one of ['low', 'medium', 'high']

Nothing was executed. Next:
  fix --input and rerun
  or run without --input/--input-file to print a template
```

The input must be a JSON object, may not contain the JSON constants `NaN` or
`Infinity`, and must stay within the container ABI's 1 MiB bound.

### The template is a skeleton, not a payload

The template is deterministic: for each property it takes the first declared
value from `examples[0]`, `default`, `const`, `enum[0]`, a well-known format
hint (for example `date-time`), or a typed empty value (`""`, `0`, `false`,
`[]`, `{}`). Required properties always appear; optional properties appear
only when the schema declares a value for them; required nested objects are
expanded.

The template is schema-shaped, **not business-valid**. It does not satisfy
`pattern`, `minItems`, cross-field rules, external identifiers, or credential
requirements, and it is never executed automatically. The validator's messages
are the guide for completing it.

### Handling secrets

Fields marked `writeOnly`, declared with `format: password`, or named like
credentials (including hyphenated and header-style names such as
`X-Api-Key`, and inside `anyOf`/`oneOf`/`allOf`) are marked `sensitive` in
the summary, and their values are redacted everywhere they could surface:

- the input echo shows them masked (`"api_token": "***"`), in human output and
  in `--json`, including values nested in objects, arrays with sensitive
  `items`, maps whose `additionalProperties` or `patternProperties` schema
  declares every value secret, declarations reached through a local `$ref`,
  and undeclared keys with credential-like names;
- validation errors name the field and the constraint only, never the value,
  and a secret value pasted into a non-sensitive field is scrubbed from the
  error message;
- a step that interpolates a sensitive value into an exception message, a
  deliberate failure message, its output (values and mapping keys), an
  object reference, an error code, or a progress update gets that value
  scrubbed to `***` before anything is printed, including the
  `Error detail:` traceback;
- the template and field summary never print a declared default, example, or
  enum value of a sensitive field.

Scrubbing matches the value case-insensitively. Redaction is driven by the
declared schema and the values you supplied, so it cannot catch everything:
a secret hard-coded in step source, transformed before it is echoed (for
example JSON-escaped or otherwise re-encoded), or placed in an undeclared
field with an innocent name may still appear. In image mode the scrub set
comes from the local workspace's schema, per the documented same-workspace
assumption. Name-based detection is deliberately conservative and may
over-mask a public field like `authorization_url`. Prefer `--input-file`
over `--input` so secret values stay out of your shell history and other
local users' view of `/proc/<pid>/cmdline` in the first place. When a
managed container dies before becoming healthy, its last log lines are
printed to explain why; those lines come from the image itself and are not
secret-scrubbed, so they carry whatever the image printed.

## Execution modes

Both modes execute through the SDK's `ContainerClient` over real gRPC, call
`Health` first to verify the container ABI and that the runtime advertises the
requested Step, and use a 60 second invocation timeout with an empty runtime
context and no credential file paths.

### Source mode (default)

```sh
syntara-plugin run --manifest examples/python-steps/plugin.yaml create-issue --input-file input.json
```

Source mode compiles the workspace with the same compiler as `validate`,
makes the workspace importable, binds the implementation through the trusted
installed-manifest path (the same compile, install, and drift-check used by
the [testing harness](testing.md)), and serves an in-process gRPC runtime on
loopback. The Step executes over the container ABI exactly as the platform
would invoke it, and the runtime stops when the result arrives.

Making the workspace importable means its root directory is inserted at the
front of the process's module search path for this invocation, exactly like
the generated test suites' `conftest.py` shim: your `module:Class` entry
point must resolve against the workspace, and a workspace file that shadows a
standard-library or SDK module name would take precedence during the run.

Action, Control, and Trigger targets with source manifests are supported.

### Image mode

```sh
syntara-plugin run --mode image \
  --manifest examples/python-steps/plugin.yaml create-issue --input-file input.json
```

Image mode executes through `ContainerClient` against a workload runtime
serving the container ABI. **By default the CLI manages the container for
you**: it detects Podman (preferred) or Docker, verifies the workspace's
declared workload image (`spec.workload.image`) is available locally, runs
it, waits for its health endpoint, executes, and removes the container
afterwards. The report shows the image it used:

```text
Target:         example.python-steps.create-issue
Mode:           image
Image:          python-steps-workload
```

The managed container publishes an ephemeral loopback port secured with
mutual TLS generated for this invocation (one throwaway CA plus server and
client certificates, removed afterwards), which works everywhere published
ports do, including Docker Desktop. openssl is required for the managed
path: there is deliberately no cleartext fallback, because a fixed-port
unauthenticated listener would let any local process impersonate the runtime.
The managed path never builds and never pulls images: a missing image is an
error with the fix, not a side effect, because building requires the
author-owned Containerfile and bindings factory the CLI cannot infer. The
image must be tagged with the logical workload ID your `plugin.yaml` declares
under `spec.workload.image`; after `build --with-workload` pushes
`repository:tag`, tag the local image accordingly, for example
`podman tag repository:tag python-steps-workload`. A
container whose process exits before its runtime becomes healthy is reported
immediately, with its exit status and the last lines of its log, instead of
waiting out the startup window. The
managed path executes the image your manifest names with the same trust as
running it by hand: only point it at workload images you built yourself. If
neither engine is usable, or the container cannot start, the command reports
one clear error with the next step instead of a traceback. If the CLI is
killed forcefully (`kill -9`) a managed container, its temporary TLS
directory (`syntara-run-tls-*`), or a source-mode runtime directory
(`syntara-run-runtime-*`) can be left behind; find and remove the container
with `podman ps --filter name=syntara-run-` and `podman rm -f <name>`, and
the directories from your temporary directory.

To manage the runtime yourself, pass `--address` (and `--tls-*` for mutual
TLS); then the CLI only connects and never starts anything. Two ways to
obtain such a runtime:

1. **The runtime server console script**, entirely on your machine. Write a
   runtime config naming a bindings factory for your steps:

   ```json
   {
     "abi": "syntara.container/v1alpha1",
     "adapter": {"bindingsFactory": "my_plugin.workload_runtime:create_bindings"}
   }
   ```

   ```sh
   SYNTARA_RUNTIME_CONFIG=runtime.json PYTHONPATH=. syntara-plugin-runtime-server
   ```

   The factory returns `StepBindings` that install your steps through
   `InstalledStepManifest.from_manifest`; the
   [runtime image integration test](../sdks/python/tests/integration/test_runtime_image.py)
   contains one complete, copy-pasteable factory, and
   [the runtime and OCI model](runtime-and-oci.md) describes the adapter
   contract. This is also the image pattern the managed path expects:
   install the `syntara-plugin-contracts` wheel on a Python base image, copy
   your plugin sources and runtime config, and set `SYNTARA_RUNTIME_CONFIG`
   and `PYTHONPATH`.

2. **A Podman container with a published port and mutual TLS.** A non-loopback
   runtime listener requires mTLS by design. Publish
   `127.0.0.1:<host-port>:50051`, configure the server's TLS material through
   `SYNTARA_RUNTIME_GRPC_TLS_CERT`, `SYNTARA_RUNTIME_GRPC_TLS_KEY`, and
   `SYNTARA_RUNTIME_GRPC_TLS_CLIENT_CA`, generate one local CA that signs both the
   server and client certificates, and connect with:

   ```sh
   syntara-plugin run --mode image --address 127.0.0.1:<host-port> \
     --tls-ca ca.crt --tls-cert client.crt --tls-key client.key \
     --manifest examples/python-steps/plugin.yaml create-issue --input-file input.json
   ```

   The three `--tls-*` options must be supplied together, mirroring the
   runtime server's own all-or-nothing TLS rule.

In both paths the local compiled manifest supplies the input schema and
target identity, and the documented assumption is that the runtime was built
from the same workspace. If it was not, the health check and the
advertised-identity check produce an actionable error instead of a confusing
failure.

`--address` and the `--tls-*` options are rejected in source mode: source mode
owns its loopback transport.

## Reading the output

A successful run prints the target identity, mode, the input that executed
(with sensitive fields masked), the invocation identifier, status, and output
JSON. The `--json` document is pretty-printed and additionally carries the
input source (`inputSource`) for programmatic consumers. A terminal failure is
a valid outcome, not a transport error: it keeps its failure code, message,
retryability, and outcome, and exits `1`. A step signals a deliberate failure
by raising `ContainerInvocationError`, and its code, message, and
retryability are preserved:

```text
Target:      demo.tracker.submit
Mode:        source
Input:       {"message": "hello"}
Invocation:  run-3527941430244a87b9f046a21ce4279f
Error:       REMOTE_REJECTED (retryable: true)
Message:     The tracker rejected the request.
Outcome:     not_completed
```

A step that raises an unexpected exception is caught and reported as a safe
`OUTCOME_UNKNOWN` failure: the container ABI never carries exception text,
because it may contain secrets or internal details. In source mode the
exception happened in your own process, so the CLI captures the traceback
locally and attaches it:

```text
Error:       OUTCOME_UNKNOWN (retryable: false)
Message:     The plugin step did not complete.
Outcome:     unknown

Error detail:
  Traceback (most recent call last):
    ...
  RuntimeError: unexpected kaboom
```

In image mode the detail cannot be retrieved (the remote runtime discards
it); the CLI says so and suggests rerunning with `--mode source`. To debug a
failing step programmatically, the [testing harness](testing.md) also
captures the raised exception in `StepOutcome.raised`.

| Exit status | Meaning |
| --- | --- |
| `0` | The Step completed with status `0`. |
| `1` | The workspace did not compile, the Step completed with a nonzero status, or the transport failed. |
| `2` | Usage error: missing or invalid input, unknown or ambiguous target, unreadable input file, or an option used in the wrong mode. |

With `--json`, stdout carries exactly one pretty-printed JSON document on
every exit path, and stderr stays empty. The document has three shapes:

- **Completed invocation** (exit 0, or exit 1 when the Step completed with a
  failure): `ok`, `mode`, `target`, `image`, `inputSource`, `input` with
  sensitive fields masked, `invocationId`, `workflowRunId`, `status`,
  `statusMessage`, `output`, `objectReference`, `outcome`, `errorDetail`,
  and `error` with `code`, `message`, and `retryable`.
- **Usage or setup failure** (exit 2: invalid input, missing manifest, bad
  flag combination): `ok: false` and `error` with `kind`, `message`,
  `detail` (the validation lines), and `nextSteps`.
- **Compilation failure** (exit 1): `ok: false` with `diagnostics` (code,
  location, message, remediation per diagnostic) and `next`.

Template mode is unchanged by `--json`: stdout stays the raw template so
redirection keeps working.

With `--verbose`, execution progress streams to stderr as
`==> <state> <percent>% <message>` lines. The `output.json` and `output.verbose`
settings are honored with the documented command-line-over-settings
precedence.

## Troubleshooting

Every failure prints the problem and the next command. The complete map:

| Situation | Message and next step | Exit |
| --- | --- | --- |
| No `plugin.yaml` in the current directory | Run from a plugin workspace or pass `--manifest`. | 2 |
| Workspace does not compile | Compiler diagnostics, then `syntara-plugin validate`. | 1 |
| No runnable target | Only custom-runner targets run locally; see `inspect`. | 2 |
| Multiple runnable targets | The target list and a concrete `run` command. | 2 |
| Unknown or ambiguous target | The matches and the full-identity form. | 2 |
| No input supplied | The field summary, the template, and the `--input-file` command. | 2 |
| Invalid input | Every JSON Pointer error, then fix-forward guidance. | 2 |
| Input not a JSON object, malformed, or oversized | The expectation and the limit. | 2 |
| Transport options in source mode | `--address` and `--tls-*` require `--mode image`. | 2 |
| Partial TLS material | The three options must be supplied together. | 2 |
| Target is platform-runner | Declarative targets cannot run locally; see `inspect`. | 2 |
| Step module fails to import | The exception with its file and line, then `validate`. | 1 |
| openssl missing for the managed container | Install openssl, or pass `--address` to a runtime you started. | 1 |
| Workload container exits before health | Its exit status and last log lines, then `--mode source`. | 1 |
| Image address unreachable | The address, then start-the-image or `--address`. | 1 |
| ABI mismatch | The supported ABI, then rebuild the image. | 1 |
| Identity not advertised | The advertised list, then check the image's workspace. | 1 |
| Nonzero terminal status | Status, error code, message, retryability. | 1 |
| gRPC or protocol failure | The failure code and a short explanation. | 1 |

## Using the template with an AI assistant

The command is deterministic and AI-free, but it is designed to pair with one.
A productive loop:

1. `syntara-plugin run TARGET > input.json` to get the schema-shaped template.
2. `syntara-plugin inspect` to read the full manifest, including descriptions
   and the output contract.
3. Ask your assistant to fill the template using both, stating the business
   context (which repository, which environment, which identifiers).
4. Review the filled file yourself; supply secrets through the file, never the
   command line.
5. Rerun `run`; the validator reports anything the assistant got wrong.

The CLI never invents business values: JSON Schema can prove structural
validity, not business validity, so execution always requires your explicit,
reviewed input.
