# Syntara Plugin Contracts

This directory is the language-neutral source of truth for the Syntara Plugin SDK contracts.

- `schemas/` contains versioned JSON Schema documents shared by every SDK.
- `fixtures/` contains valid and invalid conformance examples shared by every SDK.
- `proto/` contains the language-neutral `ContainerService` ABI source shared
  by plugin workload runtimes and the Execution Plane.

The `http-operation.schema.json` contract defines the deliberately small,
data-only HTTP operation format for a `platform-runner` action: a method,
an origin-less relative path template, and bounded request/response maps whose
values are JSON Pointers or JSON literals. It does not define HTTP transport,
credentials, a dynamic origin, headers, or executable expressions.
Source path templates cannot contain percent escapes. The shared runtime mapper
encodes a checked input value as exactly one path segment, preventing a value
from becoming a separator, query, fragment, or dot segment.

`http-runner-context.schema.json` defines the runtime-only form after Syntara
has resolved an installed action: the context label `driver: http.v1`, the
inline operation, and the output-schema digest. This label is not the source
action's runner or implementation reference. The context has no endpoint,
credential, header, image reference, asset reference, or executable expression.
The generic work item retains this as an optional bounded non-secret
`runtimeContext` object so other approved runners can later define their own
constrained context contract.

The generic `ContainerWorkloadRequest` has two mirrored `requestHash` fields:
one on the work item and one on its policy snapshot. They are the SHA-256
digest of the complete request after removing only those two fields, using
UTF-8 JSON with sorted object keys, compact separators, and no non-finite
numbers. The Python contracts package provides the reference implementation;
future language SDKs must implement the same form before they compose or verify
container requests. The hash is not a credential or signature. It detects a
request changed after trusted control-plane composition; identity and transport
authentication remain separate concerns.

The pinned `fixtures/catalog-fixture-bundle.json` covers the first
control-plane contract handoff: catalog source configuration and safe status,
immutable catalog entries, credential recipes, trigger drivers/mappings, and
installed identities. It includes a valid 20-plugin index plus deterministic
rejection cases for expiry, replay, source mismatch, broken predecessors,
unsupported contract versions, and duplicate identities. It is a local
conformance corpus only: it neither contacts a registry nor verifies OCI
signatures, persists source state, installs a plugin, or starts a workload.

`syntara-plugin-contracts` ships this corpus with its schemas. Python consumers
use `load_fixture_bundle()` to load its manifest-listed documents and verify the
fixture-content digest before running their own smoke checks. Consumers in
other languages consume the same files from the released contract bundle.

The `subworkflow-*` fixtures cover synchronous subworkflow composition. A
parent-side call is a `Control` whose `spec.invocation.kind` is
`workflow-invocation` and whose runtime is the `workflow.v1` platform runner;
a child-side entry point is a `Trigger` on the `subworkflow.v1` driver. Both
valid fixtures use existing contracts unchanged. The three invalid fixtures
pin known gaps rather than authoring mistakes: `spec.invocation.workflow` is a
version-less qualified identifier and cannot pin a published child version,
`TriggerDriver.spec.transport` has no value for a driver fired by a parent
workflow, and `InstalledIdentity.spec.target.kind` excludes `control`. Each is
rejected for exactly one reason; see
[`examples/subworkflow-composition`](../examples/subworkflow-composition) for
the runnable counterpart.

Language-specific SDKs under `sdks/` may package these files for local access, but they must not maintain copies or alter contract semantics. See [ADR 0001](../.sdlc/adrs/0001-language-sdk-layout.md) for the repository layout decision.

The Python contracts build stages this canonical tree into its wheel and source
distribution. It does not commit a second source copy: an extracted source
distribution contains the staged files only so that it can build without a
checkout of this repository.

The JSON Schema documents remain the normative durable artifact and work-item
contracts. The protobuf file is the normative in-container application-data
protocol: it selects a step revision, carries bounded JSON input, bounded
non-secret runtime context, and mounted credential-file paths, and returns
progress or one terminal result/failure. It does not select an image, command,
execution pool, endpoint, or credential value.

`runtime.schema.json` defines the canonical `StandardOutputWrapper` JSON shape
with exactly four required lower-camel keys: `result`, `statusCode`,
`statusMessage`, and `errorResult`. The runtime model is immutable after
construction; the schema rejects missing or additional serialized fields.
These keys serialize the logical `Result`, `StatusCode`, `StatusMessage`, and
`ErrorResult` fields, respectively.
`result` is the business result, `statusCode` is an unsigned 32-bit outcome
code, and `statusMessage` is a bounded, safe summary. `statusCode` is not an
HTTP status. These field names and meanings are independent of the container
protocol's wire encoding.

A zero `statusCode` is success: `result` must be present and non-null,
`errorResult` must be `null`, and `result` is validated against the step's
`spec.output` schema. A nonzero `statusCode` is a deliberate failure that is
still a valid completed outcome. It requires a structured `errorResult` and
may have a `null` result. `errorResult` requires `category`, stable uppercase `code`, a
bounded safe `message`, and boolean `retryable`. Its message must not expose
credentials, secrets, raw sensitive provider responses, or tracebacks. The
category identifies the broad failure class; the code identifies its stable,
machine-readable reason. `retryable` communicates whether retrying may succeed.

`spec.output` remains the schema for the successful business result, never for
the wrapper. The separate `spec.error` schema is optional. When present, the
runtime validates the entire error object against that schema and the SDK base
schema, and the declared schema may add step-specific fields. When absent, the
SDK uses a closed fallback schema that accepts only the base fields; custom
step-specific fields require a declared `spec.error`. A failed container result
carries the complete error object and the canonical digest of the effective
error schema. If an error, including an input-validation error, does not satisfy
the effective schema, no valid wrapper exists. A wrapper cannot represent an
invocation that produced no valid outcome. In that case the runtime emits a
`ContainerFailure` instead: its `outcome` is `not_started`, `not_completed`, or
`unknown`. `not_started` means execution did not begin; `not_completed` means
execution began and is known not to have completed; `unknown` means the runtime
cannot determine whether execution completed. These values describe execution
completion knowledge, not retryability. A `ContainerFailure` is not a deliberate
step result and carries no `StandardOutputWrapper`.

The wrapper schema does not redefine protobuf transport fields. The
`ContainerService` ABI remains the protocol contract for transporting runtime
events; any transport mapping must preserve the distinction between deliberate
failure and no valid outcome.
