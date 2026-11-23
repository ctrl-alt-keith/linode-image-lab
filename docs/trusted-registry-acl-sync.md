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

For a qualification run, pass `--registry-version-file /protected/selection.json`
as well. Use the same per-attempt selection format as the
[firewall dry run](trusted-registry-firewall-sync.md). Require an independently
selected fresh version of the exact configured key. The reader sends a
version-specific GET, refuses a missing or different response version, and
does not fall back to latest. The summary emits a keyed `version_proof` for
private comparison with the selection, without printing the version or key.
The proof identifies the read version and does not establish that it is current.

## Guarded IPv4 reconciliation

Use `--reconcile-from-stdin` for a separate summary-only operation on two explicit
WAN slots. It requires the exact-version selection file. Start with a preview:

```sh
linode-image-lab --config /protected/acl-sync.toml acl-sync --output-format summary \
  --reconcile-from-stdin --registry-version-file /protected/selection.json \
  < /protected/reconciliation.json
```

The owner prepares the input in protected storage:

- `schema_version` is `1`. `cluster_id` and `cluster_label` identify the exact
  provider target. `acl` is the full enabled policy and enforced revision.
- `slots` maps two approved opaque device/uplink identifiers to `wan1` and
  `wan2`. `observed` maps those same identifiers to objects with one `ipv4`
  host CIDR apiece. Use independently qualified public WAN IPv4 host routes.
  The registry corroborates each interface and host through fresh Meraki
  discovery entries. Existing IPv6 entries keep their exact spelling and order.
  IPv6 observations are outside the guarded writer's scope.
  Registry v1 cannot identify a physical device by interface name or address.
- `registry_version_proof` is the keyed proof of the exact version associated
  with these independent observations, using the protected selection.
- `retained` contains the two address lists for manual, recovery, and unresolved
  routes. Initially copy both lists from the baseline and set `checkpoint`
  to JSON `null`. For later runs, `checkpoint` contains `intent` and `outcome`
  from the previous enforced receipt. Only its recorded managed IPv4 routes may
  be retired. A current observation covered by retained access stays retained.

The operation refuses incomplete or ambiguous observations, unsupported host
addresses, invalid ownership, stale inputs, and drift. When explicitly approved,
add `--execute --acl-receipt-dir /protected/acl-receipts`. Use one owner-only
directory shared by the sole writer and recovery operator. The command requires
the same version to still be current, durably creates an exclusive receipt for
the baseline revision, and rechecks the full ACL immediately before one PUT.
It reports enforcement only after provider GET returns the exact policy and new
revision. The receipt's first JSON line records intent and its second records
enforcement. An interrupted or uncertain attempt leaves the intent available.
Keep the receipt and reconcile the actual provider state before another run.

For recovery, send only the first JSON line of the protected forward receipt to
`acl-sync --rollback-from-stdin --output-format summary`, with the exact cluster
ID and label supplied by protected configuration. This uses the provider
management API independently of Kubernetes and Object Storage. Use a preview
before explicitly enabling execution with the same receipt directory. The
operation requires the complete forward policy and forward revision and
restores the prior policy with a new revision. The receipt is rejected if
the IPv6 lists differ. Use the verified rollback receipt
as the next checkpoint. A conflicting revision or unresolved provider state
halts recovery.

## Qualification before scheduling writes

1. Keep the staged Job suspended. Privately classify existing routes and review
   the cap, registry sources eligible for API access, allowable add/remove
   deltas, and fresh registry behavior. Initially retain all existing routes.
   Run the preview with read access
   against a fresh provider baseline, then observe a real registry transition
   in preview mode. Verify every planned removal against the reviewed managed
   set. Missing, ambiguous, or nonpublic IPv4 observations stop the run.
   Check that both approved uplinks serve the intended IPv4 access.
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
