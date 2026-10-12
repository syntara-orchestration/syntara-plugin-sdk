# Runtime and OCI model

This page distinguishes the pieces of a plugin release. It is useful when you
need a custom workload; ordinary platform-runner actions need only the plugin
artifact.

## Release boundaries

```text
plugin.yaml + target manifests + schemas + Markdown
                  |
                  | syntara-plugin build
                  v
          plugin metadata OCI artifact
                  |
                  | syntara-plugin publish
                  v
       registry artifact repository / catalog index

custom-workload source + Containerfile
                  |
                  | syntara-plugin build --with-workload --publish
                  v
        immutable workload image digest
                  |
                  | bound into metadata artifact
                  v
       Execution Plane pulls the approved digest
```

The artifact is the declarative, inspectable plugin release. A workload image
is a separate OCI image containing author-owned executable code. The compiler
records a custom-workload action's logical image ID and binds it to an immutable
`repository@sha256:...` reference. The Execution Plane uses that resolved
reference, never a mutable tag.

For `platform-runner` actions, Syntara owns the runner image, endpoint policy,
and connection delivery. The plugin declares only an approved driver and its
bounded operation contract.

## Container ABI

The in-container plugin protocol is defined by
[`container.proto`](../contracts/proto/syntara_plugin/runtime/protocol/container.proto).
The corresponding runtime work-item JSON shape is in
[`runtime.schema.json`](../contracts/schemas/v1alpha1/runtime.schema.json).
The Python helper implementation is in
[`container.py`](../sdks/python/packages/runtime/src/syntara_plugin/runtime/container.py).

The ABI is the boundary between an Execution Plane launcher and an author
workload. It carries action inputs, output/error contracts, and approved file
paths. It does not carry raw credentials in workflow JSON, environment
variables, or command-line arguments.

Credential-bearing custom workload execution is intentionally deferred in the
current local proof of concept. The ABI reserves a map of credential-file paths:
the eventual Execution Plane will redeem an opaque, short-lived credential
handle, write a secret into an isolated per-invocation in-memory mount, make
that mount read-only in the workload container, and send only the file path to
the plugin. The plugin never receives broker credentials or a Kubernetes API
identity.

## Registry and catalog model

The artifact and workload repositories may be mirrored for disconnected or
air-gapped installations. During installation, the Control Plane records the
plugin's immutable artifact identity and the deployment-resolved pull
reference. The registry administrator controls mirroring; the plugin author
does not hard-code a deployment mirror in the manifest.

Catalogs are discovery indexes, not the source of truth for an artifact.
`syntara-plugin publish` creates or updates the configured index by default;
set `publish.updateCatalog: false` only for a staged release. Use
`syntara-plugin catalog update` for release administration. If its mutable tag is lost, use
`syntara-plugin catalog recover` with Syntara's last accepted immutable index
digest; this continues the trusted chain instead of replaying generation one.
See the [user guide](user-guide.md) for the operational commands and [build
settings reference](build-settings.md) for catalog defaults.
