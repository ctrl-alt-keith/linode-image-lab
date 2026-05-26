# Design

Linode Image Lab models the control-plane contract for custom image
capture/deploy workflows while keeping mutation paths explicit and narrow.

## Goals

- Provide a minimal CLI with deterministic JSON output.
- Create sanitized manifest previews for dry-run planning.
- Define a stable tag contract for resource rediscovery.
- Make cleanup selection testable without cloud access.
- Allow capture, deploy, and capture-deploy execution only after explicit
  opt-in; capture-deploy may fan out across requested regions.
- Allow explicit image replication submission only after explicit opt-in.
- Allow a bounded operator path that captures, explicitly replicates, waits for
  requested replicas to report available, and deploys from that captured image.
- Generate and validate a version-controlled region policy artifact that keeps
  provider region facts separate from operator-owned grouping intent.

## Non-Goals

- No implicit Linode API mutations.
- No GitHub Actions mutation, external scheduler integration, or
  general-purpose multi-region orchestration outside bounded
  capture-deploy and capture-replicate-deploy validation runs.
- No infrastructure ownership or planning model.
- No automatic geography inference, nearest-region logic, latency probing,
  fallback placement, auto-selection, partial execution, or long-lived resource
  reconciliation from region policy artifacts.
- CI exists to run `make check`.

## Execution Model Boundary

Configuration and multi-region support are execution defaults for disposable
validation runs. They must not turn the tool into infrastructure ownership.
Runs remain ephemeral validation workflows with explicit mutation entrypoints,
provider/API-level validation, tagged temporary resources, and cleanup as a
first-class outcome.

## Components

- `cli.py` owns command parsing and JSON output.
- `config.py` owns explicit TOML config loading and safe default resolution.
- `capture.py` owns dry-run capture manifests and execute-mode orchestration.
- `deploy.py` owns dry-run deploy manifests and execute-mode orchestration.
- `capture_deploy.py` owns the combined capture-deploy execute orchestration.
- `capture_replicate_deploy.py` owns the capture, explicit replication, and
  deploy execute orchestration.
- `replicate.py` owns dry-run replication manifests and execute-mode
  replication submission.
- `manifest.py` owns manifest creation, tag generation, and sanitized
  serialization.
- `region_policy.py` owns provider-backed region policy TOML generation and
  validation.
- `regions.py` owns one-or-many region parsing.
- `cleanup.py` owns tag-based cleanup candidate selection and standalone
  cleanup execution.
- `redaction.py` owns recursive output sanitization.
- `linode_api.py` owns the mockable Linode client boundary.

## Linode API Retry Boundary

The Linode API client retries only safe operation classes: non-mutating GET
preflight reads, polling GET reads, list/read validation calls, and managed
Linode discovery. Retries are bounded, use deterministic backoff without
jitter, and record public-safe retry event metadata without tokens or provider
identifiers.
For HTTP 429 rate-limit responses, the client honors Linode's documented
`Retry-After` header when valid, then falls back to `X-RateLimit-Reset` when it
can be parsed, before using the deterministic backoff.

Create-instance, image-create, image-replicate, shutdown, cleanup DELETE, and
other mutation requests remain single-attempt so transient failures cannot
create duplicate resources, repeat unsafe state transitions, or obscure
ambiguous outcomes.

## Manifest Foundation

Manifests are plain JSON-compatible dictionaries. They include schema version,
project, command, mode, regions, timestamps, dry-run status, lifecycle tags,
artifact tags, and planned actions. Serialized manifests use stable JSON
formatting and redacted provider identifiers. `lifecycle_tags` are the
cleanup/validation tag contract for temporary Linodes. `artifact_tags` are the
captured custom image tag contract. The schema-v1 `tags` field is a
compatibility alias for `lifecycle_tags`, not a generic tag bag.

M2 capture execution adds `execution_mode`, ordered `steps`, `resources`,
`capture_source`, `custom_image`, `validation`, and `cleanup` fields. M3 deploy
execution adds `execution_mode`, ordered `steps`, `resources`, `deploy_source`,
`deploy_instance`, `validation`, and `cleanup` fields. M4 capture-deploy
execution adds top-level `execution_mode`, `steps`, `resources`, `capture`,
`deploy`, `validation`, and `cleanup` fields. Top-level `resources` is the
combined resource list; nested `capture.resources` and `deploy.resources` are
phase-specific views. Top-level `validation` and `cleanup` are combined
summaries; nested validation and cleanup blocks preserve phase-specific status.
M6 replicate execution adds `execution_mode`, ordered `steps`,
`replication_source`, `replication_request`, `replication_result`,
`provider_response_summary`, `validation`, and `replica_status_polling` fields.
Capture-replicate-deploy execution adds top-level `capture`, `replication`,
`deploy_results`, `validation`, `cleanup`, and `summary` fields so the operator
path can report capture, replica readiness checks, per-region deploys, and
cleanup without durable state.
Validation checks record `name`, `status`, a symbolic `target`, and a sanitized
`failure_reason` when a check fails. Internal manifests may carry provider
resource identifiers required for cleanup and debugging. Normal stdout uses
sanitized serialization, which redacts provider identifiers before printing.

## Config Defaults

Config is opt-in through `--config PATH`, either before or after the command,
and uses TOML `schema_version = 1`. The config file can provide execution
defaults in `[defaults]`, `[capture]`, `[deploy]`, `[capture-deploy]`,
`[capture-replicate-deploy]`, `[replicate]`, and `[cleanup]` tables. Most
scalar defaults use override precedence: CLI flags,
then command-specific config, then `[defaults]`, then existing generated
defaults. Deploy metadata fields have narrower rules described below.

`config validate --config PATH --command COMMAND` provides the non-mutating
inspection path for config files. It validates the TOML schema and safety rules,
then emits redacted JSON containing the selected command's `precedence`,
`effective_defaults`, and `sources`. The `precedence` list names the source
classes considered for the selected command; `sources` is the per-field record
of the source or sources used. Supported CLI default flags passed to
`config validate` are included only for resolution inspection; the command does
not execute workflows, call Linode, or read `LINODE_TOKEN`.

Supported config values are intentionally narrow:

- `region` or `regions`,
- `ttl`,
- `source_image` for capture, capture-deploy, and capture-replicate-deploy,
- `image_id` for deploy and replicate,
- `type` or `instance_type` for capture, deploy, capture-deploy, and
  capture-replicate-deploy,
- `image_project_tag` for captured custom image artifact tags in capture and
  capture-deploy and capture-replicate-deploy,
- `firewall_id` for deploy instances in deploy, capture-deploy, and
  capture-replicate-deploy,
- `authorized_keys` and `authorized_keys_file` for deploy instances in deploy
  and the combined deploy workflows,
- `user_data_file` in `[deploy]` for Linode metadata user data on deploy
  instances.
- `deploy_regions`, `deploy_groups`, `replication_regions`,
  `replication_groups`, `replication_enabled`, and `region_policy_file` for
  capture-replicate-deploy policy-backed deploy and replication target
  resolution.

`image_project_tag` config is a value for the captured image's
`project=<value>` artifact tag, not a way to configure lifecycle tag keys, and
has no CLI override.
`ttl` accepts either an absolute ISO-8601 timestamp or a relative duration such
as `"4 hours"`, `"1 day"`, `"30m"`, `"24h"`, `"7d"`, or `"2w"`. Relative TTLs
are resolved during manifest generation against the current command execution
time; serialized manifests and lifecycle/artifact tags continue to carry
absolute UTC TTL timestamps.
`--execute`, preservation flags, run id fields, image labels, tokens,
passwords, private SSH keys, root passwords, inline metadata, and inline
cloud-init or user-data values are not configurable. Unknown keys and
secret-like keys fail before command execution. Authorized key and user-data
files are explicit paths supplied by config or CLI; the tool does not discover
keys from `~/.ssh` or user-data files. Raw authorized key contents and raw or
Base64-encoded user data are never serialized in manifests or config validation
output.

Config precedence is field-specific for deploy metadata. Scalar fields such as
`regions`, `ttl`, `source_image`, `image_id`, `type`, `image_project_tag`, and
`firewall_id` use override precedence. Authorized keys are additive:
`authorized_keys`, `authorized_keys_file`, `--authorized-key`, and
`--authorized-keys-file` inputs are merged and deduped. For `capture-deploy`
and `capture-replicate-deploy`, authorized keys from `[deploy]` feed the deploy
phase before the command table adds command-specific keys.
`[deploy].user_data_file` is deploy-scoped and feeds `deploy` and only the
deploy phase of the combined workflows; `--user-data-file` overrides it when
provided, and command-table `user_data_file` is not supported.

Multi-region config is accepted for dry-run manifests. Execute mode remains
single-region for `capture` and `deploy`; `capture-deploy --execute` accepts
multiple regions and runs one capture followed by bounded parallel deploy
attempts.
`replicate --execute` accepts multiple regions and submits one explicit image
replication request. Replicate config can provide `region`, `regions`,
`image_id`, and `ttl`.
`capture-replicate-deploy --execute` accepts multiple explicit deploy regions
and checked-in `deploy_groups`, captures in the first resolved deploy target,
and resolves replication target regions from explicit `replication_regions`,
checked-in `replication_groups`, or both. Deploy regions and `deploy_groups`
are deploy intent only and are not automatically added to resolved replication
targets when either replication input is configured. Replication targets are
not automatically added to deploy targets. When no replication regions or
groups are configured, resolved deploy targets remain the backwards-compatible
default replication target set. `replication_enabled = false` is an explicit
opt-out for deploy-only validation: replication target resolution, capability
checks, replication API calls, and replica readiness waits are skipped, and
deploy targets do not become implicit replication targets. The command verifies
resolved replication targets expose the provider `Object Storage` capability
before capture when replication is enabled, deploys only to resolved deploy
targets, and can receive the same deploy metadata defaults as `capture-deploy`.

## Region Policy Artifacts

`region-policy generate` reads public Linode provider region metadata through
the API boundary and writes deterministic TOML. The generated
`[provider_regions.*]` tables are provider fact data: region id plus normalized
capability names only. They intentionally omit labels, resolver IPs, account
limits, private resource identifiers, and any other account-specific or
unneeded provider fields.

`[generated_groups.*]` tables are generated convenience scaffolding. Capability
groups are derived from provider capability names, and country groups are
derived only from provider-exposed country codes. Capability-scoped country
groups, such as `country_us_object_storage`, are generated intersections of a
provider country code and a normalized provider capability name. Base country
groups continue to represent every provider region for the country code.
Capability-scoped country groups are generated only when at least one region
matches. These groups are safe to overwrite on regeneration and are not an
operator policy layer.

`[provider_overrides.*]` tables are narrow documented provider-discrepancy
inputs to generated helper groups. The first supported override is
`provider_overrides.image_replication_excluded_regions`, which lists provider
regions to exclude only from `country_*_image_replication` generated groups.
Raw `provider_regions.*` facts and provider-backed capability groups such as
`country_us_object_storage` remain unchanged. The override exists because
provider metadata can advertise `Object Storage` for a region while the image
replication POST rejects that region. If every Object Storage region for a
country is excluded, no `country_*_image_replication` helper is generated for
that country. It is intentionally not a general rule engine, policy transform
system, fallback mechanism, or execution-time filter.

`[groups.*]` tables are operator-owned intent. Each group has an explicit
`regions = [...]` list whose meaning is defined locally by the operator. A
group can represent any semantic boundary useful to the operator, but the tool
does not infer that boundary from country, city, coordinates, network latency,
provider labels, or region naming conventions.

When generation writes to an existing policy file, it parses and preserves the
supported `provider_overrides.*` and `groups.*` tables while refreshing
generated provider facts and generated helper groups. If the existing
provider overrides or operator-owned groups are malformed or contain
unsupported fields, generation fails rather than dropping documented intent.
Stale or malformed `generated_groups.*` tables do not block generation because
they are overwritten. `--replace-groups` is the explicit escape hatch for
dropping operator-owned groups; it does not drop documented provider
overrides.

The repository intentionally carries `policy/region-policy.toml` as the current
full generated provider policy snapshot. Operators can rerun generation and
review the version-control diff to detect provider region or capability drift.
The generated provider facts and generated helper groups stay generated. The
checked-in snapshot also carries deliberately documented, hand-maintained
operator-owned geo groups under `groups.*`. It may contain narrow documented
`provider_overrides.*` entries for known provider inconsistencies.
The operational maintenance loop is documented in
[`README.md`](../README.md#maintaining-region-policy-artifacts).

The first execution-policy consumer is `capture-replicate-deploy`.
`deploy_groups` and `replication_groups` default to
`policy/region-policy.toml` and can be pointed at another checked-in artifact
with `region_policy_file`. Resolution fails closed if the artifact is
malformed, stale against current provider metadata, contains stale generated
groups, references invalid regions, or omits a requested group. Group names
resolve from operator-owned `groups.*` first and generated
`generated_groups.*` second, so operator intent wins when names overlap.
`deploy_groups` expand deploy targets. `replication_groups` expand image
availability targets. Neither direction crosses over automatically when
replication input is configured, and the default no-replication-input behavior
continues to use resolved deploy targets as the replication target set unless
`replication_enabled = false` is configured. This resolution does not infer
geography, proximity, latency, fallback regions, or a "best" region. Generated
capability-scoped groups improve discoverability for workflows such as image
replication, but the execution layer still validates every resolved
replication target for the required provider capability and fails before
mutation if any requested target is invalid. Under the checked-in policy,
`geo_apac_north` currently has deploy targets but no matching
`geo_apac_north_image_replication` group because `jp-tyo-3` is a documented
image-replication provider discrepancy.

Versioned smoke configs under `examples/smoke/` make these policy semantics
reviewable as operational tooling. They are bounded provider validation inputs,
not broad deploy fan-out coverage: each executable smoke config uses one
explicit deploy region for capture/deploy and one operator-owned geo
image-replication group where a known-good group is checked in. `deploy_groups`
expansion remains covered by dry-run and unit behavior. APAC North and Oceania
currently have deploy geo groups but no executable replication smoke config
because the checked-in policy has no known-good geo image-replication group for
either geo. Smoke configs remain dry-run-first unless the operator explicitly
passes `--execute` with a valid provider token.

Broader geo validation configs under `examples/geo/` use deploy groups to
exercise more deploy targets. Geos with known-good image-replication groups
request those groups explicitly; deploy-only geos set `replication_enabled =
false` so the backwards-compatible no-replication-input default does not create
implicit replication targets.

Image-replication-specific generated groups improve the default operator
surface when a provider inconsistency is documented. The current documented
image-replication exclusions are `au-mel`, `de-fra-2`, `fr-par-2`, `gb-lon`,
`jp-tyo-3`, `sg-sin-2`, and `us-iad-2`. They do not rewrite raw provider
facts, bypass validation, or silently filter execution requests.

`region-policy validate` reads the artifact and current provider region
metadata, then emits sanitized JSON. It validates:

- `schema_version = 1`,
- a non-empty `[provider_regions.*]` structure,
- provider region entries with only `capabilities = [...]`,
- supported provider override entries with `regions = [...]` and `reason`,
- generated and operator group entries with only `regions = [...]`,
- all provider regions in the artifact still exist,
- all current provider regions are present in the artifact,
- stored provider capabilities match current provider capabilities, and
- every provider override and generated or operator group region reference
  points at a current provider region.

Validation is a policy-file freshness and shape check only. It does not choose
where to deploy, expand groups into execution plans, run replication, probe
latency, create fallbacks, or manage long-lived resource declarations.

## Capture Execution Boundary

`capture` without `--execute` remains non-mutating and does not read
`LINODE_TOKEN`. `capture --execute` requires exactly one region, a source image,
a Linode type, and `LINODE_TOKEN`.

The execute flow is intentionally linear:

1. preflight the token with non-mutating API calls,
2. preflight the requested region, Linode type, and available source image with
   non-mutating API calls,
3. create a tagged temporary capture-source Linode,
4. wait for readiness,
5. validate region, tags, and disk presence,
6. shut down the source Linode,
7. capture a custom image from the selected disk,
8. wait for image availability,
9. delete or preserve the source according to explicit flags.

Capture validation is provider/API-level only. It verifies region, required
tags, disk presence, image availability, and image tags from API responses.

## Deploy Execution Boundary

`deploy` without `--execute` remains non-mutating and does not read
`LINODE_TOKEN`. `deploy --execute` requires exactly one region, an existing
custom image id via `--image-id`, a Linode type, and `LINODE_TOKEN`. It may
also receive an existing firewall through `--firewall-id` or deploy config, and
public SSH keys through repeated `--authorized-key`, `--authorized-keys-file`,
or deploy config. It may receive Linode metadata user data through
`--user-data-file` or `[deploy].user_data_file`.

The execute flow is intentionally linear:

1. preflight the token with non-mutating API calls,
2. preflight the requested region, Linode type, available deploy image, and
   configured firewall with non-mutating API calls,
3. create a tagged temporary deploy Linode from the custom image id, passing
   `firewall_id`, `authorized_keys`, and Base64-encoded `metadata.user_data`
   only when explicitly configured,
4. wait for provider/API-level running status,
5. validate running status, requested region, and required tags,
6. delete or preserve the deploy instance according to explicit flags.

M3 does not perform SSH, cloud-init, service, or application readiness
validation.

## Replicate Execution Boundary

`replicate` without `--execute` remains non-mutating and does not read
`LINODE_TOKEN`. `replicate --execute` requires one or more regions, an existing
custom image id via `--image-id`, and `LINODE_TOKEN`.

The execute flow is intentionally bounded:

1. preflight the token with non-mutating API calls,
2. read the requested image and require provider-reported `available` status,
3. require the image response to expose existing image regions,
4. read each requested region and require the provider `Object Storage`
   capability needed for explicit image replication,
5. submit one image replication request to `POST /images/{imageId}/regions`,
   using the existing image regions plus the requested regions,
6. record provider/API-level validation, sanitized replication response
   details, and a response summary of returned region statuses.

The provider replication request uses a complete region set for the image. To
avoid accidentally removing an existing image region, execute mode preserves
the provider-reported existing regions in the submitted request. If existing
regions are not exposed, the command fails before mutation.
If a requested replication target region lacks the provider `Object Storage`
capability, the command fails before the replication POST and records the
failed capability check in the manifest.

Replicate execution does not poll replica convergence, repair replicas, clean
up replicas, or take ownership of regional image placement. The manifest
records `replica_status_polling: "not_attempted"` because public docs expose
replica statuses but do not define a polling contract this tool relies on.

## Capture-Deploy Execution Boundary

`capture-deploy` without `--execute` remains non-mutating and does not read
`LINODE_TOKEN`. `capture-deploy --execute` requires at least one region, a
source image, a Linode type, and `LINODE_TOKEN`.

The execute flow reuses the capture and deploy internals:

1. run capture with `mode=capture-deploy` and `component=capture`,
2. pass the internal custom image id to deploy without exposing it in stdout,
3. run deploy with `mode=capture-deploy` and `component=deploy`,
4. surface the combined provider/API-level validation summary,
5. delete the temporary capture-source Linode when its current-run tags match,
6. delete or preserve the temporary deploy Linode according to
   `--preserve-instance`,
7. preserve the custom image as the deliverable.

Capture-deploy cleanup is tag-scoped. It only deletes resources carrying all
required tags for the current run, including the matching component tag.
Because capture and deploy remain independently reusable, capture-deploy runs
each phase's non-mutating API preflight, including provider input checks.
Seeing two `preflight_api_access` and `preflight_provider_inputs` steps in a
combined manifest is expected.

With multiple requested regions, capture-deploy captures one custom image in
the first region and then deploys it to each requested region concurrently with
a bounded worker pool capped at 4 deploy workers. Linode custom images are
deployable across regions; public docs do not specify cross-region deploy
latency. Operators should expect farther-region deploys may take longer, but
the tool does not depend on that timing.

## Capture-Replicate-Deploy Execution Boundary

`capture-replicate-deploy` without `--execute` remains non-mutating and does
not read `LINODE_TOKEN`. If deploy or replication groups are configured,
dry-run resolves them through the selected region policy artifact and public
provider metadata. `capture-replicate-deploy --execute` requires at least one
resolved deploy target, a source image, a Linode type, and `LINODE_TOKEN`.

The execute flow is bounded and has no durable ownership model:

1. resolve configured `deploy_groups` and `replication_groups` from the
   selected region policy artifact, defaulting to `policy/region-policy.toml`,
2. combine explicit deploy regions and group-expanded deploy regions into a
   deterministic deploy target set,
3. when replication is enabled, combine explicit `replication_regions` and
   group-expanded regions into a deterministic replication target set,
4. when replication is enabled, read each resolved replication target region and
   require the provider `Object Storage` capability before capture,
5. run capture in the first resolved deploy target with
   `mode=capture-replicate-deploy` and `component=capture`,
6. when replication is enabled, read the captured image details and require
   `available` status plus exposed existing image regions,
7. when replication is enabled, submit one replication request for existing
   image regions plus all resolved replication target regions,
8. when replication is enabled, perform a bounded read-only wait until resolved
   replication target regions report image replica status `available`,
9. deploy from the captured image only to resolved deploy targets with bounded
   deploy fan-out,
10. clean up temporary capture and deploy Linodes by current-run tags,
11. preserve the captured custom image as the workflow deliverable.

The capture/original image region is preserved during the replication POST by
submitting provider-reported existing image regions together with the requested
replication target regions. This preservation is independent from deploy
intent: a resolved deploy target can remain outside the requested replication
target set and still be used by deploy through the provider's cross-region
image deploy behavior.

The workflow fails closed before capture if policy validation fails, a
requested group is unknown, a requested replication target region lacks the
provider `Object Storage` capability, or a group references a region missing
from current provider metadata. When replication is enabled, it fails closed
before deploy if existing image regions are not exposed or if requested replica
statuses do not report `available` before the bounded wait expires. When
`replication_enabled = false`, those replication-only gates are skipped
intentionally and recorded as skipped in the manifest. It does not retry the
replication mutation, repair replicas, reconcile desired regions, infer
fallback regions, auto-select regions, run a scheduler, write state, or own
image placement after the run.

## Cleanup Semantics

Cleanup status is narrow and literal. `deleted` means a temporary Linode was
deleted after matching current-run tags. `preserved` means no deletion occurred
for that resource because preservation was requested or tags did not match.
`completed` is reserved for combined cleanup after the phase cleanup blocks
finish. `failed` means cleanup did not complete. Preserved and failed entries
include a sanitized `reason`; the custom image uses `deliverable`.

Standalone `cleanup` is dry-run by default. Plain `cleanup` emits a
non-mutating manifest preview and never reads `LINODE_TOKEN` or calls Linode.
`cleanup --discover` requires `LINODE_TOKEN`, performs read-only managed
resource discovery, and reports expired eligible Linodes and lab-owned custom
images in `cleanup_candidates` without deleting them. `cleanup --execute`
requires `LINODE_TOKEN`, performs the same discovery, and deletes only expired
resources carrying all required managed tags:

- `project=linode-image-lab`
- `run_id=...`
- `mode=...`
- `component=...`
- `ttl=<absolute-utc-timestamp>`

`ttl` is a project-internal cleanup tag used by this tool. Linode does not
enforce it as a provider-side expiration policy.

Cleanup discovery entries expose machine-readable expiration metadata derived
from valid `ttl` tags. Expired entries include `expired_at`, the parsed UTC TTL
timestamp, and `expired_for_seconds`, the integer number of seconds elapsed
since expiration. Unexpired preserved entries include `expires_in_seconds`, the
integer number of seconds remaining until expiration. Entries with
`reason=ttl_parse_failed` omit all derived time fields. `cleanup_candidates` are
ordered deterministically with the longest-expired candidate first.

Explicit `--run-id` values must match
`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`: 1 to 64 characters, starting with a
letter or digit, followed only by letters, digits, dot, underscore, or hyphen.
The CLI validates supplied run IDs before manifest generation, discovery, or
execution. Cleanup applies the same validator to discovered `run_id` tags and
preserves resources whose run ID tag is malformed.

Standalone cleanup never deletes untagged resources, broader account resources,
or images outside the default lab-owned project tag. Those out-of-scope images
are ignored by standalone cleanup discovery. Malformed TTL values, future TTL
values, missing required tags, invalid tag values, and optional `--run-id`
filter mismatches preserve discovered resources and report a sanitized reason.
Only discovered lab-owned images can appear as deleted, preserved, or failed
cleanup entries. Execute cleanup re-fetches each candidate before one DELETE
attempt. A failed DELETE attempt is not retried blindly; it is reported as
`reason=delete_status_unknown`, and later candidates
are still evaluated.
