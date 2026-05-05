# Capture-Deploy Workflow

This document describes the current capture/deploy workflow shape. M2 adds
single-region capture execution, M3 adds single-region deploy execution, M4
adds single-region capture-deploy execution, and M5 adds bounded multi-region
capture-deploy execution.

## Capture

The capture flow creates a reusable custom image from an existing Linode or
disk. By default, the command returns a dry-run manifest and performs no Linode
action.

`capture --execute` is the only mutating M2 command. It creates Linode
resources and may incur account charges until cleanup completes. It requires:

- exactly one `--region`,
- `--source-image`,
- `--type`,
- `LINODE_TOKEN`.

Execution steps:

1. preflight token access without mutating resources,
2. verify the requested region, Linode type, and source image with read-only
   API calls,
3. create a temporary capture-source Linode from the source image,
4. wait until the source is ready,
5. validate provider/API-level region, required tags, and disk presence,
6. power off the source,
7. create a custom image from the selected disk,
8. wait until the provider reports the image is available,
9. delete the temporary source unless `--preserve-source` is set.

The custom image is preserved by default because it is the capture deliverable.
Capture validation stops at provider/API data; it does not perform SSH,
cloud-init, service, or application readiness checks.

## Deploy

The deploy flow creates a temporary Linode from a custom image. By default, the
command returns a dry-run manifest and performs no Linode action.

`deploy --execute` is the only mutating deploy command. It creates a temporary
Linode from the existing custom image passed as `--image-id` and may incur
account charges until cleanup completes. It requires:

- exactly one `--region`,
- `--image-id`,
- `--type`,
- `LINODE_TOKEN`.

Execution steps:

1. preflight token access without mutating resources,
2. verify the requested region, Linode type, and deploy image with read-only API
   calls,
3. create a temporary deploy Linode from the custom image id,
4. wait until the provider reports the instance is running,
5. validate provider/API-level running status, requested region, and required tags,
6. delete the temporary instance unless `--preserve-instance` is set.

The deploy instance is deleted by default because deploy execution is a quick
validation path, not a long-lived server creation flow.

Deploy validation stops at provider/API-level instance creation. It does not
perform SSH, cloud-init, service, or application readiness checks.

Live smoke command shape:

```sh
LINODE_TOKEN="$LINODE_TOKEN" PYTHONPATH=src python3 -m linode_image_lab.cli deploy \
  --region us-east \
  --execute \
  --image-id "$CUSTOM_IMAGE_ID" \
  --type g6-nanode-1 \
  --run-id "run-m3-smoke"
```

With config-backed defaults:

```sh
LINODE_TOKEN="$LINODE_TOKEN" PYTHONPATH=src python3 -m linode_image_lab.cli \
  --config examples/config/deploy-existing-image.toml \
  deploy \
  --execute \
  --run-id "run-m3-smoke"
```

## Capture-Deploy

The capture-deploy flow validates the end-to-end path by capturing a custom
image, then deploying a new Linode from it. By default, the command returns a
dry-run manifest and performs no Linode action.

`capture-deploy --execute` is the only mutating combined command. It requires:

- at least one `--region`,
- `--source-image`,
- `--type`,
- `LINODE_TOKEN`.

Execution steps:

1. run capture execution with `mode=capture-deploy` and `component=capture`,
2. retain the internal custom image id in memory,
3. run deploy execution with `mode=capture-deploy` and `component=deploy`,
4. validate provider/API-level running status, requested region, and required tags,
5. delete the temporary capture-source Linode,
6. delete the temporary deploy validation Linode unless `--preserve-instance` is set,
7. preserve the custom image as the workflow deliverable.

The internal image id is passed directly from capture to deploy. Normal stdout
redacts provider identifiers, so the image id is not exposed in serialized
manifests.

Capture-deploy intentionally runs the non-mutating API preflight inside both
the capture and deploy phases, including read-only provider checks for region,
type, and image availability. This keeps each phase independently safe and
reusable, so combined manifests may show two `preflight_api_access` and
`preflight_provider_inputs` steps.

When multiple regions are provided, `capture-deploy --execute` captures one
custom image in the first requested region, then deploys that same captured
image to each requested region concurrently with a bounded worker pool. There
is no cross-region dependency graph, scheduler, retry fan-out, or
infrastructure reconciliation. Linode custom images are deployable across
regions; public docs do not specify cross-region deploy latency. Operators
should expect farther-region deploys may take longer, but the tool does not
depend on that timing. The single capture result is recorded under `capture`,
and each deploy attempt is recorded under `deploy_results.<region>`.

If capture fails, no deploy regions are attempted and the top-level status is
`failed`. If capture succeeds, multi-region execution continues after a deploy
region fails. Cleanup for each temporary deploy Linode is handled by that
region's deploy run, and other deploy regions still get their own isolated
flow. After all deploy regions finish, the temporary capture-source Linode is
cleaned up once and the custom image is preserved as the single deliverable.
The top-level manifest reports:

- `succeeded` when every requested deploy region succeeds and capture cleanup
  completes,
- `partial` when at least one deploy region succeeds and at least one fails, or
  when deploys succeed but capture cleanup fails,
- `failed` when capture fails or every deploy region fails.

The top-level `summary` lists succeeded and failed regions. Deploy failures
represent real provider/API errors, invalid inputs, or transient issues. If any
region fails, or if final capture cleanup fails, the CLI still emits the
combined manifest and exits non-zero.

Live smoke command shape:

```sh
LINODE_TOKEN="$LINODE_TOKEN" PYTHONPATH=src python3 -m linode_image_lab.cli capture-deploy \
  --region us-east \
  --execute \
  --source-image linode/debian12 \
  --type g6-nanode-1 \
  --run-id "run-m4-smoke"
```

With config-backed defaults:

```sh
LINODE_TOKEN="$LINODE_TOKEN" PYTHONPATH=src python3 -m linode_image_lab.cli \
  --config examples/config/capture-deploy-smoke.toml \
  capture-deploy \
  --execute \
  --run-id "run-m4-smoke"
```

Config defaults can replace omitted `--region`, `--source-image`,
`--image-id`, `--type`, and `--ttl` values. They do not replace `--execute`,
preservation flags, run ids, or environment-based token injection.
Use `linode-image-lab config validate --config PATH --command COMMAND` to
validate a config file and inspect the effective defaults before a smoke run.
The report is non-mutating, does not read `LINODE_TOKEN`, and labels precedence
as CLI values, then the selected command table, then `[defaults]`.

## Cleanup

Cleanup is independently runnable. It selects resources by required tags and an
expired `ttl` timestamp. A resource is not a cleanup candidate unless all
required tags are present:

- `project=linode-image-lab`
- `run_id=<unique-id>`
- `mode=<capture|deploy|capture-deploy>`
- `component=<capture|deploy>`
- `ttl=<timestamp>`

`ttl` is a project-internal cleanup tag used by this tool. Linode does not
enforce it as a provider-side expiration policy.

Execute-mode cleanup inside capture, deploy, and capture-deploy is narrower
than standalone cleanup. Capture only attempts to delete the current run's
temporary capture-source Linode, deploy only attempts to delete the current
run's temporary deploy Linode, and capture-deploy only attempts to delete those
two temporary Linodes for the current combined run. In all cases, cleanup
proceeds only when the resource has all required tags matching the current run.
If tags are missing or do not match, cleanup preserves the resource and records
`reason=tag_mismatch`.

Standalone `cleanup` is also dry-run by default. Plain `cleanup` emits a local
manifest preview only; it does not read `LINODE_TOKEN` or call Linode. `cleanup
--discover` requires `LINODE_TOKEN`, lists managed Linodes, and reports expired
eligible resources without deleting them. `cleanup --execute` requires
`LINODE_TOKEN` and deletes only expired temporary Linodes with the complete
required tag set. It does not delete custom images, untagged resources, or
resources with malformed or unexpired TTL values.

Cleanup manifests use the same fields across commands: `status`, `deleted`,
`preserved`, and `failed`. `deleted` lists temporary Linodes removed after
required tags matched. `preserved` lists resources kept by request or kept
because required tags did not match, with a `reason` such as `requested`,
`tag_mismatch`, or `deliverable`. Standalone cleanup re-fetches each candidate
before one DELETE attempt; if that attempt fails, the resource is reported in
`failed` with `reason=delete_status_unknown` because the provider-side state
cannot be confirmed safely. In capture-deploy, top-level cleanup is the
combined summary; `capture.cleanup` and `deploy.cleanup` are the phase-specific
results.

## Manifest Structure

Single-command execute manifests expose top-level `status`, `steps`,
`resources`, `validation`, and `cleanup`. Single-region capture-deploy keeps
the same top-level fields and also nests `capture` and `deploy` sections.
Top-level `resources` and `validation` summarize the whole run. Nested
`capture.resources`, `deploy.resources`, `capture.validation`, and
`deploy.validation` are phase-specific slices of that same lifecycle.

Multi-region capture-deploy execute manifests expose a top-level `status`,
`regions`, `capture`, `deploy_results`, and `summary`. `capture` is the single
capture manifest from the first requested region. Each value in
`deploy_results` is a deploy execute manifest for that requested region,
including `steps`, `resources`, `validation`, and `cleanup`. The top-level
multi-region manifest is aggregate-only and does not duplicate nested
`resources`, `validation`, or `cleanup` fields.

`validation` means provider/API-level checks only: input existence/access,
image available status, resource state, requested region, required tags, and
disk presence for capture. It does not include SSH, app health, service
readiness, or cloud-init completion checks.

Validation checks are structured as stable objects with `name`, `status`, and a
symbolic `target`. Failed checks include a sanitized `failure_reason`; provider
resource identifiers are redacted during serialization.
