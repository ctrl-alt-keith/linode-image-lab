# Trusted registry to LKE control-plane ACL

`acl-sync` writes only the specified cluster's control-plane ACL. The cluster ID and label must match. It reads the [Trusted Network Registry v1](https://github.com/ctrl-alt-keith/trusted-network-registry/blob/main/docs/registry-schema.md) from Object Storage. It does not change worker or Service firewalls.

The default is a dry-run. Use `--output-format summary` for CIDR-free logs; keep the full manifest in protected operator evidence. Set `LINODE_TOKEN`, `LINODE_OBJ_ACCESS_KEY`, and `LINODE_OBJ_SECRET_KEY` for both modes.

```sh
linode-image-lab --config /protected/acl-sync.toml acl-sync --output-format summary
linode-image-lab --config /protected/acl-sync.toml acl-sync --output-format summary --execute
```

First enablement of a disabled ACL also requires `--allow-enable`. The writer rejects invalid or stale registry data and rechecks the registry, cluster, and ACL before PUT. Keep it as the sole ACL writer; the API has no compare-and-swap. A submitted PUT does not prove enforcement. Read the provider ACL before retrying an uncertain result.

## Codex Cloud snapshot

For the selected cluster only, set `openai_snapshot_cluster_id` to its `cluster_id` in protected `[acl-sync]` config. The bundled [egress snapshot](https://openai.com/chatgpt-agents.json) is pinned by digest, creation time, and counts. The writer unions its exact-coverage CIDRs with registry entries, refuses to remove an existing ACL entry, and limits the combined list to 2,048. The ACL has no port selector. The provider does not document its entry limit.

Before execute, save a protected provider ACL baseline and review the full dry-run diff for the exact target, fresh registry, zero removals, and complete payload. After execute, GET and compare the full ACL. To roll back, remove `openai_snapshot_cluster_id`, review the removal-only dry-run against the baseline, execute through this writer, and GET again.
