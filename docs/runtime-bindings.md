# Runtime bindings and independent workload releases

This guide describes the multi-image release flow for custom-runner plugins. It
keeps the plugin contract, the image selection, and each container image as
separate objects so an image can be rebuilt for a security fix without
publishing a new plugin version.

## The three release objects

| Object | What it contains | Identity and update rule |
| --- | --- | --- |
| Plugin artifact (P) | Plugin metadata, step definitions, schemas, and documentation. A custom workload is named by a logical ID. | Immutable OCI digest. Publish a new P when the plugin contract changes. |
| RuntimeBinding artifact (B) | The exact P digest plus one or more named image options for each custom workload. | Immutable OCI digest. Build a new B when an option or image digest changes. |
| Workload image (I) | The executable container image for a workload option. | Immutable OCI image digest. Rebuild and publish I independently. |

The SDK publishes images first, then P (when this is a new plugin release),
then B, then the per-plugin RuntimeBinding index. When Cosign signing is
configured, it signs each published object, including the index. For a new P,
it can update the separate plugin catalog index after those objects have
succeeded. An image-only update publishes a new I, B, and RuntimeBinding index;
P and the plugin catalog entry stay unchanged.

## Author the manifests

`plugin.yaml` declares logical workloads and assigns custom-runner step types
to them. It does not contain a registry tag or image digest:

```yaml
apiVersion: syntara.io/v1alpha1
kind: Plugin
metadata:
  namespace: acme
  name: report-tools
  version: 1.0.0
  displayName: Report tools
  description: Creates and processes reports.
spec:
  documentation:
    path: docs/README.md
  workloads:
    python:
      image: python-runtime
  targets:
    - actions/create-report.yaml
```

An action refers to the logical workload ID:

```yaml
spec:
  runtime:
    kind: custom-runner
    workload: python
    entryPoint: report_tools.actions:create_report
```

`runtime-bindings.yaml` is an author-maintained source manifest. It lists the
image choices presented to workflow authors. Each option uses either a
`buildTarget` that the SDK builds locally or an existing immutable image
`reference`:

```yaml
apiVersion: syntara.io/v1alpha1
kind: RuntimeBinding
metadata:
  namespace: acme
  name: report-tools-runtime
spec:
  plugin:
    source: plugin.yaml
  workloads:
    python:
      default: python-base
      options:
        - name: python-base
          displayName: Python base
          description: General-purpose report runtime.
          image:
            buildTarget: python-base-image
        - name: python-pdf
          displayName: Python with PDF tools
          description: Includes libraries for creating and processing PDFs.
          image:
            buildTarget: python-pdf-image
```

Keep non-secret container build settings in `syntara-plugin.yaml`:

```yaml
registry:
  origin: https://quay.io
  artifactRepository: acme/report-tools
  workloadRepository: acme/report-tools-runtime
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

## First plugin release

Scaffold the custom-workload layout, then validate it and try the source
implementation before building the container:

```sh
syntara-plugin init ./report-tools --namespace acme --template custom-workload
cd report-tools
syntara-plugin validate plugin.yaml --runtime-bindings runtime-bindings.yaml
syntara-plugin run create-report --mode source --input '{"title":"Quarterly report"}'
```

Build creates local images, the plugin archive, the resolved binding manifest,
the binding archive, and `dist/release.json`. Release-inventory archive paths
are relative to `dist`, so the complete directory can be moved to another
publisher machine. The SDK requires release archives to stay under `dist` and
rejects symlinked or parent-traversing archive paths. Build does not publish to
a registry. Inspect the release files, then run the selected option through the
container gRPC runtime:

```sh
syntara-plugin build plugin.yaml \
  --runtime-bindings runtime-bindings.yaml \
  --output dist/report-tools.oci.tar
syntara-plugin inspect dist/release.json
syntara-plugin inspect dist/report-tools.oci.tar
syntara-plugin inspect dist/runtime-bindings.yaml
syntara-plugin run create-report --mode image --image-option python-pdf \
  --release dist/release.json --input '{"title":"Quarterly report"}'
```

Workload archive filenames include the full image digest, and the default
RuntimeBinding archive filename includes its full artifact digest. Rebuilding
in the same workspace keeps older immutable outputs and adds new ones. The
generated `dist/release.json` is the latest build's inventory and is refreshed
by a later successful build. A plugin archive path is never replaced with
different bytes: when its contract changes, choose a new `--output` (or
`build.artifactOutput`) path. An identical rebuild can reuse the existing
archive.

Publish the prepared release inventory. The CLI verifies the local files and
digests, publishes in dependency order, and prints the immutable references:

```sh
printf '%s' "$REGISTRY_PASSWORD" | syntara-plugin publish dist/release.json \
  --artifact-kind release --password-stdin --cosign-key cosign.key
```

The publisher may omit `--cosign-key` only when its registry and Syntara source
policy allow an unsigned development release. This is a policy decision; the
SDK reports whether each object was signed and does not mark an unsigned
object trusted.

## Transfer a signed release to a disconnected registry

The release publication receipt records each immutable subject digest and the
registry tags used for discovery. Use `transfer export` to copy those subjects
and their attached OCI referrers, including Cosign signatures, into an OCI
layout archive. `transfer import` copies the same objects and referrers into a
disconnected registry, restores the recorded tags, and checks that every
subject manifest digest is unchanged. Keep the receipt with the archive.

These commands require the ORAS CLI (1.3 or newer). ORAS performs recursive
referrer copying; the `--recursive` option is currently marked preview in its
CLI documentation, so pin the ORAS version in the release process and exercise
the transfer path when upgrading it. Pass Docker- or Podman-compatible auth
files explicitly when either registry requires authentication; the SDK does
not store registry credentials in the release inventory.

Export from the source registry:

```sh
syntara-plugin transfer export dist/release-publication.json \
  --registry-origin https://registry.example.com \
  --registry-auth-file ~/.config/containers/auth.json \
  --output dist/release-transfer.oci.tar
```

Copy both `release-transfer.oci.tar` and `release-publication.json` to the
disconnected environment, then import using the destination registry origin.
Repository paths remain the same. The registry hostname can change for the
copy operation:

```sh
syntara-plugin transfer import dist/release-transfer.oci.tar \
  dist/release-publication.json \
  --registry-origin https://registry.airgap.example.com \
  --registry-auth-file ~/.config/containers/auth.json
```

The importer verifies the subject digest after writing each recorded tag. For
signed subjects, also verify them against the destination registry with the
publisher's Cosign public key before enabling that registry as a trusted
source. Transfer preserves signatures; it does not make a signature trusted.
The import can partially complete if the destination registry fails midway;
rerun it with the same receipt to finish the remaining copies.

The transfer is byte preserving: it does not rewrite references inside an
artifact. In particular, image references in a RuntimeBinding still name the
source registry host. The destination must make those references resolve to
the copied images, for example by retaining the same registry address or
configuring a trusted registry mirror for the workload runtime. Rewriting an
image reference changes the RuntimeBinding digest; publish and sign a new
RuntimeBinding and advance its binding index if a new reference is required.

## Image-only security update

Keep P unchanged. Rebuild an image option, or add a new option in the source
binding manifest, then build a new B against the exact existing plugin archive.
When `runtime-bindings.yaml` still points to `plugin.yaml`, the archive
supplies the plugin digest; the source manifest does not need a digest edit:

```sh
syntara-plugin build \
  --runtime-bindings runtime-bindings.yaml \
  --plugin-artifact-archive dist/report-tools.oci.tar
syntara-plugin inspect dist/release.json
printf '%s' "$REGISTRY_PASSWORD" | syntara-plugin publish dist/release.json \
  --artifact-kind release --password-stdin --cosign-key cosign.key
```

The resulting release inventory marks P as already published and publishes
only the changed I, new B, and successor RuntimeBinding index. The SDK does not
update the plugin catalog index for this flow because the plugin identity and
step contracts have not changed. Syntara must still enforce its configured
trust and installation policy before exposing the new option.

## Index expiry and concurrent publishers

An expired RuntimeBinding index remains usable only as a verified historical
predecessor when the SDK prepares a successor. The SDK copies its entries,
increments the generation, records the previous index digest, and gives the
successor a new validity window. Consumers still reject an expired index.
Republishing the same binding after expiry therefore renews the index instead
of treating the old index as an idempotent success.

The mutable per-plugin index tag is updated conditionally. A first publisher
uses `If-None-Match: *`; a successor uses the strong ETag read with its
predecessor in `If-Match`. If another publisher wins first, the registry
returns a conflict and the SDK stops without replacing that index. Rerun the
publish command to read the new predecessor and build a successor that includes
both releases. Registries must return strong ETags and honor conditional
manifest PUT requests; the SDK fails closed when it cannot safely condition an
update.

## Inspect and verify

`inspect` reads source manifests, release inventories, local plugin/Binding
archives, and workload image archives without executing workload code. It
reports the exact OCI identities and the relationships it can verify from
those files. Image archives contain layers; the SDK reports their manifest,
config, and layer descriptors without claiming to inspect arbitrary files
inside the image.

For a remote immutable OCI reference, Cosign verification is explicit:

```sh
syntara-plugin inspect quay.io/acme/report-tools@sha256:DIGEST \
  --verify --cosign-key cosign.pub
```

The verification key is supplied at inspection time. The SDK does not place
private signing material, registry credentials, or secret values in P, B, I,
or the generated release inventory.

## Lifecycle and limits

| Event | New objects | Stable objects |
| --- | --- | --- |
| Plugin schema or step contract changes | New P, then B and index for its workloads | Existing P/B/I remain addressable by digest. |
| Image CVE fix with no contract change | New I, B, and index generation | P and plugin catalog entry stay unchanged. |
| Add or remove an image option | New B and index generation; publish new I only if needed | P stays unchanged. |
| Roll back to an earlier image | Publish a successor index selecting an earlier B/I digest | Historical P/B/I and prior index generations remain immutable. |
| Retire a binding | Publish a successor index marking it withdrawn. | Do not delete registry content while workflows or retained runs may refer to it. |

The SDK provides local build, inspection, publication, and signed index
generation. The Syntara PoC in this work demonstrates catalog installation,
image-option selection, and persistence of exact digests. Production admin
approval, automatic publisher updates, registry garbage collection, and the
Execution Plane's production dispatch integration remain separate work.
