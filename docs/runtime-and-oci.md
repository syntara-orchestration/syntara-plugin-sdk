# Runtime and OCI model

This page distinguishes the pieces of a plugin release. It is useful when you
need a custom workload; ordinary platform-runner actions need only the plugin
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

custom-workload source + Containerfile
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
records a custom-workload action's logical image ID and binds it to an immutable
`repository@sha256:...` reference. The Execution Plane uses that resolved
reference, never a mutable tag.

For `platform-runner` actions, Syntara owns the runner image, endpoint policy,
and connection delivery. The plugin declares only an approved driver and its
bounded operation contract.

## Container ABI

The in-container plugin protocol is defined by
[`container.proto`](../contracts/proto/syntara_plugin/runtime/protocol/container.proto).
The corresponding runtime work-item JSON shape is in
[`runtime.schema.json`](../contracts/schemas/v1alpha1/runtime.schema.json).
The Python helper implementation is in
[`container.py`](../sdks/python/packages/runtime/src/syntara_plugin/runtime/container.py).

The ABI is the boundary between an Execution Plane launcher and an author
workload. It carries invocation and workflow-run identities, step inputs,
output/error contracts, and approved file paths. It does not carry raw
credentials in workflow JSON, environment variables, or command-line arguments.

Credential-bearing custom workload execution is intentionally deferred in the
current local proof of concept. The ABI reserves a map of credential-file paths:
the eventual Execution Plane will redeem an opaque, short-lived credential
handle, write a secret into an isolated per-invocation in-memory mount, make
that mount read-only in the workload container, and send only the file path to
the plugin. The plugin never receives broker credentials or a Kubernetes API
identity.

## Typed Python steps

Custom-workload authors implement steps with the typed Python API in
[`syntara_plugin.runtime`](../sdks/python/packages/runtime/src/syntara_plugin/runtime/).
`BaseStep[TInput, TOutput]` is bound to Pydantic v2 models and exposes one
stable author-facing entrypoint, `execute(inputs, context)`. Subclasses
implement `run()`; the shared dispatch boundary validates raw input and returns
the common output envelope without changing that hook.

The four executable taxonomy classes share the same lifecycle:

| Class | Category |
| --- | --- |
| `ActionStep` | `action` |
| `TaskStep` | `task` |
| `WorkflowStep` | `workflow` |
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
[`contract_validation`](../sdks/python/packages/runtime/src/syntara_plugin/runtime/contract_validation.py)
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
demonstrate Automation Orchestrator registry integration or end-to-end
Execution Plane execution.

```python
from pydantic import BaseModel
from syntara_plugin.runtime import (
    ActionStep,
    ExecutionContext,
    TaskStep,
    TriggerStep,
    WorkflowStep,
)

class EchoInput(BaseModel):
    message: str

class EchoOutput(BaseModel):
    message: str
    invocation_id: str

class ActionEcho(ActionStep[EchoInput, EchoOutput]):
    def run(self, inputs, context):
        return EchoOutput(message=inputs.message, invocation_id=context.invocation_id)

class TaskEcho(TaskStep[EchoInput, EchoOutput]):
    def run(self, inputs, context):
        return EchoOutput(message=inputs.message, invocation_id=context.invocation_id)

class WorkflowEcho(WorkflowStep[EchoInput, EchoOutput]):
    def run(self, inputs, context):
        return EchoOutput(message=inputs.message, invocation_id=context.invocation_id)

class TriggerEcho(TriggerStep[EchoInput, EchoOutput]):
    def run(self, inputs, context):
        return EchoOutput(message=inputs.message, invocation_id=context.invocation_id)

context = ExecutionContext(invocation_id="demo-1")
for step in (ActionEcho(), TaskEcho(), WorkflowEcho(), TriggerEcho()):
    print(step.execute(EchoInput(message="hello"), context))
```

### Registration and descriptors

`StepDescriptor` associates a declared step identity with a taxonomy. An
implementation-derived descriptor uses the step's Pydantic schemas for every
taxonomy, including Action, so authoring and schema-drift validation can
compare those models with a source manifest. An installed Action descriptor
instead derives its input schema from the manifest's resolved `spec.input`; its
output schema remains implementation-derived. `StepDescriptor.for_step()`
creates implementation descriptors, and `StepBindings` retains each
descriptor-and-implementation pair:

```python
from syntara_plugin.runtime import StepDescriptor, StepBindings

bindings = StepBindings()
step = TaskEcho()
bindings.bind(StepDescriptor.for_step("acme.plugin.echo", step), step)
binding = bindings.binding("acme.plugin.echo")
resolved = binding.step
```

Binding rejects malformed or duplicate identities, taxonomy mismatches between
the descriptor and the Python class, and attempts to bind a platform-managed
driver (such as `http.v1`) as a Python implementation. `StepExecutor` repeats
the implementation and taxonomy checks before raw-input validation, so a
directly constructed `StepBinding` cannot bypass them. `StepBindings` is an
in-process lookup table, not the platform registration database; mapping an
installed step revision to a declared identity belongs to the registry-binding
story.
Pydantic models are implementation types, not replacement manifests: the
canonical schema bundle remains the source of truth for installed Actions, and
comparing implementation schemas with source manifests is owned by the
schema-drift validation story.

For an installed Action, first resolve its manifest with
`InstalledActionManifest.from_manifest()`. Inline and asset-backed `spec.input`
document sources are supported, and the manifest and resolved schema are stored
recursively as immutable JSON documents. Pass that object to
`StepDescriptor.for_installed_action()`; the descriptor's input schema is
manifest-sourced and cannot be replaced by a caller-supplied schema. The
schema must be self-contained; local JSON Schema references are resolved when
the descriptor is created, so a dangling reference fails installation instead of
breaking every invocation. The
resulting `StepBinding` can be passed to `StepExecutor`, which validates the
raw JSON mapping against that trusted schema, constructs the registered
Pydantic model in strict mode, and invokes the implementation hook once:

```python
from syntara_plugin.runtime import (
    ExecutionContext,
    InstalledActionManifest,
    StepBindings,
    StepDescriptor,
    StepExecutor,
)

bindings = StepBindings()
step = EchoStep()
installed_action = InstalledActionManifest.from_manifest(
    "acme.plugin.echo", installed_manifest, installed_assets
)
descriptor = StepDescriptor.for_installed_action(step, installed_action)
bindings.bind(descriptor, step)
executor = StepExecutor(bindings.binding("acme.plugin.echo"))
result = executor.execute(
    {"message": "hello"}, ExecutionContext(invocation_id="demo-1")
)
```

`from_manifest()` is the installation/bootstrap handoff for an installed
Action. The component loading the installed plugin metadata must supply an
authorized document. The SDK runtime cannot independently verify remote or
cryptographic provenance, but it records opaque provenance for the exact
factory-created manifest and descriptor objects. `StepBindings` accepts
implementation-derived Action descriptors for authoring validation, while
`StepExecutor` and container dispatch reject them for execution. At present,
Action is the only taxonomy with an installed-manifest execution contract, so
an Action can execute only through the factory-created identity and canonical
input-schema binding. The validation dispatch is intentionally taxonomy-aware:
future installed-contract support for Task, Workflow, or Trigger can be added
as those binding and manifest contracts become defined, without changing the
generic executor boundary. Those taxonomies currently retain their
implementation-derived contracts.

`StepExecutor` is the owner of the runtime-local `StandardOutputWrapper` and
`ErrorMessage` result types. Invalid input returns that wrapper with a nonzero
status and a safe `ErrorMessage`; the implementation hook is not called. JSON Schema
`default` values are annotations, so the registered Pydantic model supplies the
actual default. Enums must match exactly, and unknown fields are rejected only
when the manifest schema forbids them. Pydantic runs in strict mode after JSON
Schema validation so coercion cannot accept a value that the published Action
contract rejects.

Task, Workflow, and Trigger execution uses each step's registered Pydantic input
model directly. Those taxonomies retain normal Pydantic coercion, defaults, and
model configuration; in particular, a Trigger's executor input is its registered
model rather than manifest configuration. Every taxonomy passes through the same
`StepExecutor`, which invokes the hook only after its applicable validation
completes successfully.

The container adapter currently serializes that wrapper into its terminal result
for invalid input, using the canonical digest of the wrapper schema. Direct
Python execution always returns `StandardOutputWrapper`, and the same is planned for implementation of containerized execution. Actions are the only
taxonomy that currently requires manifest-sourced input schemas; Task,
Workflow, and Trigger bindings retain their existing implementation-derived
schemas for backward compatibility.

### Container adapter

`step_registry_from_bindings(bindings)` builds a container `StepRegistry`
with one transport handler per registered step. The adapter converts each
`ContainerInvocation` into an `ExecutionContext` (sharing the same cooperative
cancellation event), runs the same `StepExecutor` input-validation boundary used
by direct Python execution, checks the returned object against the step's output
model, and returns a `ContainerResult`. Invalid input returns the shared wrapper
result with its wrapper-schema digest; valid input returns the step output and
echoes the installed `outputSchemaDigest` supplied in the runtime context,
matching the platform contract used by the HTTP runner; when no installed digest is supplied, a
provisional canonical digest of the implementation's Pydantic output model is
used until the registry-binding story binds the installed manifest schema.
Authors never import `ContainerInvocation` to write or test a step.

### Installed revision mapping

An immutable installed step revision maps a declared step to its Python
implementation:

- The step identity comes from the installed descriptor. It is never inferred
  from a source filename.
- Authors do not hard-code build-generated revision IDs.
- The required per-step `module:Class` entry point and its digest binding are
  owned by the separate SDK registry-binding story. In source manifests,
  `spec.runtime.entryPoint` names that implementation identity inside the
  digest-pinned workload image.

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
