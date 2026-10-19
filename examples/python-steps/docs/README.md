# Python steps example

One example step for each Syntara taxonomy class: Action, Task, Workflow, and
Trigger. The Action and Trigger steps declare source manifests in `plugin.yaml`;
Task and Workflow source-manifest schemas are owned by their step-type
repositories, so those steps live as Python modules and are tested through the
implementation-derived descriptor path.

Run the example test suite from the repository root:

    uv run --no-sync pytest examples/python-steps/tests -q
