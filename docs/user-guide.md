# User guide

Use this guide after completing [getting started](getting-started.md). It describes the release path; the [plugin language reference](plugin-language.md) describes the YAML documents themselves.

## The normal authoring loop

```text
edit plugin source → validate → inspect → run locally → build archive → inspect archive
```

The first three steps run inside your plugin workspace:

```sh
cd my-plugin
syntara-plugin validate
syntara-plugin inspect
syntara-plugin build
syntara-plugin inspect ./dist/my-plugin.oci.tar
```

`run` is separate from that loop for one reason: it executes custom-runner
Steps, and a workspace created by `init` declares a platform-runner action,
which Syntara's managed runner executes instead. To run a Step locally you
need a workspace that declares a custom-runner target and an input file; the
complete, copy-pasteable workflow using the
[python-steps example](../examples/python-steps) from a repository checkout:

```sh
# 1. Print the input contract of the target you want (nothing executes).
syntara-plugin run --manifest examples/python-steps/plugin.yaml create-issue

# 2. Save the printed template as your input file and fill in real values.
syntara-plugin run --manifest examples/python-steps/plugin.yaml create-issue > input.json
$EDITOR input.json

# 3. Execute with the filled file.
syntara-plugin run --manifest examples/python-steps/plugin.yaml create-issue --input-file input.json
```

Every command above runs as printed from the repository root. `run` can also
execute a workload image you built; see [Running locally](running-locally.md)
for both modes, target selection, and the meaning of every output field.

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

Before the image is pushed, the SDK adds a generated registration manifest at
`/opt/syntara/registration-manifest.json`. It maps each custom-runner step
revision to its entry point and ABI. Image bindings supplied for an externally
built image are not inspected by the SDK; the image producer must embed the
matching manifest. See the [plugin language reference](plugin-language.md) and
the [runtime and OCI model](runtime-and-oci.md).

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
