# Build settings reference

`syntara-plugin.yaml` is a versioned, non-secret configuration document for one plugin root. Its normative schema is [`plugin-build-settings.schema.json`](../contracts/schemas/v1alpha1/plugin-build-settings.schema.json).

```yaml
apiVersion: syntara.io/v1alpha1
kind: PluginBuildSettings
manifest: plugin.yaml
output:
  verbose: false
  json: false
registry:
  origin: https://registry.example.test
  artifactRepository: acme/plugins/example
  workloadRepository: acme/workloads/example
  catalogRepository: acme/catalog-index
  username: publisher
  allowInsecureLoopbackHttp: false
build:
  artifactOutput: dist/example.oci.tar
  imageBindings: {}
  workload:
    engine: podman
    context: .
    containerfile: Containerfile
    tag: 0.1.0
    platform: linux/amd64
    imageId: example-workload
publish:
  channel: 0.1.0
catalog:
  sourceId: production-catalog
  channel: stable
  expiresInHours: 168
```

## Document envelope

| Key | Meaning |
| --- | --- |
| `apiVersion` | Required settings contract version. Current value: `syntara.io/v1alpha1`. |
| `kind` | Required document discriminator. Current value: `PluginBuildSettings`. |
| `manifest` | Optional path to `plugin.yaml`. It lets commands run from the plugin root without repeating a manifest argument. |

## `output`

| Key | Meaning |
| --- | --- |
| `verbose` | Optional default for verbose progress on commands that support `--verbose`. |
| `json` | Optional default for machine-readable JSON output on `validate`, `inspect`, `build`, `publish`, and `catalog update`. |

## `registry`

| Key | Meaning |
| --- | --- |
| `origin` | Registry URL including scheme and optional port. |
| `artifactRepository` | Repository for the plugin metadata OCI artifact. This becomes the catalog entry’s `artifactRepository`. |
| `workloadRepository` | Repository for an author-owned custom-workload image. |
| `catalogRepository` | Repository for the catalog-index OCI artifact. |
| `username` | Registry username. Passwords never belong in this file. |
| `authFile` | Local path to a Docker- or Podman-compatible `auth.json` file with an inline Basic credential. The path may be relative to the settings file; the credential is read only at publication time. |
| `allowInsecureLoopbackHttp` | Allows HTTP only for `localhost` or `127.0.0.1` development registries. |

## `build`

| Key | Meaning |
| --- | --- |
| `artifactOutput` | Output path for the plugin metadata `.oci.tar` archive. |
| `imageBindings` | Optional mapping from a manifest workload image ID to an existing immutable `repository@sha256:...` image reference. |
| `workload` | Optional build target used only with `syntara-plugin build --with-workload`. |

### `build.workload`

| Key | Meaning |
| --- | --- |
| `engine` | Container engine. The current SDK supports only `podman`. |
| `context` | Container build context directory. Use `.` for a self-contained plugin; the Containerfile can copy only files inside this directory. |
| `containerfile` | Containerfile path. |
| `tag` | Temporary mutable tag Podman pushes to obtain an immutable registry digest. |
| `platform` | Explicit target platform, such as `linux/amd64` or `linux/arm64`. |
| `imageId` | Logical workload image ID declared by `plugin.yaml`. |

## `publish` and `catalog`

| Key | Meaning |
| --- | --- |
| `publish.artifact` | Optional existing archive path for `publish` or `catalog update`. |
| `publish.channel` | OCI tag used when publishing the plugin metadata artifact. |
| `catalog.sourceId` | Stable Syntara catalog-source identity. |
| `catalog.channel` | OCI tag of the catalog index, commonly `stable`. |
| `catalog.expiresInHours` | Lifetime assigned to a newly published catalog index. It does not delete plugin artifacts when it expires. |

Unknown keys fail validation. The unreleased contract deliberately has no legacy aliases: use `artifactOutput`, not `output`, and `artifactRepository`, not `pluginRepository`.
