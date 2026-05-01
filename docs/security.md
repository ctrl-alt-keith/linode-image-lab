# Security

M1 is designed to be public-safe and non-mutating.

## Token Handling

- `LINODE_TOKEN` may appear as an environment variable name.
- Secret values must never be committed.
- The CLI does not read token values in M1.
- Redaction utilities sanitize sensitive keys and token-like text before output.

## Public-Safety Scan

`make security-check` scans repository text files for:

- sensitive value assignments,
- email-like values,
- private network URLs,
- restricted workplace metadata,
- hidden Unicode bidirectional control characters,
- non-public fixture placement,
- legacy image workflow terminology.

The scan is intentionally small and local. It is a guardrail, not a full data
loss prevention system.

## Mutation Safety

`plan` is dry-run only. `capture`, `deploy`, and `capture-deploy` return
explicit placeholder responses. `cleanup` currently selects candidates from
provided data structures and does not call Linode.
