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
python -m pytest sdks/python/tests -q
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
| [`sdks/python/packages/sdk`](../sdks/python/packages/sdk) | Authoring compiler, CLI, packaging, registry, catalog, and settings behavior. |
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
