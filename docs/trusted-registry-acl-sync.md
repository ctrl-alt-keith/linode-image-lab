# Trusted registry to LKE control-plane ACL

`acl-sync` consumes the [Trusted Network Registry v1 contract](https://github.com/ctrl-alt-keith/trusted-network-registry/blob/main/docs/registry-schema.md) through the existing Object Storage transport. It owns one specified cluster's **control-plane ACL only**. The cluster id and expected label must both match. It does not change worker firewalls, NodeBalancers, Kubernetes workloads, or outbound rules.

The default is a read-only plan. `--output-format summary` omits CIDRs and provider identifiers for routine Job logs. A full manifest includes CIDRs and belongs only in protected operator evidence. Both modes require `LINODE_TOKEN` and the existing `LINODE_OBJ_ACCESS_KEY` and `LINODE_OBJ_SECRET_KEY` registry reader variables. Execute requires LKE ACL write permission.

```sh
linode-image-lab --config /protected/acl-sync.toml acl-sync --output-format summary
linode-image-lab --config /protected/acl-sync.toml acl-sync --output-format summary --execute --allow-enable # first enablement only
linode-image-lab --config /protected/acl-sync.toml acl-sync --output-format summary --execute # subsequent updates
```

The command rejects invalid, stale, empty, or universal-allow registry input and never uses cached CIDRs. A disabled ACL requires explicit `--allow-enable` for the first write; routine Jobs omit it. Immediately before PUT it re-fetches the registry, verifies the same cluster identity, and re-reads the ACL. A changed input stops the write. The provider has no atomic compare-and-swap for this ACL; coordinate so this command is its only writer. A successful PUT is reported as **submitted**, not proven enforced; first enablement may take time. On an uncertain PUT result, inspect the provider ACL before retry. Independent recovery uses the same command with protected config and credentials through the provider API; it does not require Kubernetes API access.
