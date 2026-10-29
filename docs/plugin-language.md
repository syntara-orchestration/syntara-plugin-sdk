# Plugin language reference

This is the author-facing reference for `syntara.io/v1alpha1`. The JSON Schemas in [`../contracts/schemas/v1alpha1`](../contracts/schemas/v1alpha1) are normative and reject unknown fields.

## Common envelope

Every plugin inventory and target document starts with this envelope.

| Key | Meaning |
| --- | --- |
| `apiVersion` | Contract version. Use `syntara.io/v1alpha1`. |
| `kind` | Document type; determines the schema and required fields. |
| `metadata` | Human-facing identity. A plugin uses `namespace`, `name`, `version`, `displayName`, and `description`. Target documents omit `namespace` and `version`. |
| `metadata.namespace` | DNS-like publisher namespace. Plugin only. |
| `metadata.name` | Stable, lowercase document identity. |
| `metadata.version` | Semantic plugin release version. Plugin only. |
| `metadata.displayName` | Short UI label. |
| `metadata.description` | User-facing explanation of the plugin or target. |

## Terms and execution model

A **Step type** is a concrete catalog capability such as **HTTP Request**,
**Create GitHub Issue**, or **Launch AAP Job Template**. Every Step type has
one lowercase **category**: `action`, `control`, or `trigger`. Action Step
types cover domain integrations, API calls, scripts, and data manipulation;
`Control` maps to `control`; and `Trigger` maps to `trigger`. Category states
the capability's architectural role; it does not choose its execution owner.
A **Runner** is either Syntara's `platform-runner` or an author-owned
`custom-runner`. **Execution** is the invocation of the selected Step type
through that runner or, for control and ingress Step types, through the
platform control plane.

```text
Step type -> Category -> Runner -> Execution
Launch AAP Job Template -> action -> platform-runner -> launches the selected job template
HTTP Request -> action -> platform-runner -> sends the configured request
Create GitHub Issue -> action -> custom-runner -> the pinned runner invokes the typed implementation
Launch production inventory sync -> control -> platform-runner -> invokes the selected workflow in the control plane
Pull request webhook -> trigger -> platform-runner -> webhook ingress delivers the event in the control plane
```

Use a Control Step type for orchestration behavior in the control plane, such
as invoking a workflow or composing execution flow. A Control Step type is
not a generic label for every catalog capability.
`spec.invocation.kind: orchestration` identifies control-flow metadata; a
Control Step type that invokes a named workflow uses
`spec.invocation.kind: workflow-invocation` and
`spec.invocation.workflow`. Category is unrelated to a terminal failure's
`error_code` classification.

## `kind: Plugin`

Schema: [`plugin.schema.json`](../contracts/schemas/v1alpha1/plugin.schema.json). This is the release inventory at `plugin.yaml`.

| Key | Meaning |
| --- | --- |
| `spec.targets` | Required list of contained YAML target paths. These are Step types, integration types, and credential recipes included in the release. |
| `spec.documentation.path` | Required contained UTF-8 Markdown file published with the plugin. |
| `spec.workload.image` | Optional logical image ID shared by every `custom-runner` Step type. A plugin has at most one runner image, which is bound to an immutable container digest during build. |
| `spec.providers` | Optional declarative form-provider image definitions. Each item has an `id` and a logical `image` ID. |

## `kind: Action`

Schema: [`action.schema.json`](../contracts/schemas/v1alpha1/action.schema.json). An `Action` Step type (category `action`) performs a domain operation or integration.

| Key | Meaning |
| --- | --- |
| `spec.runtime.kind` | Required execution owner: `platform-runner` or `custom-runner`. |
| `spec.runtime.implementation` | Required for `platform-runner`; a registered step reference in `<namespace>.<plugin>.<step>@version` form, such as `example.hello-world.hello@0.1.0`. Forbidden for `custom-runner`. |
| `spec.runtime.operation` | Optional HTTP operation metadata for a `platform-runner` Action: an inert method, relative path, and optional mappings. Forbidden for `custom-runner`. |
| `spec.runtime.entryPoint` | Required for `custom-runner`; a `module:Class` handle for the Step implementation inside the digest-pinned runner image. It is an implementation identity, not a placement decision: Syntara policy may run it in the selected host execution path or delegate it to the Execution Plane. |
| `spec.domain` | Optional qualified domain identifier that groups an Action Step type independently of its runner. |
| `spec.integrationType` | Optional qualified integration type required by the Action Step type. A workflow selects an instance of this type, not raw credentials. |
| `spec.input` | Required input JSON Schema source. |
| `spec.output` | Required JSON Schema source for the Step type's business result. |
| `spec.error` | Optional JSON Schema source for failure details, separate from the successful business result schema in `spec.output`. |

Custom-runner Step types implement `spec.runtime.entryPoint` with the typed
Python API; see [Typed Python steps](runtime-and-oci.md#typed-python-steps).
Platform-runner references identify a step in any registered plugin. Compilation
preserves the reference; the AO Plugin Registry resolves its image, entry point,
and ABI during registration. Example references are placeholders and must name
an implementation available in the target installation.

### `http.v1` operation

Schema: [`http-operation.schema.json`](../contracts/schemas/v1alpha1/http-operation.schema.json).

| Key | Meaning |
| --- | --- |
| `method` | HTTP method declared by the action. |
| `path` | Origin-less relative path template. It cannot select an endpoint. |
| `requestMap` | Optional mapping from action input fields to a JSON request body. |
| `responseMap` | Optional mapping from response fields to the action output. |

The plugin never declares an origin, authorization header, secret, command, or container image for a `platform-runner` action. Syntara resolves those pieces after installation.

## `kind: Control`

Schema: [`control.schema.json`](../contracts/schemas/v1alpha1/control.schema.json). A `Control` Step category represents orchestration behavior and uses the same runner declaration as every other executable Step type.

`spec.invocation.kind: orchestration` identifies control-flow metadata. Use
`workflow-invocation` together with `spec.invocation.workflow` for a Control
Step type that invokes a named workflow. `spec.runtime` selects the approved
`platform-runner` or `custom-runner`; `entryPoint` identifies a custom-runner
implementation and `implementation` identifies a platform runner. Control contracts
define public input and business-result output schemas and may optionally
declare a separate error schema. For a `workflow-invocation`, the parent
Control Step consumes the child workflow's declared result; the parent's
`spec.output` describes the result exposed by that invocation. Runtime wrapper
failure metadata is separate from both schemas.

## `kind: IntegrationType`

Schema: [`integration.schema.json`](../contracts/schemas/v1alpha1/integration.schema.json). An integration type describes the reusable connection shape a user can create.

| Key | Meaning |
| --- | --- |
| `spec.id` | Qualified, stable integration identifier, for example `acme.github.connection`. |
| `spec.endpointKinds` | Allowed endpoint categories supplied by the integration form. |
| `spec.credentialTypes` | Credential recipe IDs accepted by the integration. |
| `spec.configuration` | JSON Schema source for non-secret integration configuration. |

## `kind: CredentialRecipe`

Schema: [`credential-recipe.schema.json`](../contracts/schemas/v1alpha1/credential-recipe.schema.json). It defines the secret shape; it does not contain a secret value.

| Key | Meaning |
| --- | --- |
| `spec.id` | Qualified, stable credential type identifier. |
| `spec.integrationType` | Integration type that can use this recipe. |
| `spec.credentialSchema` | JSON Schema source for credential fields. |
| `spec.secretFields` | Names of fields that Syntara stores and handles as secrets. |
| `spec.issuance.kind` | Credential acquisition mechanism: `static-bearer.v1`, `oauth2-authorization-code.v1`, or `jwt-token-exchange.v1`. |
| `spec.issuance.scopes` | Required OAuth scopes for `oauth2-authorization-code.v1`. |
| `spec.issuance.pkce` | Must be `true` for OAuth authorization-code issuance. |
| `spec.issuance.audience` | Required token-exchange audience for `jwt-token-exchange.v1`. |

## `kind: Trigger`

| Kind | Schema | Key fields |
| --- | --- | --- |
| `Trigger` | [`trigger.schema.json`](../contracts/schemas/v1alpha1/trigger.schema.json) | `spec.driver`, `spec.runtime`, `spec.configuration`, `spec.input`, `spec.output`, optional `spec.error` |
| `TriggerDriver` | [`trigger-driver.schema.json`](../contracts/schemas/v1alpha1/trigger-driver.schema.json) | `spec.id`, `spec.transport`, `spec.events` |
| `TriggerEventMapping` | [`trigger-event-mapping.schema.json`](../contracts/schemas/v1alpha1/trigger-event-mapping.schema.json) | `spec.driver`, `spec.event`, `spec.configuration`, `spec.output` |

`Trigger` refers to the Step category `trigger`. `Trigger.driver` chooses a versioned platform
ingress driver, while `spec.runtime` selects the runner that implements the
Step type. `spec.configuration` is driver-owned setup and is not runtime
input. `spec.input` and `spec.output` are the public event and business-result
contracts. Optional `spec.error` describes the complete structured error result.
The runtime also enforces the SDK base fields; without `spec.error`, the SDK
base schema is used. Neither schema describes the runtime wrapper itself.
The same category also represents a child-workflow entry in reference-mode
composition; its declared public output is the result consumed by the parent
`Control` step, not the results of the child's internal steps.
`TriggerDriver.transport` is one of
`webhook`, `schedule`, `event-source`, or `poll`.

## Compiled `PluginDescriptor`

The compiler emits a strict
[`plugin-descriptor.schema.json`](../contracts/schemas/v1alpha1/plugin-descriptor.schema.json)
document. Each compiled executable target carries its canonical `kind`, plus
`schemaDigests` for every materialized schema.
The digest is the canonical JSON SHA-256 of that resolved schema. Consumers
must reject unknown fields, an unknown or unsupported `kind`, duplicate target
identities, or a schema
whose digest does not match its content.

### Revision identities

The compiler also emits `spec.plugin_revision_id`, `spec.sdkVersion`, and a
`step_revision_id` on each Action, Control, and Trigger target. Authors do not
write these fields. IDs are lowercase `sha256:` digests. `sdkVersion` is the
installed `syntara-plugin-sdk` version expressed as SemVer; for example, the
Python distribution version `0.1.0a1` compiles to `0.1.0-alpha.1`.

The versioned `syntara.compiled-contract/v1` projection is hashed using SHA-256
over canonical UTF-8 JSON: keys are sorted by Unicode code point, insignificant
whitespace is removed, non-ASCII characters are unescaped, and non-finite
numbers are rejected. Arrays retain order. The projection's `projection` and
`kind` fields domain-separate it, so a changed algorithm requires a new
projection version. Serialization follows `canonical_json_bytes`, including
Python JSON-number rendering (`1.0`, `-0.0`, and `1e-07` remain distinct
spellings); object keys and strings are not Unicode-normalized.

The plugin projection contains `projection`, `kind: "plugin"`, and the complete
compiled descriptor without generated IDs. It excludes every workload binding
referenced by a custom-runner target because that image embeds the registration
manifest containing these IDs. Provider-only and unreferenced workload bindings
remain in the projection. The step projection contains `projection`,
`kind: "step"`, the plugin's namespace/name/version, `sdkVersion`,
`contractBundleDigest`, and the complete compiled target without its generated
ID. Thus a change to a step, plugin release coordinates, SDK version, or
contract bundle changes that step's ID; a change to another target or plugin
documentation does not. A platform-runner implementation reference is part of
its compiled target and therefore participates in both revision projections.

Revision IDs identify compiled contracts, not image bytes. The workload image
digest remains separately bound in `spec.workloads` and in the complete
descriptor and OCI artifact. Different images can therefore share contract IDs
and must be distinguished by their pinned image digests.

## Schema sources

`input`, `output`, `error`, `configuration`, and `credentialSchema` use a document source:

| Shape | Meaning |
| --- | --- |
| `kind: inline` with `value` | The JSON Schema or mapping is embedded in the YAML file. |
| `kind: asset` with `path` | The JSON Schema or mapping is a contained file bundled in the OCI artifact. |

Use `asset` for reusable or substantial schemas. Both forms are immutable parts of the compiled plugin artifact.

### Form and result descriptions

Every schema source is a JSON Schema 2020-12 document. Use its standard
`description` annotation on the root object and on every user-facing property.
Syntara preserves those annotations in inline schemas and bundled schema
assets, so a form can show field guidance and an inspector can explain output
and error fields without a separate UI-specific metadata format.

```yaml
type: object
description: Inputs needed to create the requested resource.
properties:
  name:
    type: string
    description: Human-readable name for the new resource.
```

Descriptions are documentation only: they do not change validation or expose
credentials. Apply the same pattern to Step type `input`, `output`, and
optional `error`, as well as integration `configuration` and credential schemas.

## More contracts

The catalog index, runtime work items, provider protocol, and container ABI are platform-facing contracts rather than normal plugin authoring YAML. Read [runtime and OCI model](runtime-and-oci.md) and the relevant schema files when working on those boundaries.
