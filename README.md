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

Install the released `v0.1.0` tag directly from GitHub:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install git+https://github.com/ctrl-alt-keith/linode-image-lab.git@v0.1.0
linode-image-lab --help
```

## Release Recovery

If `make release-publish VERSION=X.Y.Z` pushes `vX.Y.Z` but fails before
`gh release create` completes, recover manually instead of rerunning the publish
target. Confirm that the remote tag exists, the GitHub release does not, and the
tag points at the intended release commit:

```sh
VERSION=X.Y.Z
TAG="v${VERSION}"
git fetch origin main --tags
git ls-remote --exit-code --tags origin "refs/tags/${TAG}"
git show --no-patch --decorate --oneline "${TAG}"
gh release view "${TAG}"
```

If the tag commit is correct and `gh release view` reports no release, create
the missing GitHub release from the existing tag:

```sh
make release-notes VERSION="${VERSION}" > /tmp/linode-image-lab-release-notes.md
gh release create "${TAG}" \
  --title "${TAG}" \
  --notes-file /tmp/linode-image-lab-release-notes.md
gh release view "${TAG}"
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
defaults. Config values fill omitted CLI flags; explicit CLI flags always win.

```sh
linode-image-lab --config examples/config/capture-deploy-smoke.toml capture-deploy
linode-image-lab capture-deploy --config examples/config/capture-deploy-smoke.toml --execute
linode-image-lab config validate --config examples/config/capture-deploy-smoke.toml --command capture-deploy
```

Config uses `schema_version = 1` with optional `[defaults]`, `[capture]`,
`[deploy]`, `[capture-deploy]`, and `[cleanup]` tables. Supported values are
`region` or `regions`, `ttl`, `source_image`, `image_id`, and `type`, depending
on the command.

`capture-deploy --execute` accepts multiple regions through repeated
`--region` flags or `regions = [...]` config. It captures one custom image in
the first requested region, then deploys that captured image sequentially to
each requested region. Linode custom images are deployable across regions; the
public docs do not specify cross-region deploy latency. Operators should expect
farther-region deploys may take longer, but the tool does not depend on that
timing. Standalone `capture --execute` and `deploy --execute` remain
single-region only.

`config validate` parses the TOML file, applies the same safety checks as
command execution, and emits a non-mutating JSON report with `precedence`,
`effective_defaults`, and `sources`. Precedence is explicit CLI values first,
then the selected command table, then `[defaults]`. You can pass supported CLI
default flags such as `--region`, `--ttl`, `--source-image`, `--image-id`, or
`--type` to preview how they override the config for the selected command.

Config is only for execution defaults. It cannot contain `LINODE_TOKEN`, token
values, passwords, SSH keys, cloud-init data, `execute`, `discover`,
preservation flags, or run id fields. `--execute` or `cleanup --discover` must
still be passed explicitly, and `LINODE_TOKEN` must still come from the
environment or approved environment injection.

## Behavior Clarifications

- All commands are dry-run by default.
- `--execute` enables real Linode API mutations for `capture`, `deploy`,
  `capture-deploy`, and `cleanup`.
- Provider behavior assumptions are tracked in
  [docs/provider-assumptions.md](docs/provider-assumptions.md).
- Config values only fill omitted command options; CLI flags override config.
- Execute runs use temporary resources and clean them up automatically unless a
  preservation flag is used.
- Execute runs verify the requested region, Linode type, and source or deploy
  image with read-only Linode API calls before resource creation.
- Custom images are preserved as deliverables.
- `cleanup` is independently runnable, dry-run by default, and can delete
  expired tagged temporary Linodes only with `--execute`.
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
- General-purpose multi-region orchestration outside sequential
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
  --execute
```

Execute capture plus deploy validation:

```sh
linode-image-lab capture-deploy \
  --region us-east \
  --source-image linode/alpine3.23 \
  --type g6-nanode-1 \
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
discovery, and reports expired eligible Linodes in `cleanup_candidates` without
deleting them. `cleanup --execute` requires `LINODE_TOKEN`, lists managed
Linodes, and deletes only expired Linodes carrying the complete required tag
set. Use `--run-id` to restrict discovery or deletion to one run.

## Required Tags

Modeled resources use rediscoverable tags:

- `project=linode-image-lab`
- `run_id=<unique-id>`
- `mode=<capture|deploy|capture-deploy>`
- `component=<capture|deploy>`
- `ttl=<timestamp>`

`ttl` is a project-internal cleanup tag used by this tool. Linode does not
enforce it as a provider-side expiration policy.

## Manifest Output

Execute manifests use consistent top-level `status`, `steps`, `resources`,
`validation`, and `cleanup` fields. For single-region `capture-deploy`,
top-level `resources`, `validation`, and `cleanup` summarize the combined run,
while nested `capture` and `deploy` blocks show phase-specific details.
Multi-region `capture-deploy --execute` emits one combined manifest with
top-level `status`, `regions`, `capture`, `deploy_results`, and `summary`.
The nested `capture` value is the single capture manifest, and each
`deploy_results.<region>` value is the deploy manifest for that requested
region.

Multi-region status is `succeeded` when every requested deploy region succeeds,
`partial` when some deploy regions fail, and `failed` when capture fails or
every deploy region fails. A failed deploy region does not block cleanup for
that region or execution of later deploy regions. Partial failures indicate
real provider/API errors, invalid inputs, or transient issues. Validation checks
are objects with `name`, `status`, and a symbolic `target`; failed checks
include a sanitized `failure_reason`.

Cleanup status values are literal: `deleted` means a temporary Linode was
deleted, `preserved` means a resource was kept or skipped for safety,
`completed` means combined cleanup finished, and `failed` means cleanup did not
complete.

Standalone `cleanup --execute` does not delete custom images, untagged
resources, or resources with missing, malformed, unexpired, or mismatched
managed tags. Preserved entries include a sanitized `reason`, such as
`ttl_not_expired`, `ttl_parse_failed`, or `missing_required_tags`.

## Independence and Intent

This is a personal, independent project. It is not affiliated with any employer
or organization.

It is designed as a public-safe workflow lab and does not use proprietary
systems, data, or credentials.
