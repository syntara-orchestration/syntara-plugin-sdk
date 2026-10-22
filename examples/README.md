# SDK examples

Examples are small complete plugin workspaces. They are intentionally safe to
validate and package without a registry, a container engine, or credentials.

| Example | What it demonstrates |
| --- | --- |
| [Hello World](hello-world) | One credentialless `platform-runner` action, an asset-backed input schema, documentation, and a non-secret build and release settings template. |
| [Python steps](python-steps) | One `custom-runner` step per Syntara kind (an Action and a Trigger with source manifests), tested on both execution paths and used as the `syntara-plugin run` demo workspace. |

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
