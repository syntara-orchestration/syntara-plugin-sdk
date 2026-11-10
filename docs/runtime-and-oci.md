# Runtime and OCI model

This page describes the workload ABI and OCI packaging details. For the
multi-image release lifecycle, use the [RuntimeBindings guide](runtime-bindings.md).
Ordinary platform-runner actions need only the plugin artifact.

## Release boundaries

```text
Plugin source + step manifests + schemas  --build-->  plugin artifact (P)
RuntimeBinding source + images             --build-->  binding artifact (B) + image(s) (I)
P + B + I                                  --publish--> registry digests
P                                          --catalog--> plugin catalog index
B                                          --index----> per-plugin RuntimeBinding index
```

The plugin artifact is the declarative, inspectable release. A workload image
is a separate OCI image containing author-owned executable code. In the
multi-image flow, the plugin descriptor carries a logical workload name and
the RuntimeBinding maps that name to one or more image options pinned by
`repository@sha256:...`. The Execution Plane must use the exact image digest
resolved by the platform; it must not accept a workflow-supplied image URI.
Legacy single-image descriptors may still carry their digest directly. New
multi-image releases should use RuntimeBindings so an image security update
does not require republishing P.

For `platform-runner` steps, the plugin declares an implementation from an
already registered plugin. The AO Plugin Registry resolves its image and entry
point; Syntara controls endpoint policy and connection delivery. Only the
built-in `syntara.http.request@<version>` implementation accepts an Action's
bounded HTTP operation contract.

## Container ABI

The in-container plugin protocol is defined by
[`container.proto`](../contracts/proto/syntara_plugin/runtime/protocol/container.proto).
The corresponding runtime work-item JSON shape is in
[`runtime.schema.json`](../contracts/schemas/v1alpha1/runtime.schema.json).
The Python helper implementation is in
[`container.py`](../sdks/python/packages/contracts/src/syntara_plugin/runtime/container.py).

The Application Binary Interface (ABI) is the boundary between an Execution
Plane launcher and a workload. The `syntara.container/v1alpha1` bidirectional
invocation stream carries non-secret inputs and business events. For a
credential read, the workload sends the declared CredentialRecipe ID as a
logical name. The Execution Plane resolves that name within the current
attempt and returns a bounded JSON object in a separate sensitive response;
credential IDs and references are never sent to the workload. `ContainerClient`
accepts a `credential_handler` per execution or stream, so the host can bind
each resolver to its own attempt. The channel must authenticate both peers
and protect confidentiality (mTLS in production).

Each credential handler receives a `CredentialRequestContext` with the
declared name, invocation ID, cancellation signal, and monotonic deadline.
Use `remaining_seconds` to bound network and operation timeouts, and observe
`cancellation` while waiting on work. `ContainerClient` invokes the handler on
the thread consuming the Execute stream and does not own a resolver executor
or queue. The Execution Plane or local runner owns resolver concurrency and
queue limits. Python cannot stop handler code that ignores its cancellation
signal or deadline. The synchronous runtime separately admits at most 16
active invocation worker pairs across the process. Call the synchronous
`ContainerRuntimeService.shutdown()` during server teardown; it signals
cancellation and waits up to one second for owned workers. Arbitrary blocked
Python step handlers cannot be preempted.

The async `AsyncContainerRuntimeService` used by the image runtime owns a
per-service pool of eight daemon step workers by default. A running handler
keeps its service capacity slot until it returns, even after cancellation or a
timeout. Shutdown signals cancellation and stops new work, but cannot preempt
a blocked handler. The daemon workers do not prevent the Python process from
exiting.

A host may close its send stream while awaiting the terminal event. The step
continues, but credential reads cannot complete after that closure. An invalid
follow-up message processed before the terminal event is committed cancels the
invocation and produces a cancellation failure. Later messages may be ignored.
On shutdown, active invocations may finish during the server's grace period.

The generic `ContainerWorkloadRequest` carries its work item and policy
snapshot without credential leases or secret file paths. Its work item contains
opaque `credentialHandles` for platform use; these are not sent to the
workload. The platform owns how an attempt's callback is resolved.

Credential values must never appear in business output, status, errors, or
logs. Do not put values in inputs or runtime context. See
[ADR 0011](../.sdlc/adrs/0011-credential-callback.md) for callback lifetime and
failure semantics.

### Runtime adapters and workload images

The SDK does not publish a universal base image that contains an SDK runtime.
The shared boundary is the versioned container ABI; each runtime adapter
provides its own self-contained workload-image recipe. This avoids maintaining
a polyglot image and avoids requiring one base image per supported language.

The Python adapter is the
[`syntara_plugin.runtime`](../sdks/python/packages/contracts/src/syntara_plugin/runtime/)
module, shipped in the
[`syntara-plugin-contracts`](../sdks/python/packages/contracts/pyproject.toml)
distribution. A Python workload image installs that distribution, its plugin
package, and an image-local runtime config. The config has a language-neutral envelope
with all adapter-specific bootstrap details under `adapter`:

```json
{
  "abi": "syntara.container/v1alpha1",
  "adapter": {
    "bindingsFactory": "acme_plugin.runtime:create_bindings"
  }
}
```

The image sets `SYNTARA_RUNTIME_CONFIG` to that file's mounted path. The
Python adapter validates the ABI before reading its adapter settings. Other
adapters use the same envelope and put their own bootstrap fields under
`adapter`.

`bindingsFactory` is a Python import path, not a filesystem path and not part
of the language-neutral runtime config envelope. The adapter imports
`acme_plugin.runtime` and calls its `create_bindings` function. That function
takes no arguments and returns a non-empty `StepBindings` object. Write it as
a one-line delegation to `load_plugin_bindings`, which loads the packaged
`plugin.yaml` and binds every custom-runner step type through its installed
manifest (see [Typed Python step types](#typed-python-step-types)); the server rejects
implementation-derived descriptors when it starts:

```python
from syntara_plugin.runtime import StepBindings, load_plugin_bindings

def create_bindings() -> StepBindings:
    return load_plugin_bindings("acme_plugin")
```

`load_plugin_bindings` reads `<package>/manifests/plugin.yaml`, the target
manifests it declares, and any asset-backed schema documents from package
resources, in the formats the compiler accepts. Package the plugin workspace
inside the plugin wheel under `manifests/`, mirroring the source layout the
[Existing step types](../examples/existing-step) example uses. Each step type identity is
derived as `namespace.plugin.step` from the plugin metadata and each target's
`metadata.name`; identities are never hard-coded. Every declared target
manifest is validated against its kind contract; platform-runner,
`IntegrationType`, and `CredentialRecipe` targets are then skipped without
binding. Startup fails
with an actionable error when a manifest or declared asset is missing or
malformed, an entry point cannot be imported or does not name a `BaseStep`
subclass, the manifest kind does not match the implementation, a step
identity is declared twice, or the plugin declares no custom-runner step type
target.

The factory is plugin-specific, not step-type-specific. A workload image may
provide one or more installed bindings. At startup the platform supplies trusted
resolved execution data as a read-only JSON file, named by
`SYNTARA_RESOLVED_EXECUTION`. The file must conform to the versioned
`resolved-execution` contract:

```json
{
  "bindings": [{
    "stepRevisionId": "sha256:<64 lowercase hex digits>",
    "stepIdentity": "acme.plugin.echo",
    "runtimeKind": "custom-runner",
    "entryPoint": "acme_plugin.steps:EchoStep",
    "abi": "syntara.container/v1alpha1",
    "schemaDigests": {
      "input": "sha256:<64 lowercase hex digits>",
      "output": "sha256:<64 lowercase hex digits>"
    }
  }]
}
```

Replace the placeholders with the exact compiled revision and installed step type
identity, plus the compiled input/output schema digests. Custom-runner entries
must match that revision, identity, entry point, ABI, and schema digests in the
packaged registration manifest at
`/opt/syntara/registration-manifest.json` (override its path with
`SYNTARA_REGISTRATION_MANIFEST` for testing). For a platform-runner entry, the
platform supplies the referenced implementation's installed identity, entry
point, and ABI alongside the referencing step type's own revision; that revision need
not occur in the image manifest. The compiled descriptor retains the authored
implementation reference; the AO Plugin Registry resolves it, and this startup
path consumes the supplied installed-binding identity, entry point, ABI, and
schema digests. The SDK rejects startup when those digests differ from the
selected installed binding, preventing a referencing step type from validating
against a different contract than its implementation.
Each entry point must import a typed `BaseStep` class matching that exact
executable installed binding from the factory. Missing, malformed, duplicate, or
inconsistent data prevents server startup. This file is bootstrap data only: it
does not contain invocation IDs, workflow-run IDs, input, timeout, credentials,
or runtime context. Those per-invocation values are supplied in the gRPC
`Execute` request. Health advertises only the registered revisions, and Execute
dispatches only an exact revision match.

The factory must obtain authorized installed manifests and schemas; the image
registration manifest supplies neither schema provenance nor image attestation.
For platform-runner reuse, this adapter applies the matched installed binding's
schemas, so the platform must ensure they are appropriate for the referencing
step type. Declarative drivers and arbitrary container handlers are not supported as
resolved Python entry points. The final image owns its runtime-config file and
adapter package versions; platform registration and image selection remain
outside this SDK boundary.

The checked-in [Existing step types](../examples/existing-step) prototype exercises
this path for Action, Control, and Trigger. `make test-integration` builds its
plugin wheel, installs it with the Python contracts distribution, and invokes
all three manifest-declared entry points over the container ABI. The current
standalone Execution Plane still vendors the older `syntara.node.v1` protocol;
until that service implements the bidirectional `syntara.container/v1alpha1`
callback protocol, this repository proves
the SDK adapter path but does not claim end-to-end Execution Plane dispatch.
Automation Orchestrator's workflow selection and platform-owned runners remain
separate platform responsibilities.

The workload image starts the SDK runtime server as its entry point:

```sh
python -m syntara_plugin.runtime.server
```

The `syntara-plugin-runtime-server` console script runs the same `main()`. The
server reads its runtime config from `SYNTARA_RUNTIME_CONFIG` and its listener
settings from the `SYNTARA_RUNTIME_GRPC_*` variables. Authors never write or
start their own gRPC server: the SDK runtime server is the only server in the
image, and it owns input validation, execution dispatch, and the container
ABI. [Existing step types](../examples/existing-step) shows the base image recipe:
the `Containerfile` entry point and the mounted TLS material.
`syntara-plugin build --with-workload` adds the registration manifest the
server requires at startup (see
[Installed revision mapping](#installed-revision-mapping)).

> **Warning:** Do not start a separate gRPC server or write your own listener
> code: the SDK runtime server opens and configures the image's listener
> itself. Do not hard-code step type identities. A hand-written server
> bypasses the runtime's validation and its mutual TLS requirement, and a
> hard-coded identity drifts from the packaged manifest contract.

The server listens on `127.0.0.1:50051` by default. Override the address with
`SYNTARA_RUNTIME_GRPC_ADDRESS`; every TCP listener requires mutual
TLS, including loopback. Supply all three of `SYNTARA_RUNTIME_GRPC_TLS_CERT`,
`SYNTARA_RUNTIME_GRPC_TLS_KEY`, and `SYNTARA_RUNTIME_GRPC_TLS_CLIENT_CA` when
TLS is enabled. These values are mounted file paths, never PEM data or private
key contents.

`syntara-plugin run --mode image` is a supported client for such a runtime: it
connects through the SDK's `ContainerClient`, verifies the ABI and advertised
type identities over `Health`, executes one invocation, and prints the result.
See [Running locally](running-locally.md) for the full workflow, including the
client-side TLS options.

The **container ABI** is the versioned gRPC/protobuf contract that the
Execution Plane and a step type container use to exchange execution requests,
progress, results, and cancellation. It is not a CPU ABI. The current contract
is defined in
[`container.proto`](../contracts/proto/syntara_plugin/runtime/protocol/container.proto)
and identified as `syntara.container/v1alpha1`.

The `Result` protobuf uses the structured error-result fields defined by this
release. Bidirectional Execute replaces the earlier unary-request protocol
under the same `v1alpha1` identifier. Existing peers must be updated together;
Health checks the identifier but cannot distinguish the two protocol shapes.

Final workload images, rather than a reusable SDK base layer, are the OCI
artifacts published for deployment. They must record their immutable
`repository@sha256:...` digest and the container ABI version they implement.
Konflux ownership and signing apply to those final images; the SDK repository
does not create a long-lived shared runtime image.

## Typed Python step types

Custom-runner authors implement step types with the typed Python API in
[`syntara_plugin.runtime`](../sdks/python/packages/contracts/src/syntara_plugin/runtime/).
`BaseStep[TInput, TOutput]` is bound to Pydantic v2 models. Its typed
implementation operation is `execute(inputs, context) -> TOutput`: it receives
a validated `TInput` and an `ExecutionContext`, and returns the typed business
result. The public invocation operation is `runtime.invoke(step, input,
context)`. It accepts a trusted `StepBinding` and raw input, validates the
input, calls `step.execute(...)`, serializes the result, validates it against
the declared `spec.output` schema, and only then constructs a
`StandardOutputWrapper`. The container gRPC adapter calls this same function;
direct callers can invoke it without using the container transport.

The output must both satisfy the declared schema and serialize as canonical
JSON before a success wrapper can be constructed. Invalid output or
serialization raises `StepExecutionError` and produces no wrapper. A
step type-authored `StepFailure` is different: it produces a valid nonzero wrapper,
which the container adapter serializes as `ContainerResult`. A
step type-authored `ContainerInvocationError` also leaves no wrapper, but is
preserved as a terminal `ContainerFailure` with its code, message,
retryability, and outcome. The adapter
reports `ContainerFailure` for errors that leave no valid wrapper. In the
current class implementation, `BaseStep.execute()` delegates to the
author-defined `run()` hook, so examples below implement `run()`.

The three executable step type classes share the same lifecycle. A catalog
**step type** (for example, Create GitHub Issue) belongs to one **Category**;
runner ownership is selected independently, and execution consumes the final
contract: **step type -> Category -> Runner -> Execution**.

| Class | Category |
| --- | --- |
| `ActionStep` | `action` |
| `ControlStep` | `control` |
| `TriggerStep` | `trigger` |

`ExecutionContext` carries the invocation ID, an optional workflow run ID,
bounded non-secret runtime data, and a cooperative cancellation signal. It
intentionally exposes no protobuf, gRPC, container, worker, or Execution Plane
types, and no raw credential values.

### Credential access

For an Action, Control, or Trigger, the installed `integrationType` and its linked
`credentialTypes` define which recipe identifiers the step may request. The host supplies a provider for
approved bindings; a supplied name never grants permission by itself.
A custom `CredentialProvider` must return a fresh `ScopedCredentialProvider`
from `restrict(names, on_read)`. The view retains only the named bindings,
reports every read through `on_read`, and revokes access on `close`. Providers
that cannot supply this restricted view fail safely. During `run`, call `context.credential("example.tracker.api-token").read()` to
obtain a detached credential document. The handle and provider do not expose
values in their representations and cannot be serialized. A handle expires as
soon as the invocation returns, including on failure. Missing, undeclared,
unauthorized, unavailable, and expired bindings raise `CredentialAccessError`
with a safe `reason` and no names, paths, or provider details.

The returned document contains sensitive values. Use them only for the intended
operation; never put them in logs, inputs, results, or error messages. SDK-generated
errors and diagnostics do not include values read through the handle. The SDK
rejects wrapper fields containing those values and suppresses business traceback
details after a read when they contain credential values, and degrades a
terminal-failure message that carries one. Safety scans are bounded:
credential documents are limited to 1 MiB, 10,000 nodes, and 256 tracked
fingerprints; output scans are limited to 10,000 nodes and 4,000,000 scan-work
units. If a scan cannot finish within those bounds, the SDK reports
`CREDENTIAL_SCAN_LIMIT`, distinct from a detected leak. Reduce credential document
size or output complexity to stay within the limits. The SDK cannot redact logs emitted by step code, so
step authors must not log returned values. The SDK does not resolve production
credential IDs or implement a vault. A Python step is not sandboxed by this
accessor; the host must authorize each invocation and enforce process isolation.

`CredentialRecipe` and `IntegrationType` Pydantic views validate and serialize
the existing v1alpha1 authoring documents; the canonical JSON Schemas remain
the source of truth. Authenticate and test extensions are described only in the
[versioned proposal](../.sdlc/proposals/credential-authenticate-test-v1.md).

### Contract drift validation

A step type declares its data shape twice: the JSON Schemas in its manifest, which
the platform renders and validates against, and the Pydantic models on its
`BaseStep` subclass, which the code enforces. The
[`contract_validation`](../sdks/python/packages/contracts/src/syntara_plugin/runtime/contract_validation.py)
module compares the two before anything ships:

```python
from syntara_plugin.runtime import validate_plugin_bindings

result = compile_workspace(BuildRequest(root, image_bindings=image_bindings))
validate_plugin_bindings(result.descriptor, bindings, strict=True)
```

Input is checked so that every manifest-valid value builds the model, and
output so that every model-emittable value satisfies the manifest. Definite
incompatibilities raise `SchemaDriftError` with JSON-pointer paths into the
declared schema; `strict=True`, intended for CI, also rejects warnings. The
declared schema is canonical — the Python side is checked against it, never
the reverse. Call it once per binding set (in CI or when bindings are
registered), not per invocation.

### Minimal direct execution

Direct SDK execution exercises the typed implementation only. It does not
demonstrate platform registry integration or end-to-end Execution Plane
execution.

```python
from pydantic import BaseModel
from syntara_plugin.runtime import (
    ActionStep,
    ExecutionContext,
    TriggerStep,
    ControlStep,
)

class EchoInput(BaseModel):
    message: str

class EchoOutput(BaseModel):
    message: str
    invocation_id: str

class ActionEcho(ActionStep[EchoInput, EchoOutput]):
    def run(self, inputs, context):
        return EchoOutput(message=inputs.message, invocation_id=context.invocation_id)

class ControlEcho(ControlStep[EchoInput, EchoOutput]):
    def run(self, inputs, context):
        return EchoOutput(message=inputs.message, invocation_id=context.invocation_id)

class TriggerEcho(TriggerStep[EchoInput, EchoOutput]):
    def run(self, inputs, context):
        return EchoOutput(message=inputs.message, invocation_id=context.invocation_id)

context = ExecutionContext(invocation_id="demo-1")
for step in (ActionEcho(), ControlEcho(), TriggerEcho()):
    print(step.execute(EchoInput(message="hello"), context))
```

### Registration and descriptors

`StepDescriptor` associates a declared step type identity with its concrete step type
kind and `StepCategory`. An implementation-derived descriptor uses the step type's
Pydantic schemas for authoring and schema-drift validation. An installed
custom-runner descriptor instead derives both its input and output schemas
from the installed manifest's resolved `spec.input` and `spec.output` for
Actions, Controls, and Triggers.
`StepDescriptor.for_step()` creates implementation descriptors, and
`StepBindings` retains each descriptor-and-implementation pair:

```python
from syntara_plugin.runtime import StepDescriptor, StepBindings

bindings = StepBindings()
step = ControlEcho()
bindings.bind(StepDescriptor.for_step("acme.plugin.echo", step), step)
binding = bindings.binding("acme.plugin.echo")
resolved = binding.step
```

Binding rejects malformed or duplicate identities, concrete-kind or category
mismatches between the descriptor and the Python class, and attempts to bind a platform-managed
driver (such as `http.v1`) as a Python implementation. `StepExecutor` repeats
the implementation and category checks before raw-input validation, so a
directly constructed `StepBinding` cannot bypass them. `StepBindings` is a local
SDK lookup table, not the platform registration database. Runtime startup maps
the platform's resolved revisions to these existing installed bindings.
Pydantic models are implementation types, not replacement manifests. For an
installed custom-runner step type, the canonical installed manifest schema is the
execution contract; comparing implementation schemas with source manifests is
owned by the schema-drift validation story. A Trigger's executor input is its
registered `spec.input` schema; `spec.configuration` remains separate driver
configuration and is not reinterpreted as invocation input.

A packaged plugin does not need to run this sequence by hand:
`load_plugin_bindings("acme_plugin")` reads the packaged `plugin.yaml`, its
target manifests, and asset-backed schema documents from package resources,
and binds every custom-runner step type with the identity
`namespace.plugin.step`. The underlying calls it wraps, for one target:

For an installed custom-runner step type, first resolve its manifest with
`InstalledStepManifest.from_manifest()`. Inline and asset-backed `spec.input`
and `spec.output` document sources are supported, and the manifest and resolved
schemas are stored recursively as immutable JSON documents. Pass that object to
`StepDescriptor.for_installed_step()`; both the descriptor's input and output
schemas are manifest-sourced and cannot be replaced by a caller-supplied schema.
The exposed documents are immutable mappings/sequences; call the runtime
`to_json_document()` helper when a mutable plain-JSON export is required for
serialization or transport.
The business result is validated against the manifest output schema before a
successful outcome is declared, so the echoed digest names the schema actually
applied. A `StepFailure` is a deliberate nonzero outcome and does not validate a
business result against `spec.output`. The schema must be self-contained; local
JSON Schema references are resolved when
the descriptor is created, so a dangling reference fails installation instead of
breaking every invocation. The
resulting `StepBinding` can be passed to `runtime.invoke`, which validates the
raw JSON mapping against that trusted schema, constructs the registered
Pydantic model in strict mode, invokes the typed implementation once, validates
and serializes its result, and then constructs the wrapper:

```python
from syntara_plugin.runtime import (
    ExecutionContext,
    InstalledStepManifest,
    StepBindings,
    StepDescriptor,
    invoke,
)

bindings = StepBindings()
# ... obtain the step, its installed manifest, and its assets from the
# packaged plugin, as `load_plugin_bindings` does. The identity passed
# below is that derived namespace.plugin.step value, never hand-written.
step = EchoStep()
installed_step = InstalledStepManifest.from_manifest(
    "acme.plugin.echo", installed_manifest, installed_assets
)
descriptor = StepDescriptor.for_installed_step(step, installed_step)
bindings.bind(descriptor, step)
result = invoke(
    bindings.binding("acme.plugin.echo"),
    {"message": "hello"},
    ExecutionContext(invocation_id="demo-1"),
)
```

`from_manifest()` is the installation/bootstrap handoff for an installed
custom-runner step type. The component loading the installed plugin metadata must supply an
authorized document. For the complete factory that supplies the installed
manifest and its assets, see `load_plugin_bindings` and the
[Existing step types](../examples/existing-step) example. The SDK runtime cannot
independently verify remote or cryptographic provenance, but it records
opaque provenance for the exact factory-created manifest and descriptor
objects. `StepBindings` accepts
implementation-derived descriptors for authoring validation, while
`runtime.invoke` and container dispatch reject them for execution. All three
supported custom-runner step type kinds execute only through the factory-created
identity and canonical installed-schema binding.

The runtime owns the `StandardOutputWrapper` and `ErrorResult` result types.
Invalid input produces a nonzero error result when it satisfies the effective
error schema; the implementation hook is not called. JSON Schema
`default` values are annotations, so the registered Pydantic model supplies the
actual default. Enums must match exactly, and unknown fields are rejected only
when the manifest schema forbids them. Pydantic runs in strict mode after JSON
Schema validation so coercion cannot accept a value that the published step type
contract rejects.

Control and Trigger execution uses the installed `spec.input` contract. The
registered Pydantic model still supplies the typed object, defaults, and model
configuration; in particular, a Trigger's executor input is its registered
input contract rather than driver configuration. Every supported custom-runner
step type passes through the same invocation boundary, which validates against
the installed schema, constructs the registered Pydantic model, and invokes
the typed implementation only after validation completes successfully.

The container adapter maps a valid `StandardOutputWrapper` into the ABI's
`ContainerResult`: business output and wrapper status/error metadata are
serialized in the result fields, alongside the digest of the applied
declared `spec.output` schema. A nonzero status is still a valid result and
carries the complete structured error JSON and the canonical digest of the
effective error schema. The SDK base requires a category, code, message, and
retryability value. When `spec.error` is declared, its schema may add
step type-specific fields and the runtime validates errors against both schemas.
When `spec.error` is absent, the SDK uses a closed fallback schema that accepts
only the base fields; custom fields require a declared `spec.error`.
An error that fails either schema produces no valid wrapper, including an
input-validation error when a declared schema requires additional fields.
`ContainerFailure` is reserved for
cases where no valid wrapper/outcome exists, such as invalid output,
serialization failure, or a runtime/protocol failure. Direct and container
execution share input validation, typed execution, and output validation;
only the container path adds ABI serialization and transport limits. The JSON
runtime contract names this outcome `ContainerFailure`; the gRPC protocol
carries it in a `Failure` event.
For a nonzero `ContainerResult`, the error and its schema digest are required.
A missing or unknown category is a protocol error. Inline output and object references are
mutually exclusive; either may carry a deliberate failure's optional business
result.

Platform-runner step types remain declarative and are not bound to a Python
implementation by this runtime path.

Direct execution is transport-independent: importing `syntara_plugin.runtime`
and using `invoke`, `StepExecutor`, the typed bindings, and the
`StandardOutputWrapper` never pulls in gRPC or the generated protobuf. The
container transport symbols
(`ContainerClient`, `ContainerRuntimeService`, the adapter factories, and
`ResultObjectStore`) are exported lazily and resolve those dependencies only on
first use, so steps run and test where the transport libraries are absent.

### Container adapter

`step_registry_from_bindings(bindings)` builds a local container `StepRegistry`
keyed by binding identity. The server startup path instead keys the handlers by
the resolved revisions. The adapter converts each
`ContainerInvocation` into an `ExecutionContext` (sharing the same cooperative
cancellation event), calls `runtime.invoke`, and maps the returned wrapper to a
`ContainerResult`. Valid input-validation errors and deliberate `StepFailure`
values raised from `run()` produce nonzero-status results; valid output
produces a zero-status result. A `ContainerInvocationError` raised from
`run()` is a deliberate terminal failure: it produces no wrapper and becomes
a terminal `ContainerFailure` that keeps its code, message, retryability,
outcome, and provider code. Any other exception raised from `run()` produces no wrapper and
becomes a terminal `ContainerFailure` with the code `OUTCOME_UNKNOWN`.
Invalid output
and serialization errors also produce no wrapper and become `ContainerFailure`
outcomes. Each `ContainerResult` carries
the canonical digest of the declared output schema; successful business results
are validated against that schema. An `outputSchemaDigest` in runtime context
is accepted only when it matches that schema; a mismatch is an ABI violation.

The adapter accepts a host-supplied `ResultObjectStore`. `runtime.invoke`
performs transport-independent output-schema and canonical-JSON validation;
the adapter applies the container ABI's 1 MiB inline limit and externalizes
larger valid results through the object store. Authors never import
`ContainerInvocation` to write or test a step type.

On the client, `ContainerClient.execute_wrapped(invocation)` returns the SDK's
`StandardOutputWrapper`, including the original status message and error metadata.
`execute()` and `stream()` continue to expose the transport result and its output
schema digest. `standard_output_from_container_result(result)` also converts an
already received result. When a result carries an `object_reference`, pass
`resolve_object_reference=` to either wrapper API: the host callback receives the
opaque reference and returns the retrieved business result as a JSON object. A
missing resolver, invalid resolved object, or resolver exception raises a safe
error without exposing resolver details; inline and resolved values are checked
against the caller-supplied trusted output schema before a wrapper is built. A
terminal transport `Failure` still raises `ContainerRemoteError` and is never
converted to a deliberate step type failure.

For a step type-specific error contract, pass the trusted `error_schema=` to either
wrapper API. The client verifies its digest and validates the complete error
before reconstruction. Without that argument it uses the closed, base-fields-only
fallback schema, so custom fields require a declared `spec.error`. Authors can
supply step type-specific fields with `StepFailure(...,
error_result=ErrorResult(category="dependency", code="REMOTE_REJECTED",
message="Rejected", retryable=True, requestId="request-1"))`.
They can also subclass `ErrorResult` to declare step type-specific fields.
Preserve an upstream `requestId` in declared step type-specific error fields when
it helps troubleshoot the provider call. For correlation with platform
execution records, use `ExecutionContext.invocation_id` in step type logs instead
of generating a second execution identifier.

### Installed revision mapping

An immutable installed step type revision maps a declared step type to its Python
implementation:

- The step type identity comes from the installed descriptor. It is never inferred
  from a source filename.
- Authors do not hard-code build-generated revision IDs.
- The compiled contract carries the `module:Class` entry point and required
  ABI. The SDK generates a registration manifest for each logical workload
  and includes it in the image before exporting the local image archive.
- Revision IDs identify compiled contracts, not image bytes. In the
  multi-image flow, the RuntimeBinding artifact separately maps each workload
  option to its immutable image digest; the plugin artifact does not change
  when only that image mapping changes.

The registration manifest is canonical JSON with
`apiVersion: syntara.registration/v1alpha1`, `kind: RegistrationManifest`,
the plugin revision ID, SDK version, workload ID, and a `steps` object keyed by
step type revision ID. Each entry records its installed step type identity, `entryPoint`,
ABI, and declared schema digests. The SDK validates this mapping against the
`registration-manifest` contract and the compiled descriptor before adding the
image layer.
`registration_manifest` and `validate_registration_manifest` in
`syntara_plugin.contracts` are available to external image producers; an image
binding alone does not prove that its image contains a matching manifest.

See the [plugin language reference](plugin-language.md) for the manifest
fields.

## Automation Orchestrator integration contract

The SDK ships the runtime pieces an installation needs; the platform around
them owns verification and lifecycle. This section is a handoff map: each
item is a duty the platform already performs, with a pointer to where the
needed data already lives. The SDK does not implement them.

- **Digest verification**: pull the plugin artifact by digest (the digest is
  the OCI address), then read workload image digests from the RuntimeBinding
  artifact referenced by the plugin's RuntimeBinding index. When signing is
  enabled, verify the index and referenced artifacts against the configured
  trust policy. Deploy
  images by digest. The registration manifest and resolved execution data carry the
  step revision IDs, entry points, ABIs, and schema digests; the runtime
  server cross-checks them against the installed bindings at startup and
  refuses to start on any mismatch. Each step revision ID digests the full
  compiled target, including its metadata, so metadata integrity is covered
  by the revision check.
- **Staging and import**: install the plugin wheel and registration manifest
  into the workload image during the local build. The Containerfile and SDK
  build settings are shown in the [RuntimeBindings guide](runtime-bindings.md).
- **Trust**: `InstalledStepManifest.from_manifest` is the authorized-document
  boundary. The SDK records opaque provenance for factory-created objects but
  cannot establish remote or cryptographic provenance; the platform supplies
  verified documents and resolved execution data.
- **Dependency compatibility**: the registration manifest records the
  `sdkVersion`, and the workload image owns its contracts distribution and
  adapter versions. The platform checks them against the compiled
  descriptor's declared ABI before dispatch. The RuntimeBinding also records
  the contract-bundle digest used to resolve the image selection.
- **Import isolation**: one workload image holds one plugin today. Running
  several plugins in one Python interpreter is a platform concern; the SDK
  gives each plugin its own package and manifest namespace but no import
  sandbox.
- **Caching**: resolution is keyed by immutable step revision ID, and the
  image-local `StepBindings` lookup is keyed by step identity. Both are safe
  to cache for the lifetime of the installed revision they were verified
  against.
- **Retirement**: deregistering revisions and garbage-collecting unreferenced
  artifacts and images are platform registry operations; the SDK never
  bypasses the trusted chain.

## Registry and catalog model

The artifact and workload repositories may be mirrored for disconnected or
air-gapped installations. During installation, the Control Plane records the
plugin's immutable artifact identity and the deployment-resolved pull
reference. The registry administrator controls mirroring; the plugin author
does not hard-code a deployment mirror in the manifest.

Catalogs are discovery indexes, not the source of truth for an artifact.
`syntara-plugin publish` creates or updates the configured index by default;
set `publish.updateCatalog: false` only for a staged release. Use
`syntara-plugin catalog update` for release administration. If its mutable tag is lost, use
`syntara-plugin catalog recover` with Syntara's last accepted immutable index
digest; this continues the trusted chain instead of replaying generation one.
See the [user guide](user-guide.md) for the operational commands and [build
settings reference](build-settings.md) for catalog defaults.
