# Linode Image Lab

Safe, repeatable Linode image capture and deploy validation with automatic cleanup.

- Plans capture, deploy, and capture-deploy runs before any API mutation.
- Captures custom images from temporary Linode instances.
- Deploys temporary validation instances from custom images.
- Validates requested region, Linode type, image inputs, tags, resources, and
  running status at the API level.
- Cleans up temporary resources while preserving custom images as deliverables.
- Emits redacted, public-safe manifests for review and automation.

## Quick Start

Requires Python 3.12 or newer.

Set `LINODE_TOKEN` first; execute mode reads it from the environment.
Plain dry-run commands do not need it.

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
--discover` is used. Dry-run commands, including plain `cleanup`, do not read
the token, call Linode, or mutate resources.

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
`[deploy]`, `[capture-deploy]`, and `[cleanup]` tables. Supported values are
`region` or `regions`, `ttl`, `source_image`, `image_id`, `type` or
`instance_type`, `image_project_tag`, `firewall_id`, `authorized_keys`,
`authorized_keys_file`, and `user_data_file`, depending on the command and
table. `[capture].image_project_tag` and
`[capture-deploy].image_project_tag` set only the captured custom image's
artifact-facing `project=<value>` tag; temporary Linode lifecycle tags remain
owned by `linode-image-lab`. A non-default image project tag places the
captured image outside standalone cleanup ownership and discovery.
`image_project_tag` is config-only and has no CLI override.

Deploy metadata defaults are field-specific. `firewall_id` is a scalar default
for deploy instances. Authorized keys are additive: configured
`authorized_keys`, configured `authorized_keys_file`, repeated
`--authorized-key`, and `--authorized-keys-file` inputs are merged and deduped
instead of replacing each other. `[deploy].authorized_keys` and
`[deploy].authorized_keys_file` also feed the deploy phase of `capture-deploy`;
`[capture-deploy]` can add command-specific keys. `[deploy].user_data_file`
provides deploy-scoped Linode metadata user data for `deploy` and for the
deploy phase of `capture-deploy`; `--user-data-file` overrides it when provided.
There is no `[capture-deploy].user_data_file`. File inputs are explicit; the
tool never discovers keys or user data.

`capture-deploy --execute` accepts multiple regions through repeated
`--region` flags or `regions = [...]` config. It captures one custom image in
the first requested region, then deploys that captured image to each requested
region concurrently with a bounded worker pool capped at 4 deploy workers.
Linode custom images are deployable across regions; the public docs do not
specify cross-region deploy latency. Operators should expect farther-region
deploys may take longer, but the tool does not depend on that timing.
Standalone `capture --execute` and `deploy --execute` remain single-region
only.

`config validate` parses the TOML file, applies the same safety checks as
command execution, and emits a non-mutating JSON report with `precedence`,
`effective_defaults`, and `sources`. The `precedence` list names the source
classes considered for the selected command, and `sources` shows which source
fed each effective field. It is not a single override rule for every field:
scalar fields use override precedence, authorized-key inputs merge and dedupe
additively, and user data remains deploy-scoped. For `capture-deploy`, scalar
defaults come from explicit CLI flags, then `[capture-deploy]`, then
`[defaults]`; authorized keys can come from `[deploy]`, `[capture-deploy]`, and
CLI key inputs; user data can come from `[deploy].user_data_file` or
`--user-data-file`. You can pass supported CLI default flags such as `--region`,
`--ttl`, `--source-image`, `--image-id`, or `--type` to preview config
resolution for the selected command. For deploy defaults, `--firewall-id`,
`--authorized-key`, `--authorized-keys-file`, and `--user-data-file` can also be
previewed. Authorized key and user-data output reports safe metadata such as
count, source, and byte count only.

Config is only for execution defaults. It cannot contain `LINODE_TOKEN`, token
values, passwords, private SSH keys, inline cloud-init data, `execute`,
`discover`, preservation flags, or run id fields. `--execute` or
`cleanup --discover` must still be passed explicitly, and `LINODE_TOKEN` must
still come from the environment or approved environment injection.

## Behavior Clarifications

- All commands are dry-run by default.
- `--execute` enables real Linode API mutations for `capture`, `deploy`,
  `capture-deploy`, and `cleanup`.
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

## What This Does

- Captures custom images from temporary Linode instances.
- Deploys temporary validation instances.
- Validates region, Linode type, image inputs, tags, resources, and running
  status at the API level.
- Cleans up temporary resources.

## What This Does Not Do

- SSH, cloud-init, service, or application-level validation.
- Manage long-lived infrastructure.
- General-purpose multi-region orchestration outside bounded
  `capture-deploy --execute` validation runs.

## Commands

Dry-run previews:

```sh
linode-image-lab plan --region us-east --mode capture-deploy
linode-image-lab capture --region us-east
linode-image-lab deploy --region us-east
linode-image-lab capture-deploy --region us-east
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
- `mode=<capture|deploy|capture-deploy>`
- `component=<capture|deploy>`
- `ttl=<timestamp>`

`ttl` is a project-internal cleanup tag used by this tool. Linode does not
enforce it as a provider-side expiration policy.

Captured custom images use separate artifact tags with the same `run_id`,
`mode`, `component`, and `ttl` metadata. By default the artifact project tag is
`project=linode-image-lab`, making expired images eligible for explicit
standalone cleanup. `[capture].image_project_tag` or
`[capture-deploy].image_project_tag` can change the value after `project=`.
Images outside the default lab-owned project tag are ignored by standalone
cleanup discovery and are not deleted by standalone cleanup.

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
