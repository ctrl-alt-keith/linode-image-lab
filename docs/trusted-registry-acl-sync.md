# Trusted registry to LKE control-plane ACL

`acl-sync` consumes the [Trusted Network Registry v1 contract](https://github.com/ctrl-alt-keith/trusted-network-registry/blob/main/docs/registry-schema.md) through the existing Object Storage transport. It owns one specified cluster's **control-plane ACL only**. The cluster id and expected label must both match. It does not change worker firewalls, NodeBalancers, Kubernetes workloads, or outbound rules.

The default is a read-only plan. `--output-format summary` omits CIDRs and provider identifiers for routine Job logs. A full manifest includes CIDRs and belongs only in protected operator evidence. Both modes require `LINODE_TOKEN` and the existing `LINODE_OBJ_ACCESS_KEY` and `LINODE_OBJ_SECRET_KEY` registry reader variables. Execute requires LKE ACL write permission.

```sh
linode-image-lab --config /protected/acl-sync.toml acl-sync --output-format summary
linode-image-lab --config /protected/acl-sync.toml acl-sync --output-format summary --execute --allow-enable # first enablement only
linode-image-lab --config /protected/acl-sync.toml acl-sync --output-format summary --execute # subsequent updates
```

The command rejects invalid, stale, empty, or universal-allow registry input and never uses cached CIDRs. A disabled ACL requires explicit `--allow-enable` for the first write; routine Jobs omit it. Immediately before PUT it re-fetches the registry, verifies the same cluster identity, and re-reads the ACL. A changed input stops the write. The provider has no atomic compare-and-swap for this ACL; coordinate so this command is its only writer. A successful PUT is reported as **submitted**, not proven enforced; first enablement may take time. On an uncertain PUT result, inspect the provider ACL before retry. Independent recovery uses the same command with protected config and credentials through the provider API; it does not require Kubernetes API access.

PUT failures expose only a fixed phase, category, and bounded HTTP status in routine summary output. The provider's published *Update the control plane access control list* reference documents 400 and 404 rejections; authentication and permission failures (401 and 403) are also `rejected`. Other HTTP responses, transport errors, malformed responses, and an interrupted PUT require provider ACL readback before retry. Neither status proves the current enforced ACL.

For a private diagnostic record, pass `--provider-error-file /protected/private-dir/new-error.json` with `--execute`. An unsafe path aborts before PUT; a no-change run creates no file. The file must not exist, and its parent must be owned and mode 0700. The command creates it at mode 0600 just before a PUT and writes a `submitted` record on success or up to eight provider errors on failure, with up to 96 characters each of `field`, `reason`, and `message`; 401 and 403 responses omit provider details. Credential-like text is redacted, but CIDRs or other private details may remain. Inspect it privately; never print, upload, or commit it. The routine summary contains only fixed diagnostics and whether the private record was written.
