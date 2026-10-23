# SDK examples

Examples are small complete plugin workspaces. They are intentionally safe to
validate and package without a registry, a container engine, or credentials.

| Example | What it demonstrates |
| --- | --- |
| [Hello World](hello-world) | One credentialless `platform-runner` action, an asset-backed input schema, documentation, and a non-secret build and release settings template. |
| [Existing Step](existing-step) | One checked-in plugin with Action, Control, and Trigger custom runners, manifest entry-point loading, direct/container equivalence, and the opt-in OCI runtime harness. |

Start with [Hello World](hello-world). Its directory is also the command
working directory:

```sh
cd examples/hello-world
syntara-plugin validate --settings syntara-plugin.yaml
syntara-plugin build --settings syntara-plugin.yaml
syntara-plugin inspect dist/hello-world.oci.tar
```

The Hello World [settings file](hello-world/syntara-plugin.yaml) also shows
the registry, publication, and catalog keys. Replace its example registry
values before using it to publish; it intentionally contains no credential.

The [Existing Step](existing-step) prototype runs without a container engine
for local development:

```sh
uv run --no-sync pytest examples/existing-step/tests -q
```

Its checked-in `Containerfile` is used by the repository's opt-in Podman
integration test; see that example's README for the execution-plane boundary.
