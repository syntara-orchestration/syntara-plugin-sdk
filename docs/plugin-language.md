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

## `kind: Plugin`

Schema: [`plugin.schema.json`](../contracts/schemas/v1alpha1/plugin.schema.json). This is the release inventory at `plugin.yaml`.

| Key | Meaning |
| --- | --- |
| `spec.targets` | Required list of contained YAML target paths. These are the actions, triggers, integration types, and credential recipes included in the release. |
| `spec.documentation.path` | Required contained UTF-8 Markdown file published with the plugin. |
| `spec.workload.image` | Optional logical image ID shared by `custom-workload` actions. It is bound to an immutable container digest during build. |
| `spec.providers` | Optional declarative form-provider image definitions. Each item has an `id` and a logical `image` ID. |

## `kind: Action`

Schema: [`action.schema.json`](../contracts/schemas/v1alpha1/action.schema.json). An Action is a workflow step type.

| Key | Meaning |
| --- | --- |
| `spec.runtime.kind` | Required execution owner: `platform-runner` or `custom-workload`. |
| `spec.runtime.driver` | Required for `platform-runner`; names an approved versioned driver, such as `http.v1`. Forbidden for `custom-workload`. |
| `spec.runtime.operation` | Required for `http.v1`; declares an inert HTTP method, relative path, and optional mappings. Forbidden for `custom-workload`. |
| `spec.runtime.entryPoint` | Required for `custom-workload`; a `module:Class` handle for the action implementation inside the digest-pinned workload image. It is an implementation identity, not a placement decision: Syntara policy may execute it in process or delegate it to the Execution Plane. |
| `spec.integrationType` | Optional qualified integration type required by the action. A workflow selects an instance of this type, not raw credentials. |
| `spec.input` | Required input JSON Schema source. |
| `spec.output` | Required output JSON Schema source. |
| `spec.error` | Required error JSON Schema source. |

Custom-workload actions implement `spec.runtime.entryPoint` with the typed
Python API; see [Typed Python steps](runtime-and-oci.md#typed-python-steps).

### `http.v1` operation

Schema: [`http-operation.schema.json`](../contracts/schemas/v1alpha1/http-operation.schema.json).

| Key | Meaning |
| --- | --- |
| `method` | HTTP method declared by the action. |
| `path` | Origin-less relative path template. It cannot select an endpoint. |
| `requestMap` | Optional mapping from action input fields to a JSON request body. |
| `responseMap` | Optional mapping from response fields to the action output. |

The plugin never declares an origin, authorization header, secret, command, or container image for a `platform-runner` action. Syntara resolves those pieces after installation.

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

## Triggers

| Kind | Schema | Key fields |
| --- | --- | --- |
| `Trigger` | [`trigger.schema.json`](../contracts/schemas/v1alpha1/trigger.schema.json) | `spec.driver`, `spec.configuration`, `spec.output` |
| `TriggerDriver` | [`trigger-driver.schema.json`](../contracts/schemas/v1alpha1/trigger-driver.schema.json) | `spec.id`, `spec.transport`, `spec.events` |
| `TriggerEventMapping` | [`trigger-event-mapping.schema.json`](../contracts/schemas/v1alpha1/trigger-event-mapping.schema.json) | `spec.driver`, `spec.event`, `spec.configuration`, `spec.output` |

`Trigger.driver` chooses a versioned platform driver. `TriggerDriver.transport` is one of `webhook`, `schedule`, `event-source`, or `poll`. An event mapping declares the configuration and output shape for one driver event.

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
credentials. Apply the same pattern to action `input`, `output`, and `error`,
as well as integration `configuration` and credential schemas.

## More contracts

The catalog index, runtime work items, provider protocol, and container ABI are platform-facing contracts rather than normal plugin authoring YAML. Read [runtime and OCI model](runtime-and-oci.md) and the relevant schema files when working on those boundaries.
