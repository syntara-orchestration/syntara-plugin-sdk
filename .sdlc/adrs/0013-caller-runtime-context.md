# Caller-owned runtime context

Status: Accepted

The SDK executes individual invocations. It does not manage the caller's
workflow executions, so it should not require an orchestration-engine run ID.

Remove the dedicated `workflow_run_id`. Callers can supply correlation data
through `runtime_context`, whose keys the SDK does not prescribe. The SDK passes
that object to plugin code unchanged. `invocation_id` identifies the invocation;
`step_revision_id` selects the handler.
