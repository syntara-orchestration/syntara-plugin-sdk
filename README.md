# Syntara Plugin SDK

Build, validate, package, and publish Syntara plugins. A plugin is a
versioned declaration of Step types in the action, control, and trigger categories,
integration types,
credential recipes, documentation, and—when needed—one custom runner image.

The SDK gives plugin authors one public command: `syntara-plugin`.

## Start here

The SDK is currently developed from source. Clone this repository and install
its two Python packages in editable mode:

```sh
git clone https://github.com/syntara-orchestration/syntara-plugin-sdk.git
cd syntara-plugin-sdk
python -m venv .venv
source .venv/bin/activate
python -m pip install \
  --editable ./sdks/python/packages/contracts \
  --editable ./sdks/python/packages/sdk
```

> **Note:** When the SDK is published, install the released package instead:
>
> ```sh
> python -m pip install syntara-plugin-sdk
> ```

With the environment active, create, validate, inspect, and package a plugin
without contacting a registry:

```sh
syntara-plugin init ./my-plugin --namespace acme
cd ./my-plugin
syntara-plugin validate
syntara-plugin inspect
syntara-plugin build
syntara-plugin inspect ./dist/my-plugin.oci.tar
```

`init` generates a platform-runner action, which Syntara's managed runner
executes; the `run` command executes custom-runner Steps locally. To try it,
run the custom-runner example from the repository root (see
[Running locally](docs/running-locally.md)):

```sh
syntara-plugin run --manifest examples/python-steps/plugin.yaml create-issue \
  --input '{"title": "Broken build"}'
```

`init` creates a small plugin workspace and a non-secret
`syntara-plugin.yaml` settings file. The default build is deterministic and
offline: it writes a portable OCI metadata archive but does not build an image,
publish, install, or execute anything.

## Publish a release

After inspecting a successful archive, configure the non-secret `registry`,
`publish`, and `catalog` values in your plugin's `syntara-plugin.yaml`. The
[Hello World settings file](examples/hello-world/syntara-plugin.yaml) is a
complete template; replace its `registry.example.test` and `example/...`
values for your registry. Do not store a password or token in the file.

Publish the archive you already verified. For CI, pass the registry password
only over standard input:

```sh
printf '%s' "$REGISTRY_PASSWORD" | syntara-plugin publish \
  ./dist/my-plugin.oci.tar \
  --password-stdin
```

By default, `publish` uploads the immutable plugin artifact and then creates
or advances the configured catalog index, making the release discoverable.
Set `publish.updateCatalog: false` only for a staged release that must not yet
be advertised. `catalog update` remains available for release administration
and `catalog recover --previous-index-digest ...` repairs a deleted or replaced
mutable catalog tag from the exact digest Syntara last accepted. This preserves
rollback protection rather than restarting at generation one.
For an Action Step type using an author-owned container, use
`syntara-plugin build --with-workload --publish` after configuring the
`build.workload` settings; see the [user guide](docs/user-guide.md).

For local development, `registry.authFile` can point to a local Docker- or
Podman-compatible `auth.json` file. It reads one matching inline Basic
credential for that invocation; the credential itself never appears in
`syntara-plugin.yaml`.

For a ready-to-read workspace, see [Hello World](examples/hello-world/).
Run it directly from a source checkout:

```sh
syntara-plugin validate examples/hello-world/plugin.yaml \
  --settings examples/hello-world/syntara-plugin.yaml
```

## Documentation

- [Installation](docs/installation.md) — install a released SDK or prepare a source checkout.
- [Getting started](docs/getting-started.md) — first plugin and the four core commands.
- [Step authoring and composition guide](docs/authoring-guide.md): the end-to-end authoring workflow for Action, Control, and Trigger Steps, and composition best practices.
- [User guide](docs/user-guide.md) — settings, custom runners, artifact publication, and catalog release.
- [Plugin language reference](docs/plugin-language.md) — each manifest kind and field, with links to its normative schema.
- [Build settings reference](docs/build-settings.md) — every `syntara-plugin.yaml` key and its behavior.
- [Testing reference](docs/testing.md) — local pytest harness, fixtures, invocation helpers, and generated tests.
- [Running locally](docs/running-locally.md) — validate inputs and run one Step from source or a running image.
- [Developer guide](docs/developer-guide.md) — local setup, tests, contract changes, and contribution expectations.
- [Runtime and OCI model](docs/runtime-and-oci.md) — container ABI, artifact boundaries, and ownership.
- [Documentation index](docs/README.md) — the complete map, including design records and language-specific notes.

The normative `syntara.io/v1alpha1` schemas and conformance fixtures live in
[contracts/](contracts/). Every SDK must package and honor those contracts;
language implementations do not maintain private schema copies.

## Contributing

The repository contains language-neutral contracts plus the Python SDK and
contracts distribution. The shared contracts distribution provides the
contract and runtime APIs used by both the SDK and Execution Plane:

```text
contracts/                    # normative JSON Schema, fixtures, and protobuf ABI
examples/                     # small author-facing plugin workspaces
docs/                         # guides and language reference
sdks/python/packages/sdk/     # syntara-plugin-sdk and CLI
sdks/python/packages/contracts/ # shared contracts and runtime APIs
sdks/python/tests/            # Python tests
```

To work from a checkout, follow the [developer guide](docs/developer-guide.md).
In short: create the workspace environment, run `python -m pytest`, keep
schemas and fixtures synchronized, and add a runnable example when a user
facing workflow changes.

## Current scope

The SDK supports offline plugin packaging, bounded OCI publication with
default catalog publication, catalog recovery, and a developer-preview Podman workload build.
Artifact signing, catalog refresh automation, and end-to-end Execution Plane
delivery of credentials to custom runners remain follow-on work. See
[ADR 0006](.sdlc/adrs/0006-deferred-custom-runner-build-ux.md) for the
custom-runner preview boundary.
