# python-steps example

One example step for each Syntara taxonomy class, tested locally through the
SDK test harness without AO, EP, or a cluster.

| Step | Taxonomy | Source manifest? | Tested through |
|---|---|---|---|
| `create_issue.CreateIssueStep` | Action | `steps/create-issue/manifest.yaml` (custom-runner) | `step_bindings.bind_action` (compile, install, drift-check, execute) |
| `record_opened.RecordOpenedStep` | Trigger | `steps/record-opened/manifest.yaml` (driver + Python binding) | `manifest_validator` + `step_bindings.bind` |
| `normalize_records.NormalizeRecordsTask` | Task | none (owned by the step-type repository) | `step_bindings.bind` (descriptor path) |
| `compose_remediation.ComposeRemediationWorkflow` | Workflow | none (owned by the step-type repository) | `step_bindings.bind` (descriptor path) |

`plugin.yaml` declares only the Action and Trigger targets: Task and Workflow
source-manifest schemas are owned by their step-type repositories, so those
steps are not listed there and are tested through implementation-derived
descriptors.

Run the suite from the repository root:

    uv run --no-sync pytest examples/python-steps/tests -q
