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

The source-owned `Publish firewall consumer image` workflow checks out exact
approved source commit `f76100c298c96a3a6fa4eeb1fbbcceb06dfe234e`, not
the workflow's own commit. It tags
`ghcr.io/ctrl-alt-keith/linode-image-lab/firewall-consumer:sha-f76100c298c96a3a6fa4eeb1fbbcceb06dfe234e`.
It runs `make check`, builds and smoke-tests Linux amd64, and records the
distinct source and workflow commits, builder, run, and pushed manifest digest.
Only the publish job receives the repository GitHub token with package write
permission; the separate public-verification job has no package permission.

The one-time first-package route is a push of the exact annotated tag
`cak-364-consumer-publish-f76100c298c96a3a6fa4eeb1fbbcceb06dfe234e` at
the reviewed workflow commit. The tag message must name the exact package and
Keith's owner-observed organization Packages inventory at 2026-10-08 20:10
UTC (source-thread reference `Sentinel_32bf136f93ac8191aa738a7c5cb96f4e`).
Keith saw only `lke-image-lab/cak-canary`; the consumer package was absent at
that time. This is attributable first-bootstrap evidence, not an independent
API verification or a claim about all later times. The workflow checks the
annotated tag, its target, the source commit's ancestry, and these exact
evidence fields. It refuses a repeat attempt. No tag has been created.
If another actor creates this package path, the organization inventory changes
in a way that affects the absence decision, or a registry check becomes
inconsistent with that decision, stop and refresh the owner evidence. Minutes
spent completing this review do not alone change the observed inventory.
If the first run fails before push, do not force-move the publication tag or
rerun that attempt; a corrected workflow needs a separately reviewed unique
trigger. If push succeeded, proceed only with visibility and verification.

Required annotated tag message lines:

```text
CAK-364-Package: ghcr.io/ctrl-alt-keith/linode-image-lab/firewall-consumer
CAK-364-Absence-Observed-By: Keith
CAK-364-Absence-Observed-At: 2026-10-08T20:10:00Z
CAK-364-Absence-Evidence: Sentinel_32bf136f93ac8191aa738a7c5cb96f4e
```

During bootstrap, package API `404` supports the owner evidence but is not
alone proof of absence. An existing package (`200`), authentication or
transport failure, or a positive registry manifest stops publication. The
registry may return `denied` for a new path; that response is accepted only
with the owner evidence and API `404`, never as an absence claim by itself.
After the package exists, manual publication from `main` requires a successful
package API read and refuses the target tag if it appears among any package
versions. It also refuses a positive registry manifest. The tag is checked
after the build and immediately before push. These checks cannot prevent an
unrelated writer racing a new tag into GHCR between check and push; keep this
one-time path under the reviewed publication decision.

GitHub creates a new container package as private by default. After the first
push, a package administrator must make it public. Then push a distinct
`cak-364-consumer-verify-sha256-<pushed-digest-hex>` tag at the same reviewed
workflow commit. Its separate fresh runner uses an empty Docker auth config,
checks the published digest and pulls the exact source tag anonymously,
requiring the tag digest and Linux amd64 platform to match. It never builds or
pushes. Do not deploy a private package or a digest before this verification.

GitHub permits manual dispatch only after the workflow file exists on the
default branch; the exact-tag push route allows a reviewed workflow to run
before merge. The source SHA is an ancestor of the current workflow branch;
retaining a tag at its reviewed descendant keeps that source reachable even
if the PR is later squash-merged and its branch removed. A failed checkout
stops before publication. Neither merge nor tag push is part of this source
contract.

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
