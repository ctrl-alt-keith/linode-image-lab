# Trusted registry to LKE control-plane ACL

`acl-sync` writes only the specified cluster's control-plane ACL. The cluster ID and label must match. Registry mode reads the [Trusted Network Registry v1](https://github.com/ctrl-alt-keith/trusted-network-registry/blob/main/docs/registry-schema.md) from Object Storage. It does not change worker or Service firewalls.

The default is a dry-run. Use `--output-format summary` for CIDR-free logs; keep the full manifest in protected operator evidence. Set `LINODE_TOKEN` for both modes; registry mode also needs `LINODE_OBJ_ACCESS_KEY` and `LINODE_OBJ_SECRET_KEY`.

```sh
linode-image-lab --config /protected/acl-sync.toml acl-sync --output-format summary
linode-image-lab --config /protected/acl-sync.toml acl-sync --output-format summary --execute
```

First enablement of a disabled ACL also requires `--allow-enable`. The writer rejects invalid or stale registry data and rechecks the registry, cluster, and ACL before PUT. Keep it as the sole ACL writer; the API has no compare-and-swap. A submitted PUT does not prove enforcement. Read the provider ACL before retrying an uncertain result.

PUT failures expose only a fixed phase, category, and bounded HTTP status. A recognized 4xx rejection is `rejected`; transport, server, and malformed-response failures remain `uncertain` and require a provider GET before retry.

To retain a rejected rule, pass `--provider-error-file /protected/private-dir/new-error.json` on an execute attempt. The parent directory must be owned and mode 0700; the file is created exclusively at mode 0600. It contains bounded provider `field`, `reason`, and `message` values. Inspect it privately; never print, upload, or commit it. Routine summary output contains only fixed diagnostics.

## Codex Cloud snapshot

For the selected cluster only, set `openai_snapshot_cluster_id` to its `cluster_id` in protected `[acl-sync]` config. The bundled [egress snapshot](https://openai.com/chatgpt-agents.json) is pinned by digest, creation time, and counts. The normal writer unions it with registry entries, refuses removals, and limits the result to 2,048 entries. The ACL has no port selector; the provider does not document its entry limit.

For cluster 664899, where the live entries differ from the registry, use `baseline_acl_file` and `baseline_acl_sha256` instead. Save an owner-only mode-0600 JSON file with `schema_version: 1`, exact `cluster_id`, `cluster_label`, and the full provider `acl` object, including `revision-id` if present. The digest is `acl_digest(normalize_acl({"acl": document["acl"]}))`; pin its hex value in protected config. This mode reads no registry and adds only the snapshot to that exact enabled baseline. It aborts if the live ACL differs. Review the dry-run payload and zero removals before `--execute`, then GET the ACL and compare it with the payload.

For rollback, keep the same baseline and snapshot inputs, set `baseline_rollback = true`, and review the dry-run. It proceeds only if the live ACL exactly matches the expected snapshot expansion. After `--execute`, GET and compare it with the original baseline addresses. Neither operation verifies enforcement automatically.
