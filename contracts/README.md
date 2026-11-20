# Syntara Plugin Contracts

This directory is the language-neutral source of truth for the Syntara Plugin SDK contracts.

- `schemas/` contains versioned JSON Schema documents shared by every SDK.
- `fixtures/` contains valid and invalid conformance examples shared by every SDK.
- `proto/` contains the language-neutral `ContainerService` ABI source shared
  by plugin workload runtimes and the Execution Plane.

Every schema in this bundle, and every schema a plugin author writes for
`spec.input`, `spec.output`, `spec.error`, or trigger `spec.configuration`,
must declare `$schema: https://json-schema.org/draft/2020-12/schema`. SDKs
reject authored schemas that declare another draft or omit the declaration.
`common.schema.json` also declares the asserted format set
(`$defs.assertedFormats`): only `date-time` (strict RFC 3339) and `uri`
(absolute URI with a scheme) are asserted when validating documents. Any other
`format` name is an annotation only, per Draft 2020-12 vocabulary semantics,
and never rejects a value.

RuntimeBinding `probeStatus` and Health `RuntimeComponent.probe_status` are
compatibility observations reported by the workload image. `matched` means
that the in-image probe reported a version matching the image annotation;
because the image supplies Health, this status is not proof of image integrity
or publisher identity. Consumers must apply their own digest, signature, and
publisher trust policy. This is a pre-release `v1alpha1` contract change: the
old `verification` field and `verified`/`unverified` values are removed, and
consumers must update together with the SDK.

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

A `ContainerWorkloadRequest` contains a work item and a policy snapshot. The
work item's `credentialHandles` are opaque platform-side references; they are
not sent to the workload. The request carries no credential leases, delivery
paths, or secret values. The Execution Plane resolves callback requests within
the attempt as described in [ADR 0011](../.sdlc/adrs/0011-credential-callback.md).
The runtime workload-request fixtures cover this shape and rejection of the
legacy file-lease field.

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
`workflow-invocation` and whose runtime is the `syntara.workflow.invoke@1.0.0`
platform-runner implementation;
a child-side entry point is a `Trigger` on the `subworkflow.v1` driver that
carries the child workflow's parameter schema in `spec.configuration` and
passes its input through unchanged. Both valid fixtures use existing contracts
unchanged. They express the composition seams but are not a confirmed fit for
AO's ANSTRAT-2347 implementation: `spec.invocation` has no trigger selector
(AO requires a `trigger_node_id`), `spec.invocation.workflow` is a
`qualifiedIdentifier` where AO references a UUID `workflow_id`, and the
parent's output is the child's declared result rather than AO's child
execution metadata. The invalid fixtures
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

The JSON Schemas are the normative durable artifact and work-item contracts.
The protobuf defines the `syntara.container/v1alpha1` in-container ABI. An
invocation is a bidirectional stream: after the start message, the workload
may request a declared CredentialRecipe ID. The host resolves it within the
current attempt and returns a bounded JSON object in a dedicated sensitive
response; credential values never appear in business events, inputs, or
runtime context. Requests and responses are correlated by invocation and
request IDs. The host must authenticate the workload and provide transport
confidentiality (mutual TLS in production). This replaces the earlier Execute
protocol under the same ABI identifier. Old `v1alpha1` peers are incompatible,
and Health cannot distinguish the two protocols. See
[ADR 0011](../.sdlc/adrs/0011-credential-callback.md).

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
`errorResult` must be `null`, and `result` is validated against the step type's
`spec.output` schema. A nonzero `statusCode` is a deliberate failure that is
still a valid completed outcome. It requires a structured `errorResult` and
may have a `null` result. `errorResult` requires `category`, stable uppercase `code`, a
bounded safe `message`, and boolean `retryable`. Its message must not expose
credentials, secrets, raw sensitive provider responses, or tracebacks. The
category identifies the broad failure class; the code identifies its stable,
machine-readable reason. The registry is closed and each code has one category:

- `validation`: `INVALID_INPUT`, `VALIDATION_FAILED`, `MISCONFIGURATION`, `UNSUPPORTED`
- `authentication`: `AUTHENTICATION_FAILED`; `authorization`: `AUTHORIZATION_FAILED`
- `business`: `BUSINESS_RULE_FAILED`
- `dependency`: `DEPENDENCY_FAILED`, `RATE_LIMITED`, `RESOURCE_EXHAUSTED`, `NOT_FOUND`
- `timeout`: `TIMEOUT_EXCEEDED`; `cancelled`: `CANCELLED`; `policy`: `POLICY_DENIED`
- `contract`: `CONTRACT_VIOLATION`; `internal`: `INTERNAL_ERROR`; `unknown`: `UNKNOWN`

### Deliberate step failure codes

Use these codes only in `errorResult`, when the step invocation completed and
the step deliberately reported a failure. The step author sets `retryable`
from the observed condition; the descriptions identify the condition and do
not prescribe the platform retry policy.

| Category | Code | Meaning |
| --- | --- | --- |
| `validation` | `INVALID_INPUT` | The supplied input cannot be accepted before the step performs its intended work. |
| `validation` | `VALIDATION_FAILED` | The step evaluated input or business data and found it invalid for this operation. |
| `validation` | `MISCONFIGURATION` | Required step configuration is missing, invalid, or inconsistent. |
| `validation` | `UNSUPPORTED` | The request asks the step to perform a recognized operation, version, or option that it does not support. |
| `authentication` | `AUTHENTICATION_FAILED` | The supplied identity or credential could not be authenticated. |
| `authorization` | `AUTHORIZATION_FAILED` | An authenticated identity lacks permission for the requested operation. |
| `business` | `BUSINESS_RULE_FAILED` | The request is valid but violates a domain rule enforced by the step or its service. |
| `dependency` | `DEPENDENCY_FAILED` | A required external dependency failed for a reason not represented by a more specific dependency code. |
| `dependency` | `RATE_LIMITED` | A dependency rejected the request because its rate limit was exceeded. |
| `dependency` | `RESOURCE_EXHAUSTED` | A dependency or required local resource has insufficient capacity to complete the request. |
| `dependency` | `NOT_FOUND` | A required remote resource does not exist or is unavailable under the requested identifier. |
| `timeout` | `TIMEOUT_EXCEEDED` | The step's intended operation exceeded its applicable timeout. |
| `cancelled` | `CANCELLED` | The operation stopped because cancellation was requested. |
| `policy` | `POLICY_DENIED` | A policy decision rejected the operation. |
| `contract` | `CONTRACT_VIOLATION` | The step or dependency produced data that violates its declared application-level contract. |
| `internal` | `INTERNAL_ERROR` | The step reached an unexpected internal condition that is safe to report but has no more specific code. |
| `unknown` | `UNKNOWN` | The step cannot safely classify the completed failure more precisely. |

`retryable` is an advisory for this failure: true means another attempt using the
same step definition and workflow input may succeed; false means no change is
expected. It does not prescribe a retry policy. This registry is separate from
the `ContainerFailure` codes used when no wrapper exists.

`spec.output` remains the schema for the successful business result, never for
the wrapper. The separate `spec.error` schema is optional. When present, the
runtime validates the entire error object against that schema and the SDK base
schema, and the declared schema may add step type-specific fields, not codes. When absent, the
SDK uses a closed fallback schema that accepts only the base fields; custom
step type-specific fields require a declared `spec.error`. Input validation errors
use the SDK base schema because they occur before the step runs. A failed container result
carries the complete error object and the canonical digest of the effective
error schema. If an error does not satisfy
the effective schema, no valid wrapper exists. A wrapper cannot represent an
invocation that produced no valid outcome. In that case the runtime emits a
`ContainerFailure` instead: its `outcome` is `not_started`, `not_completed`, or
`unknown`. `not_started` means execution did not begin; `not_completed` means
execution began and is known not to have completed; `unknown` means the runtime
cannot determine whether execution completed. These values describe execution
completion knowledge, not retryability. A `ContainerFailure` is not a deliberate
step type result and carries no `StandardOutputWrapper`.

### Container failure codes

Use these codes only in `ContainerFailure`, when the runtime cannot produce a
valid SDK wrapper. `retryable` remains an advisory for the observed failure.

| Code | Meaning |
| --- | --- |
| `INVALID_INPUT` | The runtime rejected the invocation before it could begin execution. |
| `AUTHENTICATION_FAILED` | The runtime or a required dependency could not authenticate with the supplied credentials or identity. |
| `AUTHORIZATION_FAILED` | The runtime or a required dependency denied an authenticated request. |
| `REMOTE_REJECTED` | A remote runtime or service rejected the invocation for a reason outside the other terminal-failure codes. |
| `NOT_FOUND` | A required runtime resource, endpoint, image, or remote object could not be found. |
| `CONFLICT` | The runtime detected a conflicting invocation or resource state that prevents a safe terminal result. |
| `RATE_LIMITED` | A runtime dependency or transport endpoint rejected work because its rate limit was exceeded. |
| `UPSTREAM_TRANSIENT` | A temporary upstream runtime, network, or service condition prevented completion. |
| `TIMEOUT` | The runtime or transport timed out before it could produce a valid wrapper. |
| `CANCELLED` | The invocation was cancelled before the runtime could produce a valid wrapper. |
| `POLICY_DENIED` | Runtime or platform policy rejected the invocation. |
| `ABI_VIOLATION` | The workload or peer violated the container ABI, including invalid protocol data or an incompatible runtime contract. |
| `OUTCOME_UNKNOWN` | The runtime cannot determine a valid terminal outcome, including an unexpected exception, stream closure, or serialization failure. |
| `CREDENTIAL_SCAN_LIMIT` | Credential scanning reached its safety limit before the runtime could complete the required scan. |

The wrapper schema does not redefine protobuf transport fields. The
`ContainerService` ABI remains the protocol contract for transporting runtime
events; any transport mapping must preserve the distinction between deliberate
failure and no valid outcome.

The Execute server can send a version 1 `LogBatch` notification immediately
before its terminal outcome. The [protobuf](proto/syntara_plugin/runtime/protocol/container.proto)
defines its event fields, allowed values, bounds, and overflow behavior. The
batch is one-way, separate from credential responses, and requires no
acknowledgment. Peers that reject unknown server messages must be updated before
workloads use logging; Health does not distinguish this addition under the
existing ABI identifier. Host consumers receive validated batches as
`ContainerLogBatch` events from `ContainerClient.stream()`, alongside progress
and the terminal result. See [ADR 0013](../.sdlc/adrs/0013-toolbox-logging.md).
