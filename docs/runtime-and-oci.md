# Runtime and OCI model

This page distinguishes the pieces of a plugin release. It is useful when you
need a custom runner; ordinary platform-runner actions need only the plugin
artifact.

## Release boundaries

```text
plugin.yaml + target manifests + schemas + Markdown
                  |
                  | syntara-plugin build
                  v
          plugin metadata OCI artifact
                  |
                  | syntara-plugin publish
                  v
       registry artifact repository / catalog index

custom-runner source + Containerfile
                  |
                  | syntara-plugin build --with-workload --publish
                  v
        immutable workload image digest
                  |
                  | bound into metadata artifact
                  v
       Execution Plane pulls the approved digest
```

The artifact is the declarative, inspectable plugin release. A workload image
is a separate OCI image containing author-owned executable code. The compiler
records a custom-runner action's logical image ID and binds it to an immutable
`repository@sha256:...` reference. The Execution Plane uses that resolved
reference, never a mutable tag.

For `platform-runner` steps, the plugin declares an implementation from an
already registered plugin. The AO Plugin Registry resolves its image and entry
point; Syntara controls endpoint policy and connection delivery. An Action may
also declare a bounded HTTP operation contract.

## Container ABI

The in-container plugin protocol is defined by
[`container.proto`](../contracts/proto/syntara_plugin/runtime/protocol/container.proto).
The corresponding runtime work-item JSON shape is in
[`runtime.schema.json`](../contracts/schemas/v1alpha1/runtime.schema.json).
The Python helper implementation is in
[`container.py`](../sdks/python/packages/contracts/src/syntara_plugin/runtime/container.py).

The Application Binary Interface (ABI) is the boundary between an Execution Plane launcher and an author
workload. It carries invocation and workflow-run identities, step inputs,
output/error contracts, and approved file paths. It does not carry raw
credentials in workflow JSON, environment variables, or command-line arguments.

The ABI carries approved credential-file paths, not credential values. The
launcher is responsible for authorizing the paths and making the files
available to the workload; those host-side policies are outside this SDK.

Credential values must never appear in business output, wrapper status or
error fields, raised error messages, or logs. Pass approved file paths or
handles through the runtime boundary; do not copy secret values into inputs or
runtime context. The test harness can detect sentinel leaks, but it does not
redact values written by step code.

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
takes no arguments and returns a non-empty `StepBindings` object. Every bound
Step must use a descriptor created from its installed custom-runner manifest
(see [Typed Python steps](#typed-python-steps)); the server rejects
implementation-derived descriptors when it starts:

```python
from syntara_plugin.runtime import InstalledStepManifest, StepBindings, StepDescriptor

def create_bindings() -> StepBindings:
    bindings = StepBindings()
    step = EchoStep()
    installed_step = InstalledStepManifest.from_manifest(
        "acme.echo.v1", installed_manifest, installed_assets
    )
    bindings.bind(StepDescriptor.for_installed_step(step, installed_step), step)
    return bindings
```

The factory is plugin-specific, not Step-type-specific. A workload image may
provide one or more installed bindings. At startup the platform supplies trusted
resolved execution data as a read-only JSON file, named by
`SYNTARA_RESOLVED_EXECUTION`. The file must conform to the versioned
`resolved-execution` contract:

```json
{
  "bindings": [{
    "stepRevisionId": "sha256:<64 lowercase hex digits>",
    "runtimeKind": "custom-runner",
    "entryPoint": "acme_plugin.steps:EchoStep",
    "abi": "syntara.container/v1alpha1"
  }]
}
```

Replace the placeholder with the exact compiled revision. Custom-runner entries
must match that revision, entry point and ABI in the packaged registration
manifest at `/opt/syntara/registration-manifest.json` (override its path with
`SYNTARA_REGISTRATION_MANIFEST` for testing). For a platform-runner entry, the
platform supplies the referenced implementation's entry point and ABI and the
referencing step's own revision; that revision need not occur in the image
manifest. The compiled descriptor retains the authored implementation reference;
the AO Plugin Registry resolves it, and this startup path consumes the supplied
entry point and ABI. Each entry point must import a typed `BaseStep` class matching exactly
one executable installed binding from the factory. Missing, ambiguous, malformed,
duplicate, or inconsistent data prevents server startup. Health advertises only
the registered revisions, and Execute dispatches only an exact revision match.

The factory must obtain authorized installed manifests and schemas; the image
registration manifest supplies neither schema provenance nor image attestation.
For platform-runner reuse, this adapter applies the matched installed binding's
schemas, so the platform must ensure they are appropriate for the referencing
step. Declarative drivers and arbitrary container handlers are not supported as
resolved Python entry points. The final image owns its runtime-config file and
adapter package versions; platform registration and image selection remain
outside this SDK boundary.

The checked-in [Existing Step](../examples/existing-step) prototype exercises
this path for Action, Control, and Trigger. `make test-integration` builds its
plugin wheel, installs it with the Python contracts distribution, and invokes
all three manifest-declared entry points over the container ABI. The current
standalone Execution Plane still vendors the older `syntara.node.v1` protocol;
until that service adopts `syntara.container/v1alpha1`, this repository proves
the SDK adapter path but does not claim end-to-end Execution Plane dispatch.
Automation Orchestrator's workflow selection and platform-owned runners remain
separate platform responsibilities.

The server listens on `127.0.0.1:50051` by default. Override the address with
`SYNTARA_RUNTIME_GRPC_ADDRESS`; every non-loopback listener requires mutual
TLS. Supply all three of `SYNTARA_RUNTIME_GRPC_TLS_CERT`,
`SYNTARA_RUNTIME_GRPC_TLS_KEY`, and `SYNTARA_RUNTIME_GRPC_TLS_CLIENT_CA` when
TLS is enabled. These values are mounted file paths, never PEM data or private
key contents.

`syntara-plugin run --mode image` is a supported client for such a runtime: it
connects through the SDK's `ContainerClient`, verifies the ABI and advertised
step identities over `Health`, executes one invocation, and prints the result.
See [Running locally](running-locally.md) for the full workflow, including the
client-side TLS options.

The **container ABI** is the versioned gRPC/protobuf contract that the
Execution Plane and a step container use to exchange execution requests,
progress, results, and cancellation. It is not a CPU ABI. The current contract
is defined in
[`container.proto`](../contracts/proto/syntara_plugin/runtime/protocol/container.proto)
and identified as `syntara.container/v1alpha1`.

The `Result` protobuf uses the structured error-result fields defined by this
release. The ABI remains `v1alpha1` because this pre-1.0 alpha contract does
not promise wire compatibility between prereleases. Existing alpha images
must be rebuilt or upgraded with this SDK before they are used; Health checks
the ABI identifier exactly but does not distinguish incompatible alpha builds.

Final workload images, rather than a reusable SDK base layer, are the OCI
artifacts published for deployment. They must record their immutable
`repository@sha256:...` digest and the container ABI version they implement.
Konflux ownership and signing apply to those final images; the SDK repository
does not create a long-lived shared runtime image.

## Typed Python steps

Custom-runner authors implement steps with the typed Python API in
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
step-authored `StepFailure` is different: it produces a valid nonzero wrapper,
which the container adapter serializes as `ContainerResult`. The adapter
reports `ContainerFailure` for errors that leave no valid wrapper. In the
current class implementation, `BaseStep.execute()` delegates to the
author-defined `run()` hook, so examples below implement `run()`.

The three executable Step classes share the same lifecycle. A catalog
**Step type** (for example, Create GitHub Issue) belongs to one **Category**;
runner ownership is selected independently, and execution consumes the final
contract: **Step type -> Category -> Runner -> Execution**.

| Class | Category |
| --- | --- |
| `ActionStep` | `action` |
| `ControlStep` | `control` |
| `TriggerStep` | `trigger` |

`ExecutionContext` carries the invocation ID, an optional workflow run ID,
bounded non-secret runtime data, and a cooperative cancellation signal. It
intentionally exposes no protobuf, gRPC, container, worker, or Execution Plane
types, and no raw credential values; typed credential access is tracked
separately.

### Contract drift validation

A step declares its data shape twice: the JSON Schemas in its manifest, which
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

`StepDescriptor` associates a declared step identity with its concrete Step
kind and `StepCategory`. An implementation-derived descriptor uses the step's
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
installed custom-runner Step, the canonical installed manifest schema is the
execution contract; comparing implementation schemas with source manifests is
owned by the schema-drift validation story. A Trigger's executor input is its
registered `spec.input` schema; `spec.configuration` remains separate driver
configuration and is not reinterpreted as invocation input.

For an installed custom-runner Step, first resolve its manifest with
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
custom-runner Step. The component loading the installed plugin metadata must supply an
authorized document. The SDK runtime cannot independently verify remote or
cryptographic provenance, but it records opaque provenance for the exact
factory-created manifest and descriptor objects. `StepBindings` accepts
implementation-derived descriptors for authoring validation, while
`runtime.invoke` and container dispatch reject them for execution. All three
supported custom-runner Step kinds execute only through the factory-created
identity and canonical installed-schema binding.

The runtime owns the `StandardOutputWrapper` and `ErrorResult` result types.
Invalid input produces a nonzero error result when it satisfies the effective
error schema; the implementation hook is not called. JSON Schema
`default` values are annotations, so the registered Pydantic model supplies the
actual default. Enums must match exactly, and unknown fields are rejected only
when the manifest schema forbids them. Pydantic runs in strict mode after JSON
Schema validation so coercion cannot accept a value that the published Step
contract rejects.

Control and Trigger execution uses the installed `spec.input` contract. The
registered Pydantic model still supplies the typed object, defaults, and model
configuration; in particular, a Trigger's executor input is its registered
input contract rather than driver configuration. Every supported custom-runner
Step type passes through the same invocation boundary, which validates against
the installed schema, constructs the registered Pydantic model, and invokes
the typed implementation only after validation completes successfully.

The container adapter maps a valid `StandardOutputWrapper` into the ABI's
`ContainerResult`: business output and wrapper status/error metadata are
serialized in the result fields, alongside the digest of the applied
declared `spec.output` schema. A nonzero status is still a valid result and
carries the complete structured error JSON and the canonical digest of the
effective error schema. The SDK base requires a category, code, message, and
retryability value. When `spec.error` is declared, its schema may add
step-specific fields and the runtime validates errors against both schemas.
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

Platform-runner Steps remain declarative and are not bound to a Python
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
`ContainerResult`. Valid input-validation errors and deliberate `StepFailure` values produce
nonzero-status results; valid output produces a zero-status result. Invalid
output, serialization errors, and unexpected step exceptions produce no
wrapper and become `ContainerFailure` outcomes. Each `ContainerResult` carries
the canonical digest of the declared output schema; successful business results
are validated against that schema. An `outputSchemaDigest` in runtime context
is accepted only when it matches that schema; a mismatch is an ABI violation.

The adapter accepts a host-supplied `ResultObjectStore`. `runtime.invoke`
performs transport-independent output-schema and canonical-JSON validation;
the adapter applies the container ABI's 1 MiB inline limit and externalizes
larger valid results through the object store. Authors never import
`ContainerInvocation` to write or test a step.

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
converted to a deliberate step failure.

For a step-specific error contract, pass the trusted `error_schema=` to either
wrapper API. The client verifies its digest and validates the complete error
before reconstruction. Without that argument it uses the closed, base-fields-only
fallback schema, so custom fields require a declared `spec.error`. Authors can
supply step-specific fields with `StepFailure(...,
error_result=ErrorResult(category="dependency", code="REMOTE_REJECTED",
message="Rejected", retryable=True, requestId="request-1"))`.
They can also subclass `ErrorResult` to declare typed step-specific fields.
Preserve an upstream `requestId` in declared step-specific error fields when
it helps troubleshoot the provider call. For correlation with platform
execution records, use `ExecutionContext.invocation_id` in step logs instead
of generating a second execution identifier.

### Installed revision mapping

An immutable installed step revision maps a declared step to its Python
implementation:

- The step identity comes from the installed descriptor. It is never inferred
  from a source filename.
- Authors do not hard-code build-generated revision IDs.
- The compiled contract carries the `module:Class` entry point and required
  ABI. `--with-workload` embeds the compiler-generated registration manifest
  at `/opt/syntara/registration-manifest.json` in the image before pushing it.
- Revision IDs identify compiled contracts, not image bytes. The workload image
  is separately bound by its immutable digest in the descriptor.

The registration manifest is canonical JSON with
`apiVersion: syntara.registration/v1alpha1`, `kind: RegistrationManifest`,
the plugin revision ID, SDK version, workload ID, and a `steps` object keyed by
step revision ID. Each entry records its `entryPoint` and ABI. The SDK validates
this mapping against the `registration-manifest` contract and the compiled
descriptor before adding the image layer.
`registration_manifest` and `validate_registration_manifest` in
`syntara_plugin.contracts` are available to external image producers; an image
binding alone does not prove that its image contains a matching manifest.

See the [plugin language reference](plugin-language.md) for the manifest
fields.

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
