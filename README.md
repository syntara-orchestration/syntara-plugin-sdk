# Syntara Plugin SDK

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![PostgreSQL 15+](https://img.shields.io/badge/postgresql-15+-blue.svg)](https://www.postgresql.org/)

A schema-driven framework for authoring, packaging, and registering custom automation steps for workflow orchestration. Build type-safe, composable automation steps with declarative YAML manifests that compile to runtime-ready JSON definitions.

## Features

- **🎯 Type-Safe Authoring** — Author steps in human-friendly YAML with JSON Schema validation (Draft-07)
- **🔒 Zero-Trust Security** — Authentication credentials are platform-managed UUID references, never step inputs; non-credential sensitive data is flagged `redact: true` and kept out of outputs, logs, and persisted state
- **📦 Four-Category Taxonomy** — `action` (integrations), `task` (compute), `workflow` (control flow), `trigger` (events)
- **⚡ Fast Canvas Rendering** — Compiled step definitions enable <500ms dynamic form rendering
- **🔌 Kubernetes-Native** — Follows K8s CRD conventions (`apiVersion`, `kind`, `metadata`, `spec`)
- **🛡️ Declarative Permissions** — Static capability inspection before execution-plane dispatch
- **🔄 Backwards Compatible** — Immutable output envelope (`StandardOutputWrapper`) ensures stable template expressions

## Quick Start

### Installation

```bash
# Install the Python SDK
pip install -e ./sdk-python

# Or install from the repository root
pip install -e .
```

### Scaffold and Package Steps

Use the CLI to create a shared-runner script step or a dedicated-image step:

```bash
syntara-sdk init normalize_payload --tier 2  # shared-runner script step
syntara-sdk init customer_lookup --tier 3 --image quay.io/example/customer-lookup:1.0.0  # dedicated container extension
syntara-sdk build customer_lookup/manifest.yaml --output customer-lookup-oci

# Local prototype: publish to OCI, then register in Syntara automatically
syntara-sdk push customer_lookup/manifest.yaml \
  --registry localhost:5000/syntara/steps/customer-lookup:1.0.0
```

Dedicated container-extension builds emit a standard OCI image manifest with artifact type
`application/vnd.syntara.step.manifest.v1+yaml` and the validated YAML manifest
in the `org.syntara.step.manifest` annotation.

`push` publishes the OCI metadata and then calls
`POST /api/v1/step-types` to add the step to Syntara's available-step catalog.
Use `--skip-register` for registry-only publishing or `--api-url` to target a
different Syntara instance.

### Create Your First Step

**1. Copy the plugin and step manifest examples:**

```bash
# Examples are labeled so they are not mistaken for manifests belonging to this repository.
cp plugin.example.yaml plugin.yaml
mkdir -p steps/my_http_step
cp manifest.example.yaml steps/my_http_step/manifest.yaml
cd steps/my_http_step
```

**2. Edit `manifest.yaml` (K8s CRD structure):**

```yaml
apiVersion: syntara.io/v1alpha1
kind: StepType

metadata:
  name: my_http_step
  displayName: My HTTP Step
  icon: globe
  description: Custom HTTP request step with retry logic
  tags:
    - integration:rest-api
    - network:external
  license: Apache-2.0

spec:
  category: action
  execution:
    image: quay.io/syntara/http-request-executor:latest
    entrypoint: src.main:MyHttpStep

  declaredRequirements:
    capabilities:
      - network-egress
      - readonly-root-filesystem
    platformVersion: ">=3.0.0"

  schedulingControls:
    connectivity_requirements:
      - host: api.github.com
        ports:
          - port: 443
            protocol: TCP

  inputs:
    properties:
      url:
        type: string
        description: Target URL
      method:
        type: string
        enum: [GET, POST, PUT, DELETE]
        default: GET
    required:
      - url

  outputs:
    allOf:
      - $ref: "../../schemas/common-definitions.json#/definitions/StandardOutputWrapper"

  resourceRequirements:
    limits:
      cpu: 500m
      memory: 256Mi
    requests:
      cpu: 100m
      memory: 128Mi

  executionTimeout: 60
```

### Validate & Test

**Validate manifest using SDK:**

```python
from syntara_sdk.compiler import compile_manifest, validate_manifest

# Validate only
manifest = {"apiVersion": "syntara.io/v1alpha1", ...}
errors = validate_manifest(manifest)
if errors:
    print("Validation errors:", errors)

# Compile (validates + prepares a descriptor)
descriptor = compile_manifest("steps/my-http-step/manifest.yaml")
print(f"✓ Compiled: {descriptor['metadata']['name']}")
```

**Test your step locally:**

```bash
# Run the SDK runner
python -m syntara_sdk.runner \
  --module my_step_package.src.main \
  --class MyStep \
  --inputs-file test_inputs.json
```

### Publish and Register

The OCI registry is the artifact source, and Syntara PostgreSQL is hydrated by
the registration API. The CLI publishes the OCI manifest and then registers the
image with Syntara automatically:

```bash
syntara-sdk push tests/fixtures/steps/http_request/manifest.yaml \
  --registry localhost:5000/syntara/steps/http-request:1.0.0 \
  --api-url http://localhost:5173
```

Use `--skip-register` when publishing to a registry without making the step
available in Syntara yet. An administrator or deployment process can then
register the existing image later by posting its `image_ref` to
`POST /api/v1/step-types`.

## Architecture

Syntara follows a **define-once, consume-everywhere** model:

```mermaid
graph LR
    AUTHOR["Author<br/>manifest.yaml"]
    VALIDATE["Validate<br/>(JSON Schema)"]
    BUILD["Build<br/>step-definition.json"]
    REGISTRY["Registry<br/>(PostgreSQL)"]
    CANVAS["Canvas<br/>(React UI)"]
    ORCHESTRATOR["Orchestrator<br/>(Workflow Engine)"]

    AUTHOR --> VALIDATE --> BUILD --> REGISTRY
    REGISTRY --> CANVAS
    REGISTRY --> ORCHESTRATOR
```

### Step Categories

| Category | Purpose | Examples |
|----------|---------|----------|
| **action** | External API integrations | HTTP requests, GitHub issues, Slack messages |
| **task** | Atomic compute operations | Script executor, data transformation |
| **workflow** | Control flow and composition logic | Loops, conditions, switches, subworkflow calls |
| **trigger** | Event entry points | Webhooks, schedules, subworkflow triggers |

### Execution Placement

Manifests do not declare where a step runs. The Execution Plane derives placement from the
step's category, resource requirements, declared capabilities, and administrative registration
policy. `spec.execution.image` names the runtime the execution plane runs the step *in* -- an
interpreter plus the SDK, holding no step code. The step implementations live in the plugin
artifact, and `spec.execution.entrypoint` (`module.path:ClassName`) selects which one to load.
See [Dispatch and Handoff](docs/architecture.md#dispatch-and-handoff).

## Examples

The SDK includes reference implementations for each step category:

| Example | Category | Location |
|---------|----------|----------|
| **HTTP Request** | action | [tests/fixtures/steps/http_request/](tests/fixtures/steps/http_request/) |
| **Script Executor** | task | [tests/fixtures/steps/script_executor/](tests/fixtures/steps/script_executor/) |
| **Subworkflow Call** | workflow | [tests/fixtures/steps/subworkflow_call/](tests/fixtures/steps/subworkflow_call/) |
| **Subworkflow Trigger** | trigger | [tests/fixtures/steps/subworkflow_trigger/](tests/fixtures/steps/subworkflow_trigger/) |

### Run Example Tests

```bash
# Registry platform tests (9 tests)
pytest tests/registry/test_postgres_registry.py

# HTTP Request step unit tests (12 tests)
pytest tests/test_http_step.py -v

# Run step directly with CLI runner
python -m syntara_sdk.runner \
  --module tests.fixtures.steps.http_request.src.main \
  --class HttpRequestStep \
  --inputs-file test_inputs.json
```

## Security Model

### Zero-Trust Credentials

Authentication credentials are never step inputs. Manifests and compiled descriptors store abstract UUID references only; the execution plane resolves them and supplies the values out of band:

```yaml
spec:
  credentialSpecification:
    credential_requirements:
      - name: api_auth
        types: [API Key, Bearer Token]
        mount_type: tmpfs_file
        mount_path: /tmp/api-key
```

Non-credential sensitive data (PII, business-sensitive fields) *is* supplied as a normal input, flagged `redact: true`. Neither a credential value nor a `redact`-flagged value may appear in `StandardOutputWrapper` fields, workflow variables, error messages, stack traces, execution logs, or persisted state. The platform dispatcher and execution plane enforce this and are the authoritative security boundary; SDK base classes additionally check that a step does not echo a flagged input into its output.

### Declarative Permissions

Every step declares its requirements upfront in the manifest:

```yaml
spec:
  declaredRequirements:
    capabilities:
      - network-egress        # Can make outbound HTTP requests
      - script-execution      # Can execute scripts
      - tmpfs-mount          # Needs ephemeral storage
    platformVersion: ">=3.0.0"

  schedulingControls:
    connectivity_requirements:
      - host: api.github.com
        ports:
          - port: 443
            protocol: TCP
```

Administrators can audit these requirements **before** execution-plane dispatch. The execution plane consumes the declarations together with registration policy to apply its runtime controls.

## Documentation

- **[Architecture Guide](docs/architecture.md)** — Complete technical specification
- **[Common Definitions](schemas/common-definitions.json)** — Platform meta-schema (JSON Schema Draft-07)
- **[HTTP Request Example](tests/fixtures/steps/http_request/)** — Full `action` step reference implementation
- **[Script Executor Example](tests/fixtures/steps/script_executor/)** — Full `task` step reference implementation
- **[Subworkflow Trigger Example](tests/fixtures/steps/subworkflow_trigger/)** — Child-side `trigger` descriptor and Reference-mode eligibility contract
- **[Subworkflow Call Example](tests/fixtures/steps/subworkflow_call/)** — Parent-side `workflow` step descriptor for Reference-mode child invocation

## Development

### Prerequisites

- **Python 3.12+**
- **PostgreSQL 15+** (for registry storage)
- **Kubernetes/OpenShift cluster** (for container step execution)
- **uv** (Python package manager): `pip install uv`

### Setup

```bash
# Clone the repository
git clone https://github.com/yourusername/syntara-step-sdk.git
cd syntara-step-sdk

# Install the SDK package
pip install -e ./sdk-python

# Install dev dependencies
pip install -e .

# Install test dependencies
pip install pytest pytest-mock respx httpx

# Set up PostgreSQL test database (optional)
export SYNTARA_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/syntara_test

# Run tests
uv run pytest tests/registry/test_postgres_registry.py
```

### Project Structure

```
syntara-step-sdk/
├── plugin.example.yaml                # Root plugin manifest example
├── manifest.example.yaml              # Step manifest example
├── plugin.schema.json                 # Root plugin schema entry point
├── manifest.schema.json               # Step schema entry point
├── schemas/
│   └── common-definitions.json        # Platform meta-schema
├── steps/
│   ├── http-request/                  # Action step example
│   ├── script-executor/               # Task step example
│   └── subworkflow-trigger/           # Trigger step example
├── sdk-python/
│   └── syntara_sdk/                   # Python SDK & base classes
├── docs/
│   └── architecture.md                # Technical specification
├── tests/                             # Integration tests
├── pyproject.toml                     # Python package config
└── README.md                          # This file
```

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.
