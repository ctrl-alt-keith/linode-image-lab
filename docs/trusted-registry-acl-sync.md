# Trusted registry to LKE control-plane ACL

`acl-sync` reads the [Trusted Network Registry v1 contract](https://github.com/ctrl-alt-keith/trusted-network-registry/blob/main/docs/registry-schema.md)
from Object Storage and targets one LKE control-plane ACL by cluster ID and
label. Use the registry reader's `LINODE_OBJ_ACCESS_KEY` and
`LINODE_OBJ_SECRET_KEY` and a `LINODE_TOKEN` with the required ACL access. Keep
full manifests, CIDRs, identifiers, and review proofs in protected operator
evidence. Use `--output-format summary` for routine logs.

## Provider writes

Linode's ACL PUT takes the enabled state and complete address lists. Establish
one writer because the API supplies no documented compare-and-swap. Confirm
enforcement by GET with both the expected policy and the new revision. Follow
the recovery procedure for the writer that made the request.
[Linode ACL operations](https://github.com/linode/linode-api-openapi/blob/main/openapi.json)

The plain CLI plans by default. It replaces both ACL lists with registry
entries, dropping any extra manual or recovery routes. Use the
[preservation preview](#previewing-route-preservation) to assess retained access.
For an approved plain write, use `--execute`. Add `--allow-enable` only for
approved first enablement. The command rechecks cluster, registry and ACL before
PUT and stops on drift or invalid, stale, empty, or universal-allow input. It
reports HTTP 400, 401, 403 and 404 as `rejected`. Treat `submitted` as awaiting
readback. If the outcome is uncertain, inspect the ACL and confirm the previous
request settled before writing again.

```sh
linode-image-lab --config /protected/acl-sync.toml acl-sync --output-format summary
```

For optional failure detail, pass `--provider-error-file` with `--execute`.
Use a new path under a mode `0700` directory owned by the calling user. An unsafe
path stops before PUT. A write attempt creates a mode `0600` record that may
contain private data. Keep it private.

## Previewing route preservation

Use `--preserve-from-stdin` with a protected JSON baseline. Include
`schema_version: 1`, the exact `cluster_id` and `cluster_label`, and the full
enabled `acl` with both address lists and its enforced `revision-id`. List only
explicitly managed IPv4 host routes in `managed_ipv4` and set the approved
positive `max_managed_ipv4` cap. Use an empty managed list to retain the whole
baseline. Registry membership alone conveys no ownership.

The baseline must match the effective provider policy and revision. The preview
keeps all IPv6 and unmanaged IPv4 and proposes fresh registry IPv4 hosts outside
retained coverage, subject to the cap. Use this mode without execute or first
enablement. For qualification add `--registry-version-file` with the
[protected selection format](trusted-registry-firewall-sync.md#dry-run-workflow).
Verify `version_proof` against the independently selected fresh version of the
exact configured key. A missing or mismatched version header stops the preview
without fallback. Currentness requires a separate check.

## Guarded IPv4 reconciliation

Use summary output for this guarded operation.

```sh
linode-image-lab --config /protected/acl-sync.toml acl-sync --output-format summary \
  --reconcile-from-stdin --registry-version-file /protected/selection.json \
  < /protected/reconciliation.json
```

Use the same schema version, target, and enabled ACL baseline as in the
[preservation preview](#previewing-route-preservation). In protected input
supply the following fields.

- `slots` maps two approved opaque identifiers to `wan1` and `wan2`.
  `observed` uses the same keys with one independently verified public `ipv4`
  host CIDR per entry. Fresh Meraki registry discovery must match both pairs.
- `registry_version_proof` and `source_observations_valid_until` bind the
  observation fields to the exact registry version and the earlier source
  expiry. Use a timezone-aware ISO 8601 deadline. Forward execution requires
  it. For Meraki device-report freshness, include the binder's separate
  `source_device_reported_at` field and retain the protected input for review.
- `retained` initially copies both baseline lists and `checkpoint` is
  `null`. Later, use the prior enforced receipt's `intent` and `outcome` as
  the checkpoint. Only its managed IPv4 routes may be retired. Preserve manual,
  recovery and unresolved routes and the literal IPv6 list. A WAN host already
  covered by retained access stays covered by that route.

Use `acl_reconcile.bind_wan_observations` with the independently approved
appliance/interface mapping and age limit. Registry v1 lacks appliance
identity. Supply verified per-uplink sample times when using that evidence.
For Meraki API evidence, pass the exact approved appliance's record from
[MX status](https://developer.cisco.com/meraki/api-v1/get-organization-appliance-uplink-statuses/)
as `meraki_device_status`. Build the two observations from the separate
[Meraki addresses GET](https://developer.cisco.com/meraki/api-v1/get-organization-devices-uplinks-addresses-by-device/)
with `observed_at=None`. Mixed freshness inputs stop the binder. It checks the
serial and both interface/public IPv4 pairs against the status record, then
corroborates them against the exact registry version. Use the binder's fields
with the same selection. The deadline is the device-level `lastReportedAt`
plus the approved age limit. Keep that timestamp distinct from per-uplink sample,
Dashboard retrieval, and registry publication times. Summary output labels
this basis `meraki_device_last_reported_at`. Use a synchronized runner clock.
On a stale, future, or mismatched device report, stop and recollect both GETs
before planning again.

For an in-memory guarded preview, use `acl_reconcile.reconcile_acl` with
`rollback=False`, `execute=False`, `receipt_dir=None` and the protected
selection. For an all-retained preview using a fresh Meraki addresses GET
without sample times, use `acl_reconcile.preview_dashboard_wans` with the
approved mapping, protected baseline and exact selection. Its
`dashboard_fetched_at` records retrieval time.

On approval, add `--execute --acl-receipt-dir` with the owner-only directory
shared by the writer and recovery operator. Execution checks that the version
is still current, records exclusive intent for the baseline revision, and
rechecks the full ACL and observation deadline before one PUT. The receipt's
second line records verified enforcement. Preserve an intent without that line
and reconcile it before another run, including after a restart.

For guarded recovery, stop scheduling and isolate writers. Use the first line
of the protected forward receipt with
`acl-sync --rollback-from-stdin --output-format summary` and the same exact
cluster ID and label. Preview before executing with the same receipt directory.
Proceed only when readback matches the complete forward policy and revision and
the IPv6 lists agree. Use the provider management API independently of Kubernetes
and the registry. Verify the prior policy with a new revision and use the
enforced recovery receipt as the next checkpoint. Check Kubernetes reachability
from a retained route independently.

## Guarded writer qualification

Keep the staged CronJob suspended. Start with all routes retained for the guarded
writer. Review source ownership and caps, then observe a real registry
transition in preview. Verify each proposed removal against the reviewed
managed set. Journal the baseline, planned policy and distinct forward
revision outside LKE before writing. Advance the checkpoint after verified
enforcement. Verify both WANs provide the intended access. Qualify off-cluster
provider recovery authority and custody. Rehearse on a synthetic or approved
target.

Before scheduling the guarded writer, review recurring removals and qualify the
deployment, scoped access and exclusive writer session. Hold writes on drift or
unresolved enforcement. Apply [scheduled monitoring](#scheduled-monitoring).

## Exact-set WAN preview

Use `acl_reconcile.preview_exact_wan_acl` in memory with the exact cluster ID
and label, an approved appliance/interface mapping, a fresh authorized Meraki
addresses GET, the protected registry reader and selection, and a provider
GET-only client. After the effective ACL read, verify the selected immutable
registry version is still current and unchanged. Require one fresh, distinct
public IPv4 host from each approved Meraki discovery slot and corroborate both
with the dashboard read. The registry supplies desired state. Linode supplies
effective state.

The proposal contains exactly those two IPv4 hosts. Count every extra live
route as a removal, including networks that cover a desired host. Exclude all
registry IPv6 and any IPv4 outside the approved discovery slots. Report
hypothetical live IPv6 removals separately and block cutover while live IPv6
entries exist. Use counts and fixed categories for routine review. Keep the
selection, key and `review_proof` in the protected session.

### Reviewed single cutover

Keep the CronJob suspended and exclude other writers. Confirm independent
provider-management recovery authority without Kubernetes or the registry.
Preview just after a confirmed publish. Compare the protected live ACL and
validated WAN set for that preview to approve the exact removals. Check that
the approved version is still current before invoking
`acl_reconcile.run_supervised_exact_wan_cutover` from the reviewed checkout.
Pass the protected selection and `review_proof` from that preview. A new
version or changed policy requires a fresh preview and approval before writing.

### Recurring reconciliation

Qualify the deployment and scoped credentials. Require non-overlapping runs
and no other writer.

```sh
linode-image-lab --config /protected/acl-sync.toml acl-reconcile-wans --execute-after-verified-cutover
```

The required flag attests to the operator's verification. The command cannot
establish that the supervised cutover occurred. Add the flag to the scheduled
invocation only after readback confirms the cutover revision and the exact two
validated WAN IPv4 hosts with no live IPv6 entries.

Use the existing `[acl-sync]` cluster and registry settings and the S3 and
Linode credentials above. Supply the approved appliance through
`MERAKI_ORGANIZATION_ID` and `MERAKI_DEVICE_SERIAL` and the existing read key
through `MERAKI_DASHBOARD_API_KEY`. Keep these inputs protected. The command
corroborates `wan1` and `wan2` across the Meraki status and addresses GETs for
that appliance and selects the current immutable S3 version in memory. Exit
zero means an enforced change or verified no-op. A failed, rejected or
uncertain attempt exits nonzero.

The exact set removes every extra IPv4 route, including manual recovery
routes. Suspend scheduling before manual recovery. Apply
[scheduled monitoring](#scheduled-monitoring).

The recurring command and supervised cutover read fresh Meraki and Linode data,
validate the current registry version, and block on live IPv6. A matching
policy sends no PUT. Otherwise reread the live ACL and start at most one PUT
within five minutes of receiving the Meraki addresses response. When using
Meraki device-report evidence, validate its age during planning and again
before PUT against the same write window. If either deadline expires during
preflight, stop before PUT and recollect both GETs. Read back the enforced
policy and revision.
Report rejected or uncertain writes and let the next run recompute from fresh
inputs. Never retry the previous payload.

### Scheduled monitoring

Monitor verified enforcement, drift, and failed or missed runs outside LKE.
Distinguish silence from planned suspension. Emit counts and fixed categories.
