# Trusted registry to LKE control-plane ACL

`acl-sync` consumes the [Trusted Network Registry v1 contract](https://github.com/ctrl-alt-keith/trusted-network-registry/blob/main/docs/registry-schema.md) through the existing Object Storage transport. It owns one specified cluster's **control-plane ACL only**. The cluster id and expected label must both match. It does not change worker firewalls, NodeBalancers, Kubernetes workloads, or outbound rules.

The default is a read-only plan. `--output-format summary` omits CIDRs and provider identifiers for routine Job logs. A full manifest includes CIDRs and belongs only in protected operator evidence. Both modes require `LINODE_TOKEN` and the existing `LINODE_OBJ_ACCESS_KEY` and `LINODE_OBJ_SECRET_KEY` registry reader variables. Execute requires LKE ACL write permission.

```sh
linode-image-lab --config /protected/acl-sync.toml acl-sync --output-format summary
linode-image-lab --config /protected/acl-sync.toml acl-sync --output-format summary --execute --allow-enable # first enablement only
linode-image-lab --config /protected/acl-sync.toml acl-sync --output-format summary --execute # subsequent updates
```

The command rejects invalid, stale, empty, or universal-allow registry input and never uses cached CIDRs. A disabled ACL requires explicit `--allow-enable` for the first write; routine Jobs omit it. Immediately before PUT it re-fetches the registry, verifies the same cluster identity, and re-reads the ACL. A changed input stops the write. The provider has no documented atomic compare-and-swap for this ACL; coordinate so this command is its only writer. A successful PUT is reported as **submitted**, not proven enforced; first enablement may take time. On an uncertain PUT result, inspect the provider ACL before retry. Use the restoration procedure below for exact baseline recovery.

PUT failures expose only a fixed phase, category, and bounded HTTP status in routine summary output. The provider's published *Update the control plane access control list* reference documents 400 and 404 rejections; authentication and permission failures (401 and 403) are also `rejected`. Other HTTP responses, transport errors, malformed responses, and an interrupted PUT require provider ACL readback before retry. Neither status proves the current enforced ACL.

For a private diagnostic record, pass `--provider-error-file /protected/private-dir/new-error.json` with `--execute`. An unsafe path aborts before PUT; a no-change run creates no file. The file must not exist, and its parent must be owned and mode 0700. The command creates it at mode 0600 just before a PUT and writes a `submitted` record on success or up to eight provider errors on failure, with up to 96 characters each of `field`, `reason`, and `message`; 401 and 403 responses omit provider details. Credential-like text is redacted, but CIDRs or other private details may remain. Inspect it privately; never print, upload, or commit it. The routine summary contains only fixed diagnostics and whether the private record was written.

## Previewing route preservation

The regular plan and execute paths replace the ACL address lists with registry
entries. For an ACL containing manual or recovery routes, use the preservation
preview with a separately reviewed baseline supplied through protected stdin:

```sh
linode-image-lab --config /protected/acl-sync.toml acl-sync --output-format summary \
  --preserve-from-stdin < /protected/acl-baseline.json
```

The input is a JSON object with `schema_version: 1`, the exact `cluster_id` and
`cluster_label`, and an `acl` object containing the enabled state, both address
lists and an observed nonempty `revision-id`. Include `managed_ipv4`, a list of
IPv4 host routes in that baseline with explicit management ownership, and
`max_managed_ipv4`, the reviewed positive cap for that list. Use an empty
`managed_ipv4` when all baseline entries are retained. A registry match alone
does not establish ownership. Keep the baseline in owner-controlled storage.

The preview requires the provider's current policy and enforcement revision to
match that baseline. It preserves every IPv6 entry and every IPv4 entry outside
the explicit managed list, including routes with unknown purpose. It proposes
replacing the managed list with fresh registry IPv4 host routes that are not
already covered by retained routes. It stops on a missing or invalid baseline,
drift, unsuitable registry data, or a proposed managed list above the cap.
The summary shows change counts and a failure reason where available. The
preview accepts neither execute nor first enablement.

## Qualification before recurring writes

1. Keep the staged Job suspended. Privately classify existing routes and review
   the cap, registry sources eligible for API access, allowable add/remove
   deltas, and fresh registry behavior. Initially retain any route with
   unresolved ownership. Run the preview with read access
   against a fresh provider baseline, then observe a real registry transition
   in preview mode. Verify every planned removal against the reviewed managed
   set. Keep IPv6 policy unchanged.
2. Give the scheduling owner a protected baseline record outside the cluster.
   Start it with the separately verified full policy, revision and managed
   list. Journal the baseline, planned policy and a distinct forward revision
   before writing. Advance the baseline only after exact readback confirms
   enforcement. A pending journal, lost acknowledgement, or unknown provider
   state blocks later runs until reconciled, including a restart after a PUT.
3. Establish exclusive writer ownership and a provider-management session
   available without Kubernetes. Before an approved write, revalidate the
   selected registry content and compare the complete effective ACL and
   revision with the journaled baseline. Use one dedicated ACL PUT. The
   provider's revision marks enforcement and supplies no documented precondition
   for the PUT. Observe GET until both the requested revision and the complete
   policy match, within a bounded deadline. Treat timeouts or contradictory
   state as unresolved, stop new writes, and inspect readback before retry.
   See the `put-lke-cluster-acl` and `get-lke-cluster-acl` operations in the
   [Linode API specification](https://github.com/linode/linode-api-openapi/blob/main/openapi.json).
4. Qualify an authorized operator's provider ACL write role and protected
   credential custody outside LKE, and rehearse restoration on a synthetic or
   otherwise approved target. First stop the scheduler and isolate writers.
   Restore the full pre-change policy through the dedicated provider endpoint
   only when readback still matches the confirmed forward policy and revision.
   Require a new rollback revision and exact policy on GET. If provider state
   differs or the forward request remains uncertain, resolve that live state
   before attempting restoration. Check authorized Kubernetes reachability
   from a retained route independently of provider management.
5. Observe registry validity, last successful preview, last verified
   enforcement, drift, and uncertain or failed runs through an observer
   available outside the cluster. Emit counts and fixed categories. Alert
   separately on missing observations and stop scheduling writes after drift or
   unresolved enforcement. Review recurring removal, the exact deployment,
   and its access separately before enabling execution.
