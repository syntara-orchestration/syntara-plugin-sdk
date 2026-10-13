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
workload. It carries action inputs, output/error contracts, and approved file
paths. It does not carry raw credentials in workflow JSON, environment
variables, or command-line arguments.

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
implement `run()`; later SDK stories add raw-input validation and the common
output envelope at the dispatch boundary without changing that hook.

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

`StepDescriptor` associates a declared step identity with a taxonomy and the
input/output JSON schemas derived from the step's Pydantic models.
`StepDescriptor.for_step()` derives everything from the class itself, and
`StepBindings` retains each descriptor-and-implementation pair:

```python
from syntara_plugin.runtime import StepDescriptor, StepBindings

bindings = StepBindings()
step = ActionEcho()
bindings.bind(StepDescriptor.for_step("acme.plugin.echo", step), step)
binding = bindings.binding("acme.plugin.echo")
resolved = binding.step
```

Binding rejects malformed or duplicate identities, taxonomy mismatches between
the descriptor and the Python class, and attempts to bind a platform-managed
driver (such as `http.v1`) as a Python implementation. `StepBindings` is an
in-process lookup table, not the Automation Orchestrator's registration
database; mapping an installed step revision to a declared identity belongs to
the registry-binding story.
Pydantic models are implementation types, not replacement manifests: the
canonical schema bundle remains the source of truth for manifest kinds such as
Action and Trigger, and comparing implementation schemas with source manifests
is owned by the schema-drift validation story.

### Container adapter

`action_registry_from_steps(registry)` builds a container `ActionRegistry`
with one transport action per registered step. The adapter converts each
`ContainerInvocation` into an `ExecutionContext` (sharing the same cooperative
cancellation event), validates the raw JSON inputs against the step's input
model, checks the returned object against the step's output model, and returns
a `ContainerResult`. The output schema digest echoes the installed
`outputSchemaDigest` supplied in the runtime context, matching the platform
contract used by the HTTP runner; when no installed digest is supplied, a
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
