# Hello World

This plugin contributes one credentialless `platform-runner` action: **Say
hello**. Its manifest shows the smallest useful plugin shape:

- [`plugin.yaml`](../plugin.yaml) is the release inventory.
- [`steps/hello/manifest.yaml`](../steps/hello/manifest.yaml) defines one
  action and its bounded HTTP operation.
- [`schemas/hello.input.yaml`](../schemas/hello.input.yaml)
  defines the action input.
- [`syntara-plugin.yaml`](../syntara-plugin.yaml) supplies local build output.

The implementation reference is an example placeholder: replace it with a
registered HTTP implementation available in your installation. The path is
intentionally relative. Syntara selects the endpoint,
connection, authentication, and network policy at installation and execution
time; this example does not call a real public service by itself.

Validate and build from this directory:

```sh
syntara-plugin validate --settings syntara-plugin.yaml
syntara-plugin build --settings syntara-plugin.yaml
syntara-plugin inspect dist/hello-world.oci.tar
```
