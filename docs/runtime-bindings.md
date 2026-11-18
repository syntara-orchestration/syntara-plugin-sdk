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

The `entryPoint` syntax belongs to the image's language: this Python example
uses the Python adapter's `module:Class` form, and the
[Go and TypeScript examples](authoring-guide.md#implement-in-another-language)
use their own.

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
          runtime:
            components:
              - name: python
                executable: /usr/bin/python3.12
          image:
            buildTarget: python-base-image
        - name: python-pdf
          displayName: Python with PDF tools
          description: Includes libraries for creating and processing PDFs.
          runtime:
            components:
              - name: python
                executable: /usr/bin/python3.12
          image:
            buildTarget: python-pdf-image
```

Each image option declares the runtime components and absolute executable
paths that the SDK can probe. These fields identify probe targets; they do not
duplicate the language or version. The image carries those two values as OCI
annotations named `io.syntara.runtime.language` and
`io.syntara.runtime.version`. The SDK reads them from the exact digest-pinned
image and copies them into the resolved RuntimeBinding. For an image built by
the SDK, provide the values in the corresponding `build.workloads.<target>`
settings entry; Podman writes them onto the final image. A prebuilt `reference`
image must already carry both annotations. If an image is missing either value,
the build fails with guidance to add them.

The annotated language must also match a declared runtime component name. If it
does not, the build fails; the SDK does not treat a missing declaration as an
unsupported runtime.

The SDK starts every exact image option in a local probe with an egress-restricted
internal network and asks its gRPC `Health` endpoint for runtime evidence. The
image supplies that endpoint and its probe helper, so the response is not an
independent trust signal. For Python and GNU Bash, the SDK compares the
workload-reported executable and version with the image annotation. A matching
report is recorded as `probeStatus: matched`. Missing or unsupported evidence,
a different executable, or a version mismatch fails the build for these
supported languages. For a language without a built-in SDK probe, the SDK
records `probeStatus: unsupported`; the default build warns and continues,
while `--require runtime-metadata-match` rejects it. A Health or transport
failure also fails the build.

The SDK probes every image option in the RuntimeBinding, including non-default
options and prebuilt `reference` images. A referenced image may need to be
pulled using the configured registry credentials. `--require` does not enable
probing; it only decides whether unsupported probe evidence is allowed. Publish
repeats the probes for all options before publishing. This costs one image
startup per option at build and again at publish, and ensures that the binding
contains metadata for each selectable image.

`matched` only means the image's Health response reported a supported probe
version that matched its OCI annotation. A publisher-controlled image can
change or falsify its own Health response, so this result is compatibility
metadata, not proof of image integrity, publisher identity, or safety. EP and
other consumers must enforce trust using separate digest, signature, and
publisher policies.

### Alpha migration for SDK and Execution Plane consumers

This is a forward-only change to the pre-release `v1alpha1` contracts. Before
consuming the updated package or artifacts:

- Plugin authors must add a `runtime` declaration, including at least one
  `runtime.components` entry with `name` and absolute `executable`, to each
  RuntimeBinding image option. Rebuild and republish resolved binding artifacts
  with the updated SDK.
- RuntimeBinding consumers must replace `runtime.verification` and component
  `verification` with `probeStatus`; values `verified` and `unverified` become
  `matched` and `unsupported`.
- Authors must rename the `verified-runtime` policy requirement in
  `syntara-plugin.yaml` and on the CLI to `runtime-metadata-match`.
- Workload and host implementers must update the Health `RuntimeComponent`
  protobuf accessor from `verification` to `probe_status` (field number 4 is
  unchanged) and use the new status values.
- The Execution Plane must consume the updated shared contracts package,
  regenerate its protobuf bindings, update any parsing or policy code, and must
  not use `probeStatus` as an image-trust decision. Digest/signature and
  publisher authorization checks remain separate.

The SDK and Execution Plane must agree on the same contracts package version
before they exchange these records. Older `v1alpha1` producers and consumers
are not wire or artifact compatible with this alpha change.

| Image and probe result | Build behavior |
| --- | --- |
| Supported language, matching component, and matching version | Pass; record `probeStatus: matched`. |
| Supported language, but matching component is missing or unsupported | Fail; correct the executable declaration or image. |
| Supported language, but probe version differs from the image annotation | Fail; correct the image or its metadata. |
| No SDK probe for the annotated language, with a matching declared component | Warn and record `probeStatus: unsupported`; `--require runtime-metadata-match` fails. |
| Annotated language has no matching declared component | Fail; declare the correct runtime component. |
| Missing image annotations or failed Health/transport probe | Fail. |

On macOS with Podman, the SDK uses a temporary OpenSSH tunnel through the
Podman machine to query the workload on its internal network; the workload
remains disconnected from external networks.

The image annotation remains the declared version, and the Health response
supplies a second value for the SDK to compare. Because the image controls that
response, the comparison does not independently confirm the executable's
identity or protect against a modified image. Neither plugin tags nor project
dependency constraints are used to infer the runtime version.

For example, the source declaration identifies an executable to probe, not a
second version value:

```yaml
runtime:
  components:
    - name: python
      executable: /usr/bin/python3.12
```

For an SDK-built image, set the image metadata alongside its build target:

```yaml
build:
  workloads:
    python-base-image:
      engine: podman
      context: .
      containerfile: runtime/python-base/Containerfile
      repository: quay.io/acme/report-tools-runtime
      tag: 1.0.0-python-base
      platform: linux/amd64
      # These values label the final image; use what its Containerfile installs.
      # The SDK compares the Health-reported probe version with this annotation.
      runtimeMetadata:
        language: python
        version: "3.12.8"
```

The version above is an example. Set it to the version actually present in the
image; a supported probe checks that claim during `build` and again during
`publish`.

The resolved RuntimeBinding records what the image reports:

```yaml
runtime:
  language: python
  version: "3.12.8"
  probeStatus: matched
  components:
    - name: python
      executable: /usr/bin/python3.12
      version: "3.12.8"
      probeStatus: matched
```

`--require runtime-metadata-match` makes `build` and `publish` fail unless
every declared component of every image option has a supported matching probe
and non-empty version. An image option with no components, a missing component
version, or any unsupported component fails this requirement. `--require all`
expands to every check registered by the installed SDK. Requirements can also be set persistently in
`syntara-plugin.yaml`:

```yaml
policy:
  requirements:
    - runtime-metadata-match
```

The default is an empty requirement list. CLI requirements add to the
settings list and cannot remove a configured check. `build` records the
expanded checks and results in `dist/release.json`. `publish` probes the
digest-pinned images again and validates that evidence instead of trusting a
stored pass flag. Pin the SDK version in CI when using `all`, because a later
SDK release can register additional checks.

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
  --output dist/report-tools.oci.tar \
  --require runtime-metadata-match
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
  --artifact-kind release --require all --password-stdin --cosign-key cosign.key \
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
