# Developer guide

This guide is for contributors to the SDK itself. For authoring a plugin, start
with the [getting-started guide](getting-started.md) instead.

## Prerequisites

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- Podman only when exercising a custom-runner image build

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

## Repository map

| Location | Responsibility |
| --- | --- |
| [`contracts/schemas/v1alpha1`](../contracts/schemas/v1alpha1) | Normative plugin, catalog, and runtime JSON Schemas. |
| [`contracts/proto`](../contracts/proto) | Versioned container/runtime protobuf ABI. |
| [`contracts/fixtures`](../contracts/fixtures) | Cross-language conformance fixtures. |
| [`sdks/python/packages/sdk`](../sdks/python/packages/sdk) | Authoring compiler, CLI, packaging, registry, catalog, settings behavior, and the local testing harness (`syntara_plugin.testing`). |
| [`sdks/python/packages/runtime`](../sdks/python/packages/runtime) | In-container runtime implementation. |
| [`sdks/python/packages/contracts`](../sdks/python/packages/contracts) | Python package that ships the contract bundle. |
| [`sdks/python/tests`](../sdks/python/tests) | Unit and CLI contract tests. |
| [`examples`](../examples) | End-user examples that must remain runnable. |

## Change contracts deliberately

The schemas and protobufs are the public SDK specification. Do not implement a
private language-specific interpretation.

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

The SDK is not released yet. Prefer a clear forward-only contract over a
compatibility shim; removed configuration keys should fail validation rather
than silently selecting legacy behavior.

### Capture-budget rollout follow-ups

The SDK owns one protobuf contract and validates/carries `max_output_bytes`.
See [capture budgets and compatibility](runtime-and-oci.md#capture-budget-and-compatibility)
for presence, hashing, capability negotiation, and transport semantics. This
patch alone does not restore end-to-end script capture. Companion work:

1. **EP:** restore `max_output_bytes` in
   `src/execution_plane_client/commands/work.py::WorkloadInvocation`, forward
   it from `src/execution_plane/workload.py` to `ContainerInvocation`, and
   update helpers, OpenAPI, tests, the pinned SDK source/version, and lockfiles.
   Enforce the Health capability before dispatch. Keep the SDK-owned
   `ContainerService`; do not restore NodeService or vendor generated bindings.
2. **AO:** preserve the operator-resolved byte value when migrating the EP
   payload builder to its new command shape. The current setting resolves KiB
   to bytes, defaults to 1 MiB, and applies independently to both streams. Its
   operator UI minimum is not the generic protocol minimum.
3. **Production script runner:** enforce bounded byte capture while reading
   stdout and stderr, continue draining excess, handle UTF-8 boundaries, and
   preserve exit/failure behavior and the output schema. Decide how combined
   bounded output fits SDK and downstream delivery limits, including deliberate
   failures with partial output. Optional object storage does not by itself
   solve downstream retrieval or the adapter's inline-only partial failures.
4. **EP test fixture:** replace unbounded `communicate()` capture and
   `stdout[:10_000]`/`stderr[:10_000]` character slicing with bounded capture
   before using that fixture to demonstrate enforcement. It is not the
   production runner.

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
