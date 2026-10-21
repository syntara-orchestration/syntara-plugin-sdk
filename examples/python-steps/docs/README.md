# Python steps example

One example step for each Syntara kind: Action, Control, and Trigger. The
Action and Trigger steps declare source manifests in `plugin.yaml`; Control
source-manifest schemas are owned by their step-type repositories, so that
step lives as a Python module and is tested through a locally installed
manifest.

Run the example test suite from the repository root:

    uv run --no-sync pytest examples/python-steps/tests -q
