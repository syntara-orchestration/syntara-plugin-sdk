# Caller-owned runtime context

Status: Accepted

The container runtime executes an invocation and selects a handler by
`step_revision_id`. Requiring an orchestration-engine workflow run ID couples
the runtime to a caller's execution model; local tests previously invented one.

Remove the dedicated `workflow_run_id` from the container ABI, runtime context
class, harness, CLI reports, and generic work-item schema. Keep `invocation_id`
and `step_revision_id`. Callers can supply their project, workflow, execution,
or optional `temporal_run_id` in the existing non-secret runtime context object.
The SDK forwards that object without assigning domain semantics or inventing IDs.

EP stores runtime context in a separate indexed JSONB work-item column and maps
it into the SDK invocation. Storage/query ownership and plugin visibility are
independent: EP still owns the work item, and the plugin receives the context.
The encrypted invocation payload does not retain a second copy. Credentials
continue to use the authenticated callback, never caller context.

This is a coordinated pre-release contract change. Reserve protobuf field 8 and
its old name; regenerate bindings and rebuild plugin containers with the host
contracts. JSON schemas reject the removed field. Old runtimes require it and
must be upgraded; no compatibility alias or generated workflow ID is provided.
