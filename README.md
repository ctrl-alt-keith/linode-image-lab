# Linode Image Lab

Safe, repeatable Linode image capture and deploy validation with automatic cleanup.

- Plans capture, deploy, capture-deploy, and capture-replicate-deploy runs
  before any API mutation.
- Captures custom images from temporary Linode instances.
- Deploys temporary validation instances from custom images.
- Submits explicit custom image replication requests when requested.
- Generates and validates public-safe region policy artifacts that separate
  provider region facts from operator-owned grouping intent.
- Validates requested region, Linode type, image inputs, tags, resources, and
  running status at the API level.
- Cleans up temporary resources while preserving custom images as deliverables.
- Emits redacted, public-safe manifests for review and automation.

## Quick Start

Requires Python 3.12 or newer.

Set `LINODE_TOKEN` first; execute mode reads it from the environment.
Plain dry-run commands do not need it, except `firewall-sync` because it reads
the target firewall before planning changes.

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
linode-image-lab capture-deploy \
  --region us-east \
  --source-image linode/alpine3.23 \
  --type g6-nanode-1 \
  --execute
```

That command creates a temporary capture source, captures a custom image, boots
a temporary validation instance from it, validates the result through the Linode
API, cleans up the temporary instances, and preserves the custom image.

## Installation

Install a released tag directly from GitHub:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install git+https://github.com/ctrl-alt-keith/linode-image-lab.git@vX.Y.Z
linode-image-lab --help
```

Replace `vX.Y.Z` with the desired release tag.

## Release Recovery

If `make release-publish VERSION=X.Y.Z` pushes `vX.Y.Z` but fails before
`gh release create` completes, inspect the partial state before rerunning any
release command:

```sh
make release-recover VERSION=X.Y.Z
```

The recovery target reports whether the local tag, remote tag, and GitHub
release exist. It does not create, delete, move, or push tags.

If only the local tag exists and the publish should be retried from scratch, the
target prints the exact manual local deletion command:

```sh
git tag -d vX.Y.Z
```

If the remote tag exists but the GitHub release is missing, create the release
from the existing remote tag without creating, deleting, or moving tags:

```sh
make release-create-from-tag VERSION=X.Y.Z
```

Do not delete, recreate, or force-push a public release tag during this recovery
path. If the pushed tag points at the wrong commit, stop and handle it as an
explicit release correction.

Run from source:

```sh
git clone https://github.com/ctrl-alt-keith/linode-image-lab.git
cd linode-image-lab
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
linode-image-lab --help
```

No install fallback:

```sh
git clone https://github.com/ctrl-alt-keith/linode-image-lab.git
cd linode-image-lab
PYTHONPATH=src python3 -m linode_image_lab.cli --help
PYTHONPATH=src python3 -m linode_image_lab.cli capture-deploy \
  --region us-east \
  --source-image linode/alpine3.23 \
  --type g6-nanode-1
```

## Authentication

`LINODE_TOKEN` is required when `--execute` is used and when `cleanup
--discover` is used. Dry-run commands, including plain `cleanup` and
`replicate` and `capture-replicate-deploy`, do not read the token or mutate
resources. `region-policy generate`, `region-policy validate`, and
`capture-replicate-deploy` runs that resolve `deploy_groups` or
`replication_groups` read public provider region metadata from the Linode
regions API without account authentication and do not mutate resources.

Use any shell method that exports the variable:

```sh
export LINODE_TOKEN='<your-linode-api-token>'
```

With 1Password, inject the token for a single command:

```sh
LINODE_TOKEN='op://Private/Linode API Token/credential' op run -- \
  linode-image-lab capture-deploy \
    --region us-east \
    --source-image linode/alpine3.23 \
    --type g6-nanode-1 \
    --execute
```

## Live Smoke Test

`make smoke` is a manual-only validation target for exercising the full
`capture-deploy --execute` path against real Linode APIs. It is intentionally
separate from `make check` and should not be wired into CI, schedules, or other
automation.

The target requires both `LINODE_TOKEN` and the explicit opt-in variable
`SMOKE_EXECUTE=1`. Without both gates, it exits before running the mutating
command. When enabled, it prints this warning:

```text
WARNING: This will create and delete temporary Linodes
```

Then it runs the known-good smoke config:

```sh
linode-image-lab capture-deploy --config examples/config/capture-deploy-smoke.toml --region us-sea --execute
```

The default smoke region is `us-sea`. Run it only when you are ready to create
billable temporary Linodes and preserve one custom image deliverable:

```sh
export LINODE_TOKEN='<your-linode-api-token>'
SMOKE_EXECUTE=1 make smoke
```

Override the region explicitly when needed:

```sh
SMOKE_EXECUTE=1 REGION=us-lax make smoke
```

Expected output is the warning followed by a redacted JSON manifest. A successful
single-region smoke run reports `status: "succeeded"`, includes nested
`capture` and `deploy` sections, records deleted temporary capture and deploy
Linodes in cleanup data, and preserves one custom image as the deliverable.
Provider or validation failures are reported with public-safe symbolic targets
and sanitized failure reasons; normal stdout must not expose provider resource
identifiers.

## Geo Replication Smoke Configs

`examples/smoke/` contains operational `capture-replicate-deploy` smoke configs
for bounded provider validation against the checked-in region policy. They are
safe by default because a config run is still a dry run unless `--execute` and
`LINODE_TOKEN` are both provided.

The geo replication smoke configs intentionally use one explicit
`deploy_regions` entry for capture and deploy, plus a checked-in geo
`replication_groups` entry for image availability. They validate the
replication policy surface without broad full-geo deploy fan-out. `deploy_groups`
expansion remains covered by dry-run behavior and unit tests; every smoke
execute does not need to deploy to every region in a geo group.

APAC North and Oceania currently have deploy geo groups but no executable
replication smoke config because the checked-in policy has no known-good geo
image-replication group for either geo. Keeping those configs out of
`examples/smoke/` avoids accidental fail-closed execute runs caused by the
backwards-compatible no-replication-input default.

`examples/geo/` contains broader deploy validation configs that use
operator-owned `deploy_groups`. Geos with known-good image-replication groups
also set `replication_groups`; deploy-only geos set `replication_enabled =
false` so the backwards-compatible replication default does not turn deploy
targets into replication targets.

## Config Defaults

Pass `--config` before or after the command to load optional TOML execution
defaults. Most scalar config values fill omitted CLI flags; explicit CLI flags
override those scalar defaults.

```sh
linode-image-lab --config examples/config/capture-deploy-smoke.toml capture-deploy
linode-image-lab capture-deploy --config examples/config/capture-deploy-smoke.toml --execute
linode-image-lab config validate --config examples/config/capture-deploy-smoke.toml --command capture-deploy
```

Config uses `schema_version = 1` with optional `[defaults]`, `[capture]`,
`[deploy]`, `[capture-deploy]`, `[capture-replicate-deploy]`, `[replicate]`,
`[cleanup]`, and `[firewall-sync]` tables.
Supported values are
`region` or `regions`, `ttl`, `source_image`, `image_id`, `type` or
`instance_type`, `image_project_tag`, `firewall_id`, `authorized_keys`,
`authorized_keys_file`, and `user_data_file`, depending on the command and
table. `[capture-replicate-deploy]` also accepts `deploy_regions`,
`deploy_groups`, `replication_regions`, `replication_groups`,
`replication_enabled`, and `region_policy_file`.
`region` or `regions` remain supported there as legacy deploy-region aliases.
`[capture].image_project_tag` and
`[capture-deploy].image_project_tag` set only the captured custom image's
artifact-facing `project=<value>` tag; temporary Linode lifecycle tags remain
owned by `linode-image-lab`. A non-default image project tag places the
captured image outside standalone cleanup ownership and discovery.
`image_project_tag` is config-only and has no CLI override.
`ttl` may be an absolute ISO-8601 timestamp or a relative duration such as
`"4 hours"`, `"1 day"`, `"30m"`, `"24h"`, `"7d"`, or `"2w"`. Relative TTLs
are resolved at command runtime and manifests still emit absolute UTC `ttl`
values and `ttl=...` tags.

## Region Policy Artifacts

Region policy artifacts keep provider facts separate from operator intent.
Generated `[provider_regions.*]` sections contain public-safe provider region
ids and capabilities from the current Linode regions API. Generated
`[generated_groups.*]` sections are overwrite-safe convenience scaffolding
derived from provider capabilities and provider country codes. Base generated
country groups such as `country_us` represent all provider regions for that
country code. Capability-scoped country groups such as
`country_us_object_storage` represent only regions in that country exposing the
named capability. Image-replication country groups such as
`country_us_image_replication` are narrow workflow-specific convenience
scaffolding derived from Object Storage regions minus documented
`provider_overrides.image_replication_excluded_regions` entries. The override
table does not rewrite provider facts or capability groups; it records known
provider discrepancies for image-replication helper groups only. When every
Object Storage region for a generated country image-replication helper is
excluded, that helper group is omitted rather than emitted empty.
Operator-maintained `[groups.*]` sections name semantic region groups for
local workflows and remain the canonical intent layer.

Generate or refresh the default version-controlled artifact:

```sh
linode-image-lab region-policy generate --output policy/region-policy.toml
```

When the output file already exists, generation refreshes provider facts and
generated helper groups while preserving supported `provider_overrides.*` and
`groups.*` tables. Use `--replace-groups` only when you intentionally want to
drop operator-owned groups. Use `--output -` to print the TOML without writing
a file.

The repository intentionally includes `policy/region-policy.toml` as the full
current generated provider policy snapshot. It is versioned so operators can
rerun generation periodically and review the diff for provider region or
capability drift. The generated provider and helper sections stay generated;
the checked-in snapshot also carries deliberately documented, hand-maintained
operator-owned geo groups under `groups.*`. Documented `provider_overrides.*`
entries are allowed only for narrow provider-discrepancy handling such as
image replication exclusions.

## Maintaining Region Policy Artifacts

Use normal source control review for provider drift:

```sh
linode-image-lab region-policy generate \
  --output policy/region-policy.toml
linode-image-lab region-policy validate \
  --path policy/region-policy.toml
git diff -- policy/region-policy.toml
```

`policy/region-policy.toml` is intentionally versioned. Regeneration updates
`provider_regions.*` and `generated_groups.*` from current public provider
metadata. Documented `provider_overrides.*` remain preserved and are applied
only to workflow-specific helper groups that explicitly name that behavior.
Operator-owned `groups.*` remains preserved unless `--replace-groups` is used.
Generation and validation do not require `LINODE_TOKEN`, read no
account-specific data, and perform no provider mutations.

Validate the artifact against current provider metadata:

```sh
linode-image-lab region-policy validate --path policy/region-policy.toml
```

Validation emits sanitized JSON, fails closed on malformed TOML, unknown or
missing provider regions, stale provider capabilities, stale generated groups,
malformed or stale provider overrides, and generated or operator groups that
reference regions missing from current provider metadata.

The policy file is deliberately not an automatic placement engine. The tool
does not infer geography, measure latency, choose nearest regions, plan
fallback placement, execute replication policy, or reconcile long-lived
resource declarations from these groups. Generated groups are starting points,
not policy. Workflow-specific generated groups can encode narrow documented
provider discrepancies, but execution validation remains authoritative.
Operators own the meaning of each operator group, and later commands can
validate against that explicit local intent.

`capture-replicate-deploy` can consume these artifacts for deploy target
expansion and replication target selection. When
`[capture-replicate-deploy].deploy_groups` or `replication_groups` is
configured, the command uses `policy/region-policy.toml` by default; set
`region_policy_file = "policy/staging-region-policy.toml"` only when an
alternate checked-in artifact is intended. Group names resolve first from
operator-owned `groups.*`, then from generated `generated_groups.*`. Deploy
regions and `deploy_groups` are deploy intent. Replication regions and
`replication_groups` are image-availability intent. Deploy targets are not
automatically replication targets when replication input is configured, and
replication targets are not automatically deploy targets. When no replication
regions or groups are configured, resolved deploy targets remain the
backwards-compatible default replication target set.
`replication_enabled = false` is the explicit opt-out for deploy-only
validation. When set, replication target resolution, capability checks,
replication API calls, and replica readiness waits are skipped; deploy targets
do not become implicit replication targets. Generated capability-scoped and
image-replication groups improve discoverability, but they do not bypass
execution validation. Execute mode still validates every resolved replication
target for `Object Storage` and fails before mutation if any requested target is
invalid whenever replication is enabled. Under the checked-in policy,
`geo_apac_north` currently has deploy targets but no corresponding known-good
`geo_apac_north_image_replication` group because `jp-tyo-3` is a documented
image-replication provider discrepancy.

Deploy metadata defaults are field-specific. `firewall_id` is a scalar default
for deploy instances. Authorized keys are additive: configured
`authorized_keys`, configured `authorized_keys_file`, repeated
`--authorized-key`, and `--authorized-keys-file` inputs are merged and deduped
instead of replacing each other. `[deploy].authorized_keys` and
`[deploy].authorized_keys_file` also feed the deploy phase of `capture-deploy`;
`[capture-deploy]` can add command-specific keys. The same rule applies to
`capture-replicate-deploy` with `[capture-replicate-deploy]`.
`[deploy].user_data_file` provides deploy-scoped Linode metadata user data for
`deploy` and for the deploy phase of `capture-deploy` and
`capture-replicate-deploy`; `--user-data-file` overrides it when provided.
There is no `[capture-deploy].user_data_file` or
`[capture-replicate-deploy].user_data_file`. File inputs are explicit; the tool
never discovers keys or user data.

`capture-deploy --execute` accepts multiple regions through repeated
`--region` flags or `regions = [...]` config. It captures one custom image in
the first requested region, then deploys that captured image to each requested
region concurrently with a bounded worker pool capped at 4 deploy workers.
Linode custom images are deployable across regions; the public docs do not
specify cross-region deploy latency. Operators should expect farther-region
deploys may take longer, but the tool does not depend on that timing.
Standalone `capture --execute` and `deploy --execute` remain single-region
only.

`replicate --execute` accepts multiple regions and submits one explicit custom
image replication request. Because the Linode API request represents the
complete region set for an image, execute mode first reads the image's existing
regions and verifies that each requested replication target exposes the
provider `Object Storage` capability, then submits existing-plus-requested
regions. If existing regions are not exposed, or a requested target lacks that
capability, the command fails before mutation. Replicate dry-run output models
the requested regions and records that execute mode will preserve
provider-reported existing regions, but it does not read `LINODE_TOKEN` or call
Linode.

`capture-replicate-deploy --execute` captures one custom image in the first
resolved deploy target, resolves deploy targets from explicit deploy regions
and checked-in `deploy_groups`, resolves replication targets from explicit
`replication_regions`, checked-in `replication_groups`, or both, then deploys
from the captured image only to resolved deploy targets after bounded
read-only status checks show the resolved image regions are `available`. For
country-based image replication, generated groups such as
`country_us_image_replication` are the ergonomic starting point because base
country groups include all provider regions for that country, and
`country_us_object_storage` intentionally mirrors provider Object Storage
metadata even when a provider discrepancy is documented. The checked-in
provider discrepancy exclusions currently include `au-mel`, `de-fra-2`,
`fr-par-2`, `gb-lon`, `jp-tyo-3`, `sg-sin-2`, and `us-iad-2` for
image-replication helper groups only. Before creating the capture Linode, it
validates any configured region policy artifact and
verifies that each resolved replication target exposes the provider
`Object Storage` capability. The replication request preserves
provider-reported existing image regions plus resolved replication targets, so
the capture/original region is preserved through the provider-reported image
region set rather than by treating every deploy target as a requested
replication target. Deploy may still target a resolved deploy region outside
the requested replication targets, relying on the provider's existing
cross-region image deploy behavior. If a requested group is unknown,
malformed, stale, or references invalid regions, if a requested target lacks
`Object Storage`, if the image response does not expose existing regions, or if
requested replicas do not report available before the bounded wait expires, the
workflow fails closed, cleans up temporary resources when any were created, and
does not deploy. The captured custom image remains the workflow deliverable
under the same artifact-tag semantics as capture-deploy. Capability validation
records a check for every resolved target region before deciding whether the
workflow can proceed.

`config validate` parses the TOML file, applies the same safety checks as
command execution, and emits a non-mutating JSON report with `precedence`,
`effective_defaults`, and `sources`. The `precedence` list names the source
classes considered for the selected command, and `sources` shows which source
fed each effective field. It is not a single override rule for every field:
scalar fields use override precedence, authorized-key inputs merge and dedupe
additively, and user data remains deploy-scoped. For `capture-deploy` and
`capture-replicate-deploy`, scalar defaults come from explicit CLI flags, then
the command table, then `[defaults]`; authorized keys can come from `[deploy]`,
the command table, and CLI key inputs; user data can come from
`[deploy].user_data_file` or `--user-data-file`. You can pass supported CLI
default flags such as `--region`, `--ttl`, `--source-image`, `--image-id`, or
`--type` to preview config resolution for the selected command. For deploy
defaults, `--firewall-id`, `--authorized-key`, `--authorized-keys-file`, and
`--user-data-file` can also be previewed. For `capture-replicate-deploy`,
`--region` previews `deploy_regions`, and `--replication-region`,
`--replication-group`, and `--region-policy-file` preview policy input
resolution. Authorized key and user-data output reports safe metadata such as
count, source, and byte count only.

Config is only for execution defaults. It cannot contain `LINODE_TOKEN`, token
values, passwords, private SSH keys, inline cloud-init data, `execute`,
`discover`, preservation flags, or run id fields. `--execute` or
`cleanup --discover` must still be passed explicitly, and `LINODE_TOKEN` must
still come from the environment or approved environment injection.

`firewall-sync` adds non-secret registry fields under `[firewall-sync]`:
`registry_endpoint_url` (HTTPS only), `registry_bucket`, `registry_object_key`,
`registry_region`, `protocol`, `ports`, and `managed_label`. Object Storage
credentials must come only from `LINODE_OBJ_ACCESS_KEY` and
`LINODE_OBJ_SECRET_KEY`.

## Trusted Registry Firewall Sync

`firewall-sync` consumes a private Trusted Network Registry JSON document from
Linode Object Storage and plans one managed inbound allow rule on an existing
Linode Cloud Firewall. It validates the registry before use, rejects stale
registries, supports IPv4 and IPv6 CIDRs, rejects universal allow CIDRs, and
does not fall back to stale or local defaults.

Dry-run example:

```sh
export LINODE_TOKEN='<linode-api-token>'
export LINODE_OBJ_ACCESS_KEY='<object-storage-access-key>'
export LINODE_OBJ_SECRET_KEY='<object-storage-secret-key>'
linode-image-lab --config examples/config/firewall-sync.example.toml firewall-sync
```

Execute requires an explicit flag:

```sh
linode-image-lab --config examples/config/firewall-sync.example.toml firewall-sync --execute
```

The managed firewall rule is identified by exact label and description. Rules
outside that marker are preserved; ambiguous ownership fails closed. See
[docs/trusted-registry-firewall-sync.md](docs/trusted-registry-firewall-sync.md)
for rollback notes, stale-registry behavior, and log-safety cautions.

## Behavior Clarifications

- All commands are dry-run by default.
- `--execute` enables real Linode API mutations for `capture`, `deploy`,
  `capture-deploy`, `capture-replicate-deploy`, `replicate`, `cleanup`, and
  `firewall-sync`.
- Existing resources passed to commands remain operator-owned inputs.
  `firewall-sync` is limited to one labeled inbound allowlist rule on an
  existing Cloud Firewall, not broader firewall management.
- Provider behavior assumptions are tracked in
  [docs/provider-assumptions.md](docs/provider-assumptions.md).
- Scalar config values fill omitted command options; CLI scalar flags override
  config, while authorized-key inputs merge and dedupe additively.
- Execute runs use temporary resources and clean them up automatically unless a
  preservation flag is used.
- Execute runs verify the requested region, Linode type, source or deploy
  image, and configured firewall with read-only Linode API calls before
  resource creation.
- Deploy user data is read only from explicit files, Base64 encoded for Linode
  `metadata.user_data`, and omitted from manifests except for safe metadata.
- A non-default `image_project_tag` keeps deliverable custom images outside
  standalone cleanup ownership and discovery.
- `cleanup` is independently runnable, dry-run by default, and can delete
  expired tagged temporary Linodes and lab-owned custom images only with
  `--execute`.
- Normal stdout is redacted for public-safe review.
- `--manifest-file PATH` writes an atomic copy of the same redacted manifest
  emitted on stdout for `capture`, `deploy`, `capture-deploy`,
  `capture-replicate-deploy`, `replicate`, and `cleanup`; `--manifest-file -`
  keeps stdout-only behavior.

## What This Does

- Captures custom images from temporary Linode instances.
- Explicitly replicates captured images when using the
  capture-replicate-deploy operator path.
- Deploys temporary validation instances.
- Validates region, Linode type, image inputs, tags, resources, and running
  status at the API level.
- Cleans up temporary resources.

## What This Does Not Do

- SSH, cloud-init, service, or application-level validation.
- Manage long-lived infrastructure or broader firewall configuration.
- General-purpose multi-region orchestration outside bounded
  `capture-deploy --execute` and `capture-replicate-deploy --execute`
  validation runs.

## Commands

Dry-run previews:

```sh
linode-image-lab plan --region us-east --mode capture-deploy
linode-image-lab capture --region us-east
linode-image-lab deploy --region us-east
linode-image-lab capture-deploy --region us-east
linode-image-lab capture-replicate-deploy --config examples/config/capture-replicate-deploy.example.toml
linode-image-lab cleanup
```

Execute capture:

```sh
linode-image-lab capture \
  --region us-east \
  --source-image linode/alpine3.23 \
  --type g6-nanode-1 \
  --execute
```

Execute deploy from an existing custom image:

```sh
linode-image-lab deploy \
  --region us-east \
  --image-id "$CUSTOM_IMAGE_ID" \
  --type g6-nanode-1 \
  --firewall-id "$FIREWALL_ID" \
  --execute
```

Execute capture plus deploy validation:

```sh
linode-image-lab capture-deploy \
  --region us-east \
  --source-image linode/alpine3.23 \
  --type g6-nanode-1 \
  --firewall-id "$FIREWALL_ID" \
  --execute
```

Execute capture, explicit replication, and deploy validation:

```sh
linode-image-lab capture-replicate-deploy \
  --region us-sea \
  --region us-east \
  --source-image linode/alpine3.23 \
  --type g6-nanode-1 \
  --firewall-id "$FIREWALL_ID" \
  --execute
```

Preview or execute tag-scoped cleanup:

```sh
linode-image-lab cleanup
LINODE_TOKEN='<your-linode-api-token>' linode-image-lab cleanup --discover
LINODE_TOKEN='<your-linode-api-token>' linode-image-lab cleanup --execute
```

Plain `cleanup` never reads `LINODE_TOKEN`, calls Linode, or deletes resources.
`cleanup --discover` requires `LINODE_TOKEN`, performs read-only Linode
resource discovery, and reports expired eligible Linodes and lab-owned custom
images in `cleanup_candidates` without deleting them. `cleanup --execute`
requires `LINODE_TOKEN`, lists managed resources, and deletes only expired
resources carrying the complete required tag set. Use `--run-id` to restrict
discovery or deletion to one run.

## Required Tags

Modeled resources use rediscoverable tags:

- `project=linode-image-lab`
- `run_id=<unique-id>`
- `mode=<capture|deploy|capture-deploy|capture-replicate-deploy|replicate>`
- `component=<capture|deploy|replicate>`
- `ttl=<absolute-utc-timestamp>`

`ttl` is a project-internal cleanup tag used by this tool. Linode does not
enforce it as a provider-side expiration policy.

Captured custom images use separate artifact tags with the same `run_id`,
`mode`, `component`, and `ttl` metadata. By default the artifact project tag is
`project=linode-image-lab`, making expired images eligible for explicit
standalone cleanup. `[capture].image_project_tag`,
`[capture-deploy].image_project_tag`, or
`[capture-replicate-deploy].image_project_tag` can change the value after
`project=`. Images outside the default lab-owned project tag are ignored by
standalone cleanup discovery and are not deleted by standalone cleanup.

## Manifest Output

Execute manifests use consistent top-level `status`, `steps`, `resources`,
`validation`, and `cleanup` fields. For single-region `capture-deploy`,
top-level `resources`, `validation`, and `cleanup` summarize the combined run,
while nested `capture` and `deploy` blocks show phase-specific details.
Manifests expose internal cleanup ownership as `lifecycle_tags` and captured
custom image identity as `artifact_tags`; the legacy top-level `tags` field is
kept as a compatibility alias for `lifecycle_tags`. Consumers should treat
`lifecycle_tags` as the cleanup/validation tag contract and must not treat
`tags` as captured custom image tags.
Use `--manifest-file PATH` with `capture`, `deploy`, `capture-deploy`,
`capture-replicate-deploy`, `replicate`, or `cleanup` to persist the exact
redacted JSON string emitted on stdout. The parent directory must already
exist, and partial failure manifests are written when execution has a manifest
to report.
Multi-region `capture-deploy --execute` emits one combined manifest with
top-level `status`, `regions`, `capture`, `deploy_results`, and `summary`.
The nested `capture` value is the single capture manifest, and each
`deploy_results.<region>` value is the deploy manifest for that requested
region. Capture-deploy manifests include `component_tags.capture` and
`component_tags.deploy` so consumers can distinguish the lifecycle tag sets for
the capture-source and deploy-validation Linodes. When a firewall is
configured, deploy manifests include `deploy_config.firewall`; when authorized
keys are configured, deploy manifests include `deploy_config.authorized_keys`
count metadata only; when user data is configured, deploy manifests include
`deploy_config.user_data` source and byte count metadata only. Provider
identifiers, raw key material, and raw or encoded user data remain redacted in
normal stdout.

`capture-replicate-deploy --execute` emits one combined manifest with
top-level `capture`, `replication`, `deploy_results`, `validation`, `cleanup`,
and `summary` blocks. Dry-run manifests show the capture region, requested
deploy groups, resolved deploy target regions, requested replication groups,
the replication enabled/disabled state, policy file path when groups are used,
group source namespaces, resolved replication target regions, planned
capture/replication/deploy phases, cleanup expectations, and
`provider_calls: "not_attempted"`. Execute manifests record the policy
validation result, resolved replication targets when replication is enabled,
replication capability checks, capture result, replication request/result,
replica status checks, deploy results by resolved deploy target, validation
summary, cleanup summary, and final `status` of `succeeded`, `partial`, or
`failed`. When `replication_enabled = false`, the `replication` and
`validation.replication` blocks report `status: "skipped"` with reason
`replication_enabled=false`.

Multi-region status is `succeeded` when every requested deploy region succeeds
and capture cleanup completes, `partial` when some deploy regions fail or
capture cleanup fails after successful deploys, and `failed` when capture fails
or every deploy region fails. A failed deploy region does not block cleanup for
that region or completion of other deploy regions. Partial failures indicate
real provider/API errors, invalid inputs, transient issues, or unresolved
cleanup. Validation checks are objects with `name`, `status`, and a symbolic
`target`; failed checks include a sanitized `failure_reason`.

Cleanup status values are literal: `deleted` means a temporary Linode was
deleted, `preserved` means a resource was kept or skipped for safety,
`completed` means combined cleanup finished, and `failed` means cleanup did not
complete.

Standalone `cleanup --execute` does not delete untagged resources, images
outside the default lab-owned project tag, or resources with missing,
malformed, unexpired, or mismatched managed tags. It re-fetches each discovered
candidate before a single DELETE attempt. Only discovered lab-owned images can
appear as deleted, preserved, or failed cleanup entries. Preserved and failed
entries include `resource_type` plus a sanitized `reason`, such as
`ttl_not_expired`, `ttl_parse_failed`, `missing_required_tags`, or
`delete_status_unknown`.

## Known Limitations

- Disk selection: Capture currently requires exactly one suitable disk
  (non-swap, ready). Multi-disk sources are not supported.
- Cross-region deploy latency: Linode supports cross-region image deploy, but
  latency is not specified by provider docs.
- Replica readiness waits: `capture-replicate-deploy --execute` waits only for
  provider/API image region statuses to report `available`; it does not repair,
  retry, or own replicas after the run.
- Replication target eligibility: explicit image replication requires requested
  target regions to expose the provider `Object Storage` capability.
- Image replication provider discrepancies: `au-mel`, `de-fra-2`,
  `fr-par-2`, `gb-lon`, `jp-tyo-3`, `sg-sin-2`, and `us-iad-2` are
  documented exclusions from generated image-replication helper groups and
  matching operator image-replication groups in the checked-in policy. Raw
  provider facts and provider-backed capability groups remain unchanged.
- Region policy consumption: `deploy_groups` expand deploy targets, while
  `replication_groups` expand image availability only. They do not infer
  geography, choose nearest regions, plan fallbacks, bypass capability
  validation, or perform partial execution.
- Deploy-only geo validation: set `replication_enabled = false` to intentionally
  skip replication for broad deploy validation configs. Existing configs that
  omit the field keep the backwards-compatible default where deploy targets are
  replication targets when no replication input is configured.
- Retry semantics: Retry behavior for some HTTP statuses (e.g., 5xx) is a
  project policy, not a provider guarantee.
- Cleanup semantics: DELETE operations are single-attempt after re-fetch;
  ambiguous failures are reported rather than retried.

## Related Lab

`ctrl-alt-keith/linode-backup-lab` is the sibling public-safe lab for backup
validation, snapshot inspection, and future restore-drill validation. It keeps
that scope separate from this repo's image capture and deploy validation work.

## Independence and Intent

This is a personal, independent project. It is not affiliated with any employer
or organization.

It is designed as a public-safe workflow lab and does not use proprietary
systems, data, or credentials.

> AI-generated. Human-verified. Occasionally argued about.
