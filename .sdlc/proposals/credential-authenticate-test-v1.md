# Proposal v1: credential authenticate and test operations

Status: proposed, not part of `syntara.io/v1alpha1`.

The current CredentialRecipe and IntegrationType contracts define metadata,
credential schema, secret fields, and issuance. They do not define plugin
`authenticate` or connection `test` operations. This proposal reserves those
behaviors for a future versioned contract: it must specify operation inputs,
outputs, safe failures, authorization and secret handling before any schema or
runtime implementation is added. No field described here is accepted by the
current schemas, and this proposal does not imply a live connection test or
credential issuance implementation.
