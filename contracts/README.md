# Syntara Plugin Contracts

This directory is the language-neutral source of truth for the Syntara Plugin SDK contracts.

- `schemas/` contains versioned JSON Schema documents shared by every SDK.
- `fixtures/` contains valid and invalid conformance examples shared by every SDK.
- `proto/` contains the language-neutral `ContainerService` ABI source shared
  by plugin workload runtimes and the Execution Plane.

The `http-operation.schema.json` contract defines the deliberately small,
data-only operation format used by the `http.v1` platform runner: a method,
an origin-less relative path template, and bounded request/response maps whose
values are JSON Pointers or JSON literals. It does not define HTTP transport,
credentials, a dynamic origin, headers, or executable expressions.
Source path templates cannot contain percent escapes. The shared runtime mapper
encodes a checked input value as exactly one path segment, preventing a value
from becoming a separator, query, fragment, or dot segment.

`http-runner-context.schema.json` defines the runtime-only form after Syntara
has resolved an installed action: `driver: http.v1`, the inline operation, and
the output-schema digest. It deliberately has no endpoint, credential, header,
image reference, asset reference, or executable expression. The generic work
item retains this as an optional bounded non-secret `runtimeContext` object so
other approved runners can later define their own constrained context contract.

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

For this SDK revision, the container adapter's result mapping is provisional.
`Result` currently carries the SDK output envelope explicitly: `output_json` or
`object_reference` holds the business result, while `status_code`,
`status_message`, `error_code`, `error_message`, and `retryable` hold the
wrapper's status and safe error metadata. `output_schema_digest` always binds
the step type's declared output schema to the business result, never the
envelope itself. A nonzero `StatusCode` is a valid completed outcome and must
carry a safe `error_code` and `error_message`; the `Failure` event is reserved
for protocol/runtime failures where no valid outcome exists. The added `Result`
fields are wire-compatible additions within `syntara.container/v1alpha1`; absent
fields decode as a zero `StatusCode`, and the `runtime_abi` string pins the
envelope version.
