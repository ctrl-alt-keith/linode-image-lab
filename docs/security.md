# Security

Linode Image Lab is designed to be public-safe by default. Dry-run behavior is
non-mutating; capture and deploy execution require explicit opt-in.

## Token Handling

- `LINODE_TOKEN` may appear as an environment variable name.
- Secret values must never be committed.
- The CLI reads `LINODE_TOKEN` only for `capture --execute`,
  `deploy --execute`, or `capture-deploy --execute`.
- Dry-run commands do not read token values.
- Redaction utilities sanitize sensitive keys and token-like text before output.
- Normal stdout and stderr must not print tokens, authorization headers, root
  passwords, SSH keys, cloud-init secrets, or provider resource identifiers.

## Execute Permissions

`capture --execute`, `deploy --execute`, and `capture-deploy --execute` need a
personal access token or equivalent OAuth access that can:

- read the current profile for preflight,
- create, read, shut down, and delete temporary Linodes,
- create and read custom images,
- apply tags to created resources.

In Linode scope terms, this generally means `linodes:read_write` and
`images:read_write`, plus account permissions or grants that allow Linode
creation and tagging. Deploy execution from an existing image does not create a
custom image, but the same image read permissions are expected. If tags cannot
be applied or later verified, execution fails safely because cleanup depends on
rediscoverable tags.

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

`plan` is dry-run only. `capture`, `deploy`, and `capture-deploy` are dry-run
unless `--execute` is provided. `cleanup` currently selects candidates from
provided data structures and does not call Linode.

`capture --execute`, `deploy --execute`, and `capture-deploy --execute` fail
before mutation if required options or `LINODE_TOKEN` are missing. They perform
non-mutating token preflight before creating resources. Partial-failure cleanup
only targets resources whose required tags exactly match the current run.
Tag mismatches are represented as preserved resources in manifests, not as
deletion attempts.

`capture-deploy --execute` creates resources with `mode=capture-deploy` and a
component-specific tag: capture resources use `component=capture`, and deploy
resources use `component=deploy`. The custom image is preserved by default.
Temporary Linodes are deleted only when all required tags match the current run.

Validation is limited to provider/API responses: resource state, requested
region, required tags, disk presence for capture, and image availability for
capture. It does not perform SSH, cloud-init, service, or application readiness
checks.
