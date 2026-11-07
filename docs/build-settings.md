# Build settings reference

`syntara-plugin.yaml` is a versioned, non-secret configuration document for one
plugin workspace. Its normative schema is
[`plugin-build-settings.schema.json`](../contracts/schemas/v1alpha1/plugin-build-settings.schema.json).
See the [RuntimeBindings guide](runtime-bindings.md) for the full multi-image
example and release commands.

```yaml
apiVersion: syntara.io/v1alpha1
kind: PluginBuildSettings
manifest: plugin.yaml
output:
  verbose: false
  json: false
registry:
  origin: https://quay.io
  artifactRepository: acme/report-tools
  workloadRepository: acme/report-tools-runtime
  catalogRepository: acme/catalog-index
  username: publisher
build:
  artifactOutput: dist/report-tools.oci.tar
  runtimeBindings: runtime-bindings.yaml
  workloads:
    python-base-image:
      engine: podman
      context: .
      containerfile: runtime/python-base/Containerfile
      repository: quay.io/acme/report-tools-runtime
      tag: 1.0.0-python-base
      platform: linux/amd64
    python-pdf-image:
      engine: podman
      context: .
      containerfile: runtime/python-pdf/Containerfile
      repository: quay.io/acme/report-tools-runtime
      tag: 1.0.0-python-pdf
      platform: linux/amd64
publish:
  channel: 1.0.0
  updateCatalog: true
catalog:
  sourceId: acme-plugins
  channel: stable
  expiresInHours: 168
```

## Document envelope and lookup

| Key | Meaning |
| --- | --- |
| `apiVersion` | Required settings contract version, currently `syntara.io/v1alpha1`. |
| `kind` | Required document discriminator, `PluginBuildSettings`. |
| `manifest` | Optional path to `plugin.yaml`. Relative paths are resolved from this settings file. |

The SDK selects one settings file in this order: explicit `--settings`, a file
beside the selected plugin manifest, then a file in the current directory. It
does not merge files. Command-line arguments override values from the selected
file. Relative paths are resolved from the settings file.

## `output`

| Key | Meaning |
| --- | --- |
| `verbose` | Default verbose progress for commands that support it. An explicit `--verbose` option takes precedence. |
| `json` | Default machine-readable output for commands that support it. An explicit `--json` option takes precedence. |

## `registry`

| Key | Meaning |
| --- | --- |
| `origin` | Registry URL, including scheme and optional port. |
| `artifactRepository` | Repository for plugin metadata artifacts and RuntimeBindings. |
| `workloadRepository` | Default repository for custom-runner images. A workload build may set its own `repository`. |
| `catalogRepository` | Repository containing the separate plugin catalog index. |
| `username` | Registry username. Passwords and tokens do not belong in this file. |
| `authFile` | Local path to Docker/Podman `auth.json`; credentials are read only for the publication command. |
| `allowInsecureLoopbackHttp` | Allows HTTP only for an explicit local development registry. |

## `build`

| Key | Meaning |
| --- | --- |
| `artifactOutput` | Path for the plugin artifact OCI archive. |
| `runtimeBindings` | Path to the author-maintained source `RuntimeBinding` manifest. |
| `runtimeBindingOutput` | Optional path for the built RuntimeBinding OCI archive. |
| `imageBindings` | Existing immutable image references, keyed by logical image ID. Each value must be `repository@sha256:digest`. |
| `workloads` | Container build settings keyed by the `buildTarget` identifiers used in `runtime-bindings.yaml`. |

Each `build.workloads.<buildTarget>` entry accepts:

| Key | Meaning |
| --- | --- |
| `engine` | Container engine; the current SDK supports `podman`. |
| `context` | Container build context. The Containerfile can only copy files inside this directory. |
| `containerfile` | Path to the Containerfile within the build context. |
| `repository` | Optional destination repository; defaults to `registry.workloadRepository`. |
| `tag` | Local and temporary registry tag. The release identity is the resulting digest. |
| `platform` | Target OS and architecture, such as `linux/amd64`. |

For image-only updates, the SDK uses the exact plugin OCI archive supplied to
`build --plugin-artifact-archive`. If the source binding manifest still names
`plugin.yaml`, no digest edit is needed there.

## `publish` and `catalog`

| Key | Meaning |
| --- | --- |
| `publish.artifact` | Optional path to a prepared plugin artifact archive. |
| `publish.channel` | OCI tag used when publishing a new plugin artifact. |
| `publish.updateCatalog` | Whether a new plugin release also advances the plugin catalog index. It defaults to `true`; image-only binding updates do not change the plugin catalog. |
| `catalog.sourceId` | Stable catalog-source identity configured in Syntara. |
| `catalog.channel` | OCI tag for the catalog index, commonly `stable`. |
| `catalog.expiresInHours` | Validity period for a new catalog or RuntimeBinding index. Expiry does not delete artifacts. |

Registry passwords, signing keys, and secret values must not be stored in
`syntara-plugin.yaml`. Use `--password-stdin` or a protected registry auth file
for publication, and pass signing keys explicitly to the command.

Unknown keys fail validation. The schemas in the contracts bundle define the
supported fields; do not add environment-specific values to this file.
