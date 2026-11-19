# Credential compat steps

Test-only fixture workload for the Execution Plane compatibility harness in
this repository. It is never published.

It declares one integration (`compat.credentials.connection`) with a single
bearer-token credential recipe (`compat.credentials.bearer-token`) and three
custom-runner Action steps that mirror the HTTP Request, Script Bash, and
Script Python step-type shapes:

- `compat.credential-steps.script-bash` runs Bash code in a subprocess.
- `compat.credential-steps.script-python` runs Python code in a subprocess.
- `compat.credential-steps.http-request` makes a loopback HTTP request.

When the script step inputs set `environment.COMPAT_USE_CREDENTIAL=1`, the
worker reads the credential delivered through the invocation callback and
appends `COMPAT_CREDENTIAL_SHA256=<hex>` to stdout: the sha256 of the
canonical JSON credential document. The HTTP step always sends the delivered
token as an `Authorization: Bearer` header to a loopback server that accepts
only that token and answers with the same digest. No step output, log, or
error ever carries the token value itself.

See `docs/developer-guide.md` (Execution Plane compatibility) for how the
harness dispatches these steps.
