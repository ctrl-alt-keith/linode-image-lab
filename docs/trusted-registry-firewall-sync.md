# Trusted Registry Firewall Sync

`firewall-sync` is the first consumer for a private Trusted Network Registry.
It fetches registry JSON from Linode Object Storage, validates it, reads the
target Linode Cloud Firewall rules, and plans one managed inbound allow rule.

The command is dry-run by default. It mutates the firewall only when
`--execute` is provided.

This is not general firewall management. The target firewall is an
operator-owned existing resource, and the registry controls only the CIDRs for
one managed inbound allowlist rule. `firewall-sync` does not create or attach
firewalls, change outbound policy, split allowlists across multiple rules,
cache fallback CIDRs, run continuously, or reconcile the rest of the firewall
configuration.

## Compatibility Contract

- Consumer: `ctrl-alt-keith/linode-image-lab` `firewall-sync`.
- Producer: `ctrl-alt-keith/trusted-network-registry`.
- Artifact: Trusted Network Registry registry JSON.
- Accepted schema version: registry schema v1, identified by top-level
  `schema_version: 1`.
- Compatibility fixture:
  [`../tests/fixtures/sanitized/trusted-network-registry.v1.example.json`](../tests/fixtures/sanitized/trusted-network-registry.v1.example.json),
  vendored from the producer's public-safe v1 registry fixture.

The compatibility test feeds the vendored fixture through the trusted registry
validator and firewall-sync planning path. Incompatible producer artifact
changes require a new schema/version, and this consumer must explicitly opt in
before accepting incompatible schema versions.

## Required Inputs

Non-secret inputs can come from CLI flags or `[firewall-sync]` config:

- `firewall_id`
- `registry_endpoint_url`, HTTPS only
- `registry_bucket`
- `registry_object_key`
- `registry_region`, optional when the region can be inferred from the endpoint
- `protocol`, defaults to `TCP`
- `ports`, required for `TCP` and `UDP`
- `managed_label`, defaults to `tnr-allowlist`

Secrets must come from environment variables only:

- `LINODE_TOKEN`
- `LINODE_OBJ_ACCESS_KEY`
- `LINODE_OBJ_SECRET_KEY`

Do not commit real bucket names, object keys, endpoints, private CIDRs, firewall
IDs, or credential values.

Example config shape:

```toml
schema_version = 1

[firewall-sync]
firewall_id = 12345
registry_endpoint_url = "https://us-east-1.linodeobjects.com"
registry_bucket = "example-bucket"
registry_object_key = "registry.json"
ports = "22"
```

## Dry-Run Workflow

Dry-run fetches and validates the registry, reads the current firewall rules,
computes the intended change, and emits a redacted JSON manifest. It does not
call the firewall update endpoint.

```sh
export LINODE_TOKEN='<linode-api-token>'
export LINODE_OBJ_ACCESS_KEY='<object-storage-access-key>'
export LINODE_OBJ_SECRET_KEY='<object-storage-secret-key>'

linode-image-lab --config examples/config/firewall-sync.example.toml firewall-sync
```

The manifest includes planned additions, removals, and kept CIDRs. CIDRs appear
because this command is specifically for allowlist review; keep logs in an
operator-appropriate location.

For routine scheduled runs, add `--output-format summary`. This emits one JSON
object with outcome version, status, execution mode, registry timestamps, and
CIDR/change counts. It excludes CIDRs, the provider request payload, firewall
ID, bucket, and object key. Runtime failures after argument parsing exit
nonzero and emit `status: failed`; when failure occurs before planning,
registry timestamps and counts are absent. Argument-parser errors exit 2 and
do not emit JSON.
Do not use the summary as an operator approval view. The default `manifest`
format remains the full protected dry-run evidence. `--manifest-file` writes the
selected format, so a scheduled Job should omit it unless an approved evidence
destination is mounted.

## Execute Workflow

After reviewing dry-run output, add `--execute`:

```sh
linode-image-lab --config examples/config/firewall-sync.example.toml firewall-sync --execute
```

Execute mode prints a short planned-change summary to stderr before applying
the update, then emits the final JSON manifest on stdout.

Only the exact managed rule is replaced or added. Unrelated inbound and
outbound rules are preserved in the submitted rule payload.

Immediately before an execute-mode update, the command re-reads the firewall
rules and compares them with the rules used for planning. If the rules changed
while the plan was being reviewed, or the pre-write response is malformed, the
command fails closed without updating the firewall and emits the planned
manifest. Rerun the dry-run and review the new plan before retrying execute.

## Registry Validation

The command fails closed when the registry cannot be fetched or validated. It
does not fall back to local, stale, cached, or default CIDRs.

Validation requires:

- JSON object payload
- supported `schema_version`
- non-stale `registry.valid_until`
- active entries only
- canonical IPv4 and IPv6 CIDRs
- matching `address_family`
- no universal allow CIDRs such as `0.0.0.0/0` or `::/0`

## Managed Rule Ownership

Linode firewall rules are updated through the whole firewall rules document.
Rule labels and descriptions are operator-facing identifiers, not stable
per-rule IDs. This command therefore owns exactly one inbound rule where both
of these match:

- label: configured `managed_label`
- description: `Managed by linode-image-lab trusted-network-registry sync.`

If a rule uses the managed label without the managed description, or more than
one rule uses the managed label, the command fails closed and does not update
the firewall.

## Rollback And Recovery

Before execute, save the dry-run manifest and current firewall export from your
normal operator tooling if rollback evidence is needed.

If execute applies the wrong intended allowlist, publish a corrected registry or
restore the prior CIDRs in the registry source, then rerun `firewall-sync`
dry-run and execute. If the managed rule itself should be removed, remove the
single managed rule manually after confirming its label and description match
the ownership marker above.

If registry publication is stale or unavailable, `firewall-sync` stops before
firewall update. Fix the publisher or Object Storage access first; do not use
local fallback CIDRs.

## Container Build And Job Interface

The root `Dockerfile` packages this repository's Python source with no package
download or install step. Its official Python 3.12.15 slim Bookworm base is
pinned to a verified Linux amd64 manifest digest. From a clean checkout, the
build context includes only source, package version metadata, and public region
policy; it excludes configuration, credentials, and fixtures. The image runs as
UID/GID 65532 and starts the `linode-image-lab` console entrypoint. It performs
one command and exits; it owns no schedule.

Build from a clean checkout of the exact reviewed source commit, with no
modified or untracked package Python files: the ignore rules allow any `*.py`
under `src/linode_image_lab/`. Target Linux amd64 and record the source commit,
base digest, and resulting pushed image digest in the deployment review.
Deployment must reference the resulting image by digest; this repository does
not claim one before publication.

```sh
docker build --platform linux/amd64 --tag linode-image-lab:local .
```

The manual `Publish firewall consumer image` workflow is source-owned and
checks out the exact approved source commit, not the workflow's own commit.
Its tag is `sha-<source-commit>` in
`ghcr.io/ctrl-alt-keith/linode-image-lab/firewall-consumer`. It runs `make
check`, builds and smoke-tests Linux amd64, refuses to overwrite an existing
tag or publish when tag absence cannot be proven, and uses only the repository
GitHub token with package write permission. It records the distinct source and
workflow commits, builder, run, and pushed manifest digest in the workflow run
summary. The publish run also attempts a pull without registry credentials.
GitHub creates a new container package as private by default, so its first
publish run may push successfully but fail the anonymous-pull check. In that
case, make the package public in GitHub package settings, then dispatch the
same workflow in `verify-public` mode with the exact pushed digest from the
publish run. That mode performs no build or push and succeeds only if an
anonymous pull resolves the tag to that digest. A private or otherwise
inaccessible package must not be used by deployment.

GitHub permits manual dispatch only after the workflow file exists on the
repository's default branch. Landing that workflow and starting its first run
are separate controlled actions; this draft branch cannot publish the image.
The pinned source commit must also remain fetchable at run time. The current PR
branch contains it; any later source-ref removal or squash merge requires a
reviewed retention or promotion decision before dispatch. A failed checkout
stops before image publication.

The LKE Job supplies an existing `[firewall-sync]` TOML config mount and the
three environment variables named above. Its argv must include
`--output-format summary` to keep routine Job logs CIDR-free:

```sh
linode-image-lab --config /config/firewall-sync.toml firewall-sync --output-format summary
```

The Job may append `--execute` only after separate standing update authority
is approved. Keep the config, credentials, private registry, full dry-run
manifest, and Job logs under their respective protected owners. A failed cycle
does not add a fallback CIDR and does not intentionally remove the last applied
firewall rule.
