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
  --artifact-kind release --password-stdin --cosign-key cosign.key \
  --cosign-verify-key cosign.pub
```

The publisher may omit `--cosign-key` only when its registry and Syntara source
policy allow an unsigned development release. This is a policy decision; the
SDK reports whether each object was signed and does not mark an unsigned
object trusted.

## Mirror a release with ORAS

Registry mirroring is an operational step, not an SDK command. Use ORAS to
copy each release object by its immutable digest. Add `--recursive` to copy
OCI referrers such as Cosign signatures as well. The SDK's
`release-publication.json` lists the immutable references for the workload
images, plugin artifact, RuntimeBinding, and RuntimeBinding index. Use the
release inventory and publication output to select the tags to retain in the
destination registry. The catalog index is reported by the catalog update
command; copy it too when mirroring that catalog source.

ORAS 1.3 supports registry-to-registry copies, OCI layout archives, auth files,
and multiple destination tags. Its `--recursive` option is currently marked
Preview, so pin the ORAS version used by the promotion process and validate it
when upgrading. [ORAS `cp` documentation](https://oras.land/docs/commands/oras_cp/)

When both registries are reachable, copy each object directly. Replace the
references and tags with values from the publication receipt and release
inventory; repeat for every workload image, plugin artifact, binding, and
index. Include the discovery tags used by the target environment:

```sh
SOURCE_AUTH="$HOME/.config/containers/source-auth.json"
DEST_AUTH="$HOME/.config/containers/destination-auth.json"
SOURCE_REF="registry.example.com/syntara-admin/plugins/http-request@sha256:<digest>"
DEST_REPO="registry-mirror.example.com/syntara-admin/plugins/http-request"

oras cp --recursive \
  --from-registry-config "$SOURCE_AUTH" \
  --to-registry-config "$DEST_AUTH" \
  "$SOURCE_REF" \
  "$DEST_REPO:sha256-<digest-hex>,<release-tag>"
```

For a disconnected environment, copy each object into a local OCI layout on the
connected side, then carry the layout archive and a small mapping of each
layout tag to its destination repository and tags:

```sh
LAYOUT=./release-layout
LAYOUT_TAG=plugin-<digest-hex>
mkdir -p "$LAYOUT"

oras cp --recursive \
  --from-registry-config "$SOURCE_AUTH" \
  --to-oci-layout \
  "$SOURCE_REF" \
  "$LAYOUT:$LAYOUT_TAG"

tar -cf release-transfer.tar -C "$LAYOUT" .
```

On the disconnected side, repeat the import for each saved mapping, using the
recorded layout tag, repository, and destination tags:

```sh
oras cp --recursive \
  --from-oci-layout \
  --to-registry-config "$DEST_AUTH" \
  "release-transfer.tar:$LAYOUT_TAG" \
  "$DEST_REPO:sha256-<digest-hex>,<release-tag>"
```

Resolve a destination tag to confirm it still names the expected digest, then
verify signatures with the publisher's Cosign key and the platform's normal
trust policy:

```sh
oras resolve --registry-config "$DEST_AUTH" \
  "$DEST_REPO:sha256-<digest-hex>"
cosign verify --key cosign.pub "$DEST_REPO@sha256:<digest>"
```

Cosign verification may also check the repository identity recorded in the
signature. If the mirror changes that identity, use the platform's approved
promotion trust policy or sign the mirrored reference; do not disable identity
checks just to make verification pass.

Copying does not rewrite artifact content. In particular, image references in
a RuntimeBinding retain the source registry hostname. Configure the mirror so
those references resolve to the copied images, or retain the same registry
address. Changing an image reference changes the RuntimeBinding digest; that
requires publishing a new binding and advancing its binding index.

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

When publishing a standalone RuntimeBinding archive, supply the exact plugin
archive it names so the SDK can verify the plugin digest, descriptor, and
contract before it publishes the binding or advances the index:

```sh
printf '%s' "$REGISTRY_PASSWORD" | syntara-plugin publish \
  dist/runtime-bindings.oci.tar --artifact-kind runtime-binding \
  --plugin-artifact-archive dist/report-tools.oci.tar \
  --password-stdin --cosign-key cosign.key
```

Before extending an existing binding index, the SDK requires a trust anchor:
either `--cosign-verify-key` for the current index signature or
`--expected-binding-index-digest` from a trusted release record. For local
development only, `--allow-unverified-binding-index` explicitly opts out of
that check. `--cosign-key` signs the new objects; it is not the public key used
to verify the existing index. The first index has no predecessor to verify.

Registry write permissions authorize publishing the binding and changing its
mutable index tag. Keep each plugin in its own artifact repository and scope
release credentials to that repository. The SDK verifies the binding against
the exact plugin archive, but per-publisher authorization remains the registry's
responsibility.

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
