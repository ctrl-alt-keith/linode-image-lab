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

## Execute Workflow

After reviewing dry-run output, add `--execute`:

```sh
linode-image-lab --config examples/config/firewall-sync.example.toml firewall-sync --execute
```

Execute mode prints a short planned-change summary to stderr before applying
the update, then emits the final JSON manifest on stdout.

Only the exact managed rule is replaced or added. Unrelated inbound and
outbound rules are preserved in the submitted rule payload.

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
