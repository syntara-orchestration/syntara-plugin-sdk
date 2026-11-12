# Developer guide

This guide is for contributors to the SDK itself. For authoring a plugin, start
with the [getting-started guide](getting-started.md) instead.

## Prerequisites

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- Podman only when exercising a custom-runner image build
- Podman, OpenSSL, registry network access, and PyPI access only for the
  opt-in runtime-image integration test

From the repository root, create the development environment and run the test
suite:

```sh
uv venv
source .venv/bin/activate
uv sync --all-groups --active --inexact
python -m pytest sdks/python/tests examples -q
```

Run the CLI from the checkout after syncing the environment:

```sh
syntara-plugin --help
syntara-plugin validate examples/hello-world/plugin.yaml \
  --settings examples/hello-world/syntara-plugin.yaml
```

Run the opt-in runtime-image integration test when changing the container
bootstrap or image contract:

```sh
make test-integration
```

The test builds a temporary image from the pinned UBI Python base, installs the
SDK runtime and the checked-in [Existing step types](../examples/existing-step)
plugin from local wheels, starts the server with mutual TLS, and invokes its
Action, Control, and Trigger steps over gRPC. It does not require the backend,
Kubernetes, or the Execution Plane.

The **Execution Plane compatibility** GitHub Actions workflow runs on pull
requests and pushes to `devel`. It builds the
[Existing step types](../examples/existing-step) plugin
from this checkout, pushes it to a disposable registry, and dispatches its
Action, Control, and Trigger through the pinned Execution Plane Kubernetes
worker using the registry-resolved image digest. The workload advertises exact generated step revisions, and the tests select
those revisions and check their outputs and schema digests. A skip or missing case fails the run.

To run the static fixture checks without containers or a cluster:

```sh
make test-execution-plane-helpers
```

For the full test, use a disposable PostgreSQL database, an Execution Plane
checkout at the commit in `EP_REVISION`, and a disposable Kind
cluster created with `tests/execution_plane/kind.yaml`. Install uv, Docker (or
a compatible engine), Kind, kubectl, psql, and curl. The engine must support
Kind networking and node `exec`; the selected kubectl context must point to
the disposable cluster. Configure the database and run:

```sh
EP_PATH=/path/to/syntara-execution-plane \
EP_REVISION=<full-commit-sha> \
EP_TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/execution_plane \
make test-execution-plane
```

The compatibility workflow pins `EP_REVISION` once at its root and accepts an
optional `ep-ref` input for `workflow_dispatch`. The helper script requires a
full 40-character commit SHA and verifies that the checkout matches it.

The target installs dependencies without checkout package build hooks, builds
local wheels and the image, and starts a registry and the EP API. It requires
the database and its `execution_plane_runtime` role to exist; alternatively set
`EP_COMPAT_CREATE_DATABASE=1` and provide `PGHOST`, `PGUSER`, and `PGPASSWORD`
for a disposable PostgreSQL server. Override `CONTAINER_ENGINE`,
`EP_KIND_CLUSTER_NAME`, `EP_REGISTRY_NAME`, `EP_REGISTRY_PORT`, and
`EP_COMPAT_LOG_DIR` as needed. The cluster node's egress rules are changed for
the duration of the cluster, so delete that cluster after the test. Failure
logs and Kubernetes diagnostics are saved in the log directory.

The pinned EP revision does not yet supply resolved execution data to
workloads. This test therefore generates a fixture from the compiled plugin
registration and places it in the test image; this is not a publishing path.
Update the pinned EP revision and harness together when its helpers or workload
contract change.

## Repository map

| Location | Responsibility |
| --- | --- |
| [`contracts/schemas/v1alpha1`](../contracts/schemas/v1alpha1) | Normative plugin, catalog, and runtime JSON Schemas. |
| [`contracts/proto`](../contracts/proto) | Versioned container/runtime protobuf ABI. |
| [`contracts/fixtures`](../contracts/fixtures) | Cross-language conformance fixtures. |
| [`sdks/python/packages/sdk`](../sdks/python/packages/sdk) | Authoring compiler, CLI, packaging, registry, catalog, settings behavior, and the local testing harness (`syntara_plugin.testing`). |
| [`sdks/python/packages/contracts`](../sdks/python/packages/contracts) | Shared contracts distribution used by the SDK and Execution Plane. |
| [`sdks/python/tests`](../sdks/python/tests) | Unit and CLI contract tests. |
| [`examples`](../examples) | End-user examples that must remain runnable. |

## Change contracts deliberately

The schemas and protobufs are the public SDK specification. Do not implement a
private adapter-specific interpretation.

When changing a contract:

1. Update the canonical schema or protobuf under [`contracts`](../contracts).
2. Update the Python contracts bundle and any affected conformance fixture.
3. Regenerate protocol code when a protobuf changes:

   ```sh
   make generate-container-protocol
   ```

4. Add or update focused tests in [`sdks/python/tests`](../sdks/python/tests).
5. Update the relevant reference in this [`docs`](.) directory and a runnable
   example if an author-visible workflow changed.
6. Run the full Python suite and check the diff:

   ```sh
   python -m pytest sdks/python/tests -q
   git diff --check
   ```

The SDK is pre-release. Prefer a clear forward-only contract over a
compatibility shim; removed configuration keys should fail validation rather
than silently selecting legacy behavior. Follow the [release guide](releasing.md)
when preparing a published package release.

## Documentation and review checklist

- Keep the root [README](../README.md) short and task-oriented.
- Put command walkthroughs in the [user guide](user-guide.md).
- Describe every author-facing manifest key in the
  [plugin language reference](plugin-language.md), with a link to its schema.
- Describe every settings key in the
  [build settings reference](build-settings.md).
- Validate [`examples/hello-world`](../examples/hello-world) after changing
  compilation, settings discovery, or packaging behavior.

Do not commit generated build output, local registry credentials, or secrets.
