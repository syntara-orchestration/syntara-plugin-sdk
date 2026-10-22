# Getting started

This guide takes a plugin author from installation to a locally verified OCI
plugin archive. It does not require a registry, credentials, Podman, or a
running Syntara instance.

## Install

Install the released CLI or prepare an unreleased checkout by following
[installation](installation.md), then confirm `syntara-plugin --help` works.

## Create your first plugin

```sh
syntara-plugin init ./my-plugin --namespace acme --fields title
cd ./my-plugin
```

Pass a comma-separated list to `--fields` for optional input fields. For more
control, pass an inline JSON schema, a JSON field map, or a path to a JSON
schema file. Field maps support per-field types, descriptions, defaults,
enums, patterns, and requiredness. Add `"required": true` to a field-map
entry when the action needs that input in addition to the built-in
`resource` field.

For example:

```sh
syntara-plugin init ./my-plugin --namespace acme \
  --fields '{"url":{"type":"string","description":"Target URL","minLength":1,"maxLength":50}}'
```

To make `url` required, use:

```sh
syntara-plugin init ./my-plugin --namespace acme \
  --fields '{"url":{"type":"string","description":"Target URL","minLength":1,"maxLength":50,"required":true}}'
```

The generated workspace contains:

```text
my-plugin/
├── plugin.yaml                 # plugin inventory
├── syntara-plugin.yaml         # non-secret build defaults
├── steps/request/manifest.yaml # one action declaration
└── schemas/request.input.yaml  # the action input schema
```

The manifest selects the files that form the release. The settings file does
not duplicate that inventory; it only supplies build and release defaults.

## Validate, inspect, and build

```sh
syntara-plugin validate --settings syntara-plugin.yaml
syntara-plugin inspect --settings syntara-plugin.yaml
syntara-plugin build --settings syntara-plugin.yaml
syntara-plugin inspect ./dist/my-plugin.oci.tar
```

`validate` checks the YAML documents against the versioned contracts and
cross-document rules. `inspect` shows the canonical descriptor. `build` writes
a deterministic metadata OCI archive. The final `inspect` verifies the archive
before displaying it.

## Learn from a complete example

[`../examples/hello-world`](../examples/hello-world) is intentionally small:
one plugin, one `platform-runner` action, a JSON Schema input asset,
documentation, and build settings.

```sh
syntara-plugin validate ../examples/hello-world/plugin.yaml \
  --settings ../examples/hello-world/syntara-plugin.yaml
syntara-plugin build ../examples/hello-world/plugin.yaml \
  --settings ../examples/hello-world/syntara-plugin.yaml
```

## Next steps

- Read the [plugin language reference](plugin-language.md) before changing a manifest.
- Read the [build settings reference](build-settings.md) before configuring a registry or workload image.
- Use the [user guide](user-guide.md) when you are ready to publish an artifact or update a catalog.
