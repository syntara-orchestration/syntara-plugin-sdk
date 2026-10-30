# SDK examples

Examples are small complete plugin workspaces. They are intentionally safe to
validate and package without a registry, a container engine, or credentials.

| Example | What it demonstrates |
| --- | --- |
| [Hello World](hello-world) | One credentialless `platform-runner` action, an asset-backed input schema, documentation, and a non-secret build and release settings template. |
| [Existing Step](existing-step) | One checked-in plugin with Action, Control, and Trigger custom runners, manifest entry-point loading, direct/container equivalence, and the opt-in OCI runtime harness. |
| [Python steps](python-steps) | One `custom-runner` step per Syntara kind (an Action and a Trigger with source manifests), tested on both execution paths and used as the `syntara-plugin run` demo workspace. |
| [Subworkflow composition](subworkflow-composition) | A parent-side `Control` that invokes a named child workflow and the child-side `Trigger` that starts it, with manifest validation, seam checks between the child's steps, and the parent surfacing the child's declared result. |

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

The [Existing Step](existing-step) prototype's end-to-end suite runs without a
container engine and lives with the SDK's integration tests:

```sh
uv run --no-sync pytest sdks/python/tests/integration/test_existing_step_e2e.py -q
```

Its checked-in `Containerfile` is used by the repository's opt-in Podman
integration test; see that example's README for the execution-plane boundary.
