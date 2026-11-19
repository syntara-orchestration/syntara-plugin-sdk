# python-steps example

One example step type per Syntara kind, tested locally through the
SDK test harness without AO, EP, or a cluster.

| Step type | Taxonomy | Source manifest? | Tested through |
|---|---|---|---|
| `create_issue.CreateIssueStep` | Action | `steps/create-issue/manifest.yaml` (custom-runner) | `step_bindings.bind_step` (compile, install, drift-check, execute) |
| `record_opened.RecordOpenedStep` | Trigger | `steps/record-opened/manifest.yaml` (driver + Python binding) | `manifest_validator` + `step_bindings.bind` |
| `normalize_records.NormalizeRecordsStep` | Control | none (owned by the step-type repository) | `step_bindings.bind` (local install) |
| `compose_remediation.ComposeRemediationStep` | Control | none (owned by the step-type repository) | `step_bindings.bind` (local install) |

`plugin.yaml` declares only the Action and Trigger targets: the Control
source-manifest schemas are owned by their step-type repositories, so those
steps are not listed there and are tested through implementation-derived
descriptors.

Run the suite from the repository root:

    uv run --no-sync pytest examples/python-steps/tests -q

## Runtime metadata for a built image

When packaging this plugin as a custom-runner image, declare the executable
to probe in `runtime-bindings.yaml` and the image's actual language and
version in `syntara-plugin.yaml`. The binding workload ID matches the image
identifier declared by `plugin.yaml`; its `buildTarget` matches the workload
settings key:

```yaml
# runtime-bindings.yaml
apiVersion: syntara.io/v1alpha1
kind: RuntimeBinding
metadata:
  namespace: example
  name: python-steps-runtime
spec:
  plugin:
    source: plugin.yaml
  workloads:
    python-steps-workload:
      default: python-base
      options:
        - name: python-base
          runtime:
            components:
              - name: python
                executable: /usr/local/bin/python
          image:
            buildTarget: python-runtime
```

```yaml
# syntara-plugin.yaml
apiVersion: syntara.io/v1alpha1
kind: PluginBuildSettings
manifest: plugin.yaml
build:
  runtimeBindings: runtime-bindings.yaml
  workloads:
    python-runtime:
      engine: podman
      context: .
      containerfile: Containerfile
      repository: quay.io/acme/python-steps-runtime
      tag: "0.1.0"
      platform: linux/amd64
      runtimeMetadata:
        # Adds io.syntara.runtime.language=python and
        # io.syntara.runtime.version=3.12.8 to the built image.
        # Match the version installed by this Containerfile; these annotations
        # describe the image and do not install or select Python.
        language: python
        version: "3.12.8"
```

The example version must be the Python version installed in the image's
Containerfile. Replace `3.12.8` with the version actually installed in your
image. `runtimeMetadata` adds OCI annotations; it does not install Python or
choose its version. For this example, if the image's interpreter reports
Python `3.12.8`, the annotation and the Health probe agree. If the image reports
a different version, the SDK rejects the mismatch during `build`. The same
digest-pinned image is checked again during `publish`.
