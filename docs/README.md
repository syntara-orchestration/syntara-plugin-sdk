# Syntara Plugin SDK documentation

Choose the guide that matches what you are doing.

| If you want to… | Read… |
| --- | --- |
| Install the CLI or prepare a source checkout | [Installation](installation.md) |
| Create and package a first plugin | [Getting started](getting-started.md) |
| Configure a plugin, publish an artifact, or update a catalog | [User guide](user-guide.md) |
| Understand every YAML manifest field | [Plugin language reference](plugin-language.md) |
| Configure `syntara-plugin.yaml` | [Build settings reference](build-settings.md) |
| Test step implementations locally | [Testing reference](testing.md) |
| Contribute to the SDK or change a contract | [Developer guide](developer-guide.md) |
| Prepare or approve a Python package release | [Release guide](releasing.md) |
| Understand the OCI artifact and container ABI boundaries | [Runtime and OCI model](runtime-and-oci.md) |

## Normative sources

The human-readable reference explains the authoring language, but the versioned
JSON Schemas under [`../contracts/schemas/v1alpha1`](../contracts/schemas/v1alpha1)
are normative. Use them for an editor integration, another language SDK, or a
strict validator. The shared test inputs are under
[`../contracts/fixtures`](../contracts/fixtures), and the container ABI source
is [`../contracts/proto/syntara_plugin/runtime/protocol/container.proto`](../contracts/proto/syntara_plugin/runtime/protocol/container.proto).

## Design records

The accepted design decisions live in [`../.sdlc/adrs`](../.sdlc/adrs):

- [SDK repository layout](../.sdlc/adrs/0001-language-sdk-layout.md)
- [Catalog index OCI artifact](../.sdlc/adrs/0002-catalog-index-oci-artifact.md)
- [Catalog publication boundaries](../.sdlc/adrs/0003-bounded-catalog-index-publication.md)
- [Plugin publication boundaries](../.sdlc/adrs/0004-bounded-plugin-artifact-publication.md)
- [Generic container request hash](../.sdlc/adrs/0005-generic-container-request-hash.md)
- [Custom-runner preview scope](../.sdlc/adrs/0006-deferred-custom-runner-build-ux.md)
- [SDK local test harness](../.sdlc/adrs/0007-sdk-testing-harness.md)
- [Testing harness inside the SDK distribution](../.sdlc/adrs/0008-testing-harness-inside-sdk.md)
