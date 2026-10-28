# Compiled contract revision identities

The compiler emits `spec.plugin_revision_id`, `spec.sdkVersion`, and a
`step_revision_id` on each Action, Control, and Trigger target. Authors do not
write these fields. IDs are lowercase `sha256:` digests. `sdkVersion` is the
exact installed `syntara-plugin-sdk` distribution version, including any
pre-release or local version suffix.

The `syntara.compiled-contract/v1` projection is a JSON object hashed using
SHA-256 over UTF-8 JSON: keys sorted by Unicode code point, no insignificant
whitespace, non-ASCII characters unescaped, and no non-finite numbers. Arrays
retain order. The projection is domain-separated by its `projection` and
`kind` fields. A new projection algorithm requires a new projection version.
Serialization follows `canonical_json_bytes` in the contracts package,
including Python JSON number rendering (for example, `1.0`, `-0.0`, and
`1e-07` remain distinct spellings). Object keys and strings are not additionally
Unicode-normalized by this projection.

For a plugin, the preimage has `projection`, `kind: "plugin"`, and `descriptor`.
The descriptor is the entire compiled descriptor with `spec.plugin_revision_id`
and each target's `step_revision_id` removed. From `spec.workloads`, it removes
only workload entries referenced by custom-runner targets that are not also
providers. Provider entries remain, including those shared with custom-runner
targets, as do workload entries not referenced by any custom-runner target; their
image bindings participate in the plugin ID.
Workload names and ABIs used by steps remain in each step's runtime contract.
All other descriptor fields, including the asset inventory,
bundle digest, and SDK version, participate. The full descriptor digest and
OCI artifact digest are separate from this revision ID.

For a step, the preimage has `projection`, `kind: "step"`, `plugin` containing
only the plugin's `namespace`, `name`, and `version`, `sdkVersion`,
`contractBundleDigest`, and `target` containing the complete compiled step
except `step_revision_id`. Consequently a change to another target or plugin
documentation changes the plugin ID but not this step's ID. Changes to the
step, its plugin release coordinates, SDK version, or contract bundle change
the step ID.

The step ID does not include workload image bindings. The plugin ID excludes
the image binding of a workload referenced by a custom-runner target when that
workload is not also a provider. The digest is separately bound in
`spec.workloads` and the complete descriptor and artifact. Excluding the
non-provider binding avoids a cycle: the image contains the registration
manifest, which contains these IDs. Different images may therefore have the same
contract IDs and must be distinguished by their separately pinned image digests.
Provider and unreferenced workload image bindings remain in the plugin
projection and affect the plugin ID.

`--with-workload` builds the image with a canonical JSON registration manifest
at `/opt/syntara/registration-manifest.json`. It contains `apiVersion`
`syntara.registration/v1alpha1`, `kind` `RegistrationManifest`,
`plugin_revision_id`, `sdkVersion`, `workload`, and `steps`, an object keyed by
step revision ID with `entryPoint` and `abi` values. The compiler validates the
mapping against the pre-build descriptor and checks the contract again after the
image build. The public `registration_manifest` and
`validate_registration_manifest` functions in `syntara_plugin.contracts`
support producers of external images; supplying an image binding alone does not
prove that image contains the manifest. Runtime loading and dispatch are separate
work.
