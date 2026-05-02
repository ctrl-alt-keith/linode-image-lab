# Security

Linode Image Lab is designed to be public-safe by default. Dry-run behavior is
non-mutating; capture and deploy execution require explicit opt-in.

## Token Handling

- `LINODE_TOKEN` may appear as an environment variable name.
- Secret values must never be committed.
- The CLI reads `LINODE_TOKEN` for `capture --execute`, `deploy --execute`,
  `capture-deploy --execute`, `cleanup --discover`, and `cleanup --execute`.
- Plain `cleanup` does not read token values or call Linode.
- Config files cannot provide `LINODE_TOKEN` or any token value. They only fill
  non-secret execution defaults after explicit `--config PATH`.
- `config validate` validates and reports effective defaults without reading
  token values, calling Linode, or mutating resources.
- Redaction utilities sanitize sensitive keys and token-like text before output.
- Normal stdout and stderr must not print tokens, authorization headers, root
  passwords, SSH keys, cloud-init secrets, or provider resource identifiers.

## Config Safety

Config files are parsed with the Python standard library TOML parser and must
declare `schema_version = 1`. Unknown keys fail, and secret-like keys fail even
when they appear in a table that is not used by the selected command.

Supported config values are limited to region defaults, TTL, source image,
existing custom image id, and Linode type. Config cannot set `--execute`,
`--discover`, preservation flags, run ids, image labels, tokens, passwords, SSH
keys, root passwords, cloud-init data, or user-data.

Config loading and validation happen before token lookup. Execute mode and
`cleanup --discover` still require `LINODE_TOKEN` from the environment or
approved environment injection.

The `config validate` report uses the same config safety checks and redacted
serialization as other CLI output. It shows precedence as CLI values, then the
selected command table, then `[defaults]`.

## Execute Permissions

`capture --execute`, `deploy --execute`, `capture-deploy --execute`, `cleanup
--discover`, and `cleanup --execute` need a personal access token or equivalent
OAuth access that can:

- read the current profile for preflight,
- read regions, Linode types, and images for input preflight,
- create, read, shut down, and delete temporary Linodes,
- create and read custom images,
- apply tags to created resources.

In Linode scope terms, this generally means `linodes:read_write` and
`images:read_write`, plus account permissions or grants that allow Linode
creation and tagging. Deploy execution from an existing image does not create a
custom image, and standalone cleanup does not create or delete custom images.
If tags cannot be applied or later verified, execution fails safely because
cleanup depends on rediscoverable tags.

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

`plan` is dry-run only. `capture`, `deploy`, `capture-deploy`, and `cleanup`
are dry-run unless `--execute` is provided. Plain `cleanup` is a local manifest
preview only; it does not read `LINODE_TOKEN` or call Linode. `cleanup
--discover` is the explicit read-only provider discovery path.

`capture --execute`, `deploy --execute`, and `capture-deploy --execute` fail
before mutation if required options or `LINODE_TOKEN` are missing. They perform
non-mutating token preflight and read-only region, type, and image input
preflight before creating resources. Partial-failure cleanup only targets
resources whose required tags exactly match the current run. Tag mismatches are
represented as preserved resources in manifests, not as deletion attempts.

`capture-deploy --execute` creates resources with `mode=capture-deploy` and a
component-specific tag: capture resources use `component=capture`, and deploy
resources use `component=deploy`. The custom image is preserved by default.
Temporary Linodes are deleted only when all required tags match the current run.

Standalone `cleanup --execute` deletes only expired temporary Linodes with the
complete managed tag set: `project`, `run_id`, `mode`, `component`, and `ttl`.
The `ttl` value is a project-internal cleanup tag used by this tool; Linode
does not enforce it as a provider-side expiration policy. Cleanup preserves
custom images, untagged resources, resources with missing or mismatched tags,
resources with malformed or unexpired TTL values, and resources outside an
optional `--run-id` filter. Preserved entries use sanitized reason strings and
normal stdout redacts provider identifiers.

Transient Linode API retries are limited to read-only API calls, polling reads,
managed Linode discovery, and cleanup DELETE attempts for eligible tagged
temporary Linodes. Create-instance, image-create, and shutdown requests are not
retried automatically. HTTP 429 retries honor Linode's documented rate-limit
headers before falling back to deterministic backoff. Retry errors and metadata
use public-safe operation names, status categories, and delay sources rather
than tokens or provider identifiers.

Validation is limited to provider/API responses: input existence/access, image
available status, resource state, requested region, required tags, and disk
presence for capture. It does not perform SSH, cloud-init, service, or
application readiness checks.
