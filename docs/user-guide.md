# User guide

Use this guide after completing [getting started](getting-started.md). It describes the release path; the [plugin language reference](plugin-language.md) describes the YAML documents themselves.

## The normal authoring loop

```text
edit plugin source → validate → inspect → run locally → build archive → inspect archive
```

```sh
syntara-plugin validate
syntara-plugin inspect
syntara-plugin run create-issue --input-file input.json
syntara-plugin build
syntara-plugin inspect ./dist/my-plugin.oci.tar
```

`run` executes custom-runner Steps; the `create-issue` target lives in the
[python-steps example](../examples/python-steps), so run that line from a
checkout with `--manifest examples/python-steps/plugin.yaml`. See
[Running locally](running-locally.md) for the full workflow.

The default build has no registry side effects. It compiles the selected manifest, validates the contracts, and writes a deterministic OCI-layout tar archive. `run` executes one custom-runner Step locally, from source or against a running workload runtime, so you can inspect a real result without a test suite; see [Running locally](running-locally.md).

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

The command verifies the local archive first, uploads its immutable blobs, advances the configured catalog index, and reports immutable references for both. It does not recompile the source. Production registries require HTTPS; HTTP is allowed only for an explicit loopback development registry. Set `publish.updateCatalog: false` (or pass `--no-update-catalog`) only when staging a release that must not yet be discoverable.

## Optional custom-runner image build

For a plugin containing an action with `runtime.kind: custom-runner`, the developer-preview flow can build and push the author-owned image, use the registry-returned digest in the metadata archive, and publish the archive:

```sh
printf '%s' "$REGISTRY_PASSWORD" | syntara-plugin build \
  --with-workload --publish --password-stdin --verbose
```

This performs three OCI publications:

1. Podman pushes the workload image to `registry.workloadRepository` and returns its immutable digest.
2. The SDK publishes the plugin metadata archive to `registry.artifactRepository`.
3. The SDK creates or advances the configured catalog index so Syntara can discover the release.

The feature does not yet prove custom-runner execution in the Execution Plane, secret delivery, signing, or production egress policy. See [ADR 0006](../.sdlc/adrs/0006-deferred-custom-runner-build-ux.md).

## Administer or recover a catalog

Normal publication updates the configured catalog automatically. Use the explicit catalog command only for a serialized administrative update, such as promoting an already-published artifact after review and signing:

```sh
printf '%s' "$REGISTRY_PASSWORD" | syntara-plugin catalog update \
  ./dist/my-plugin.oci.tar \
  --password-stdin --json
```

The command reads the configured catalog channel, verifies it, and either creates generation `1` or appends one new immutable plugin entry. It preserves existing entries and links the new index through `previousIndexDigest`.

An exact retry is a no-op. Catalog expiry does not remove plugin artifacts. To renew an unchanged catalog before expiry, publish an explicit successor generation:

```sh
printf '%s' "$REGISTRY_PASSWORD" | syntara-plugin catalog update \
  ./dist/my-plugin.oci.tar --renew --password-stdin --json
```

This preserves every entry and advances the generation and expiry window without requiring a new plugin release.

### Recover a lost catalog channel

If the mutable catalog tag was deleted or replaced, do not recreate generation `1`: Syntara will correctly reject that replay because it records the digest and generation it last accepted. A catalog publisher must recover from that trusted immutable revision:

```sh
printf '%s' "$REGISTRY_PASSWORD" | syntara-plugin catalog recover \
  ./dist/my-plugin.oci.tar \
  --previous-index-digest sha256:LAST_ACCEPTED_CATALOG_DIGEST \
  --password-stdin --json
```

`catalog recover` fetches that digest directly (not the mutable tag), preserves its entries, adds the supplied release when needed, and publishes a successor generation. The digest must come from Syntara's catalog-source reconciliation record or another durable release record. If the registry has garbage-collected that immutable revision, restore it from the catalog publisher's archived OCI artifact before recovering; the SDK never bypasses the trusted chain.
