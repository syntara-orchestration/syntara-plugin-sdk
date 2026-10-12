# User guide

Use this guide after completing [getting started](getting-started.md). It describes the release path; the [plugin language reference](plugin-language.md) describes the YAML documents themselves.

## The normal authoring loop

```text
edit plugin source → validate → inspect → build archive → inspect archive
```

```sh
syntara-plugin validate
syntara-plugin inspect
syntara-plugin build
syntara-plugin inspect ./dist/my-plugin.oci.tar
```

The default build has no registry side effects. It compiles the selected manifest, validates the contracts, and writes a deterministic OCI-layout tar archive.

## Settings and command-line precedence

Keep stable, non-secret defaults in `syntara-plugin.yaml`. The lookup order is:

1. `--settings PATH`;
2. `syntara-plugin.yaml` next to the selected `plugin.yaml`;
3. `syntara-plugin.yaml` in the current directory.

Only the first file found is used; files are never merged. A command-line value overrides the selected setting. Relative paths are relative to the settings file. See the complete [build settings reference](build-settings.md).

Never place passwords, tokens, or private keys in settings. For local development, `registry.authFile` may point to a Docker- or Podman-compatible `auth.json` file with an inline Basic credential. The SDK reads that credential only in memory; it never writes it into the settings file, artifact, catalog, or command output. For CI, use `--password-stdin`, which overrides `registry.authFile` for that invocation. Use top-level `output.verbose` and `output.json` for local command-output defaults.

## Publish a verified archive

After a successful offline build, publish that exact archive:

```sh
printf '%s' "$REGISTRY_PASSWORD" | syntara-plugin publish \
  ./dist/my-plugin.oci.tar \
  --registry-origin https://registry.example.test \
  --repository acme/plugins/my-plugin \
  --channel 0.1.0 \
  --username publisher \
  --password-stdin
```

The command verifies the local archive first, uploads its immutable blobs, and reports an immutable `repository@sha256:...` reference. It does not recompile the source. Production registries require HTTPS; HTTP is allowed only for an explicit loopback development registry.

## Optional custom-workload image build

For a plugin containing an action with `runtime.kind: custom-workload`, the developer-preview flow can build and push the author-owned image, use the registry-returned digest in the metadata archive, and publish the archive:

```sh
printf '%s' "$REGISTRY_PASSWORD" | syntara-plugin build \
  --with-workload --publish --password-stdin --verbose
```

This performs two distinct OCI publications:

1. Podman pushes the workload image to `registry.workloadRepository` and returns its immutable digest.
2. The SDK publishes the plugin metadata archive to `registry.artifactRepository`.

The feature does not yet prove custom-workload execution in the Execution Plane, secret delivery, signing, or production egress policy. See [ADR 0006](../.sdlc/adrs/0006-deferred-custom-workload-build-ux.md).

## Update a catalog

Catalog mutation is a separate, shared release operation. It should normally run in a serialized platform release pipeline after review and signing:

```sh
printf '%s' "$REGISTRY_PASSWORD" | syntara-plugin catalog update \
  ./dist/my-plugin.oci.tar \
  --password-stdin --json
```

The command reads the configured catalog channel, verifies it, and either creates generation `1` or appends one new immutable plugin entry. It preserves existing entries and links the new index through `previousIndexDigest`.

An exact retry is a no-op. Catalog expiration renewal and full catalog recovery are not automated yet; they must be owned by the catalog publisher rather than individual plugin authors.
