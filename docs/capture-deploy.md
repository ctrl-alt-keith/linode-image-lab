# Capture-Deploy Workflow

This document describes the current capture/deploy workflow shape. M2 adds
single-region capture execution, M3 adds single-region deploy execution, and M4
adds single-region capture-deploy execution.

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
2. create a temporary capture-source Linode from the source image,
3. wait until the source is ready,
4. validate provider/API-level region, required tags, and disk presence,
5. power off the source,
6. create a custom image from the selected disk,
7. wait until the provider reports the image is available,
8. delete the temporary source unless `--preserve-source` is set.

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
2. create a temporary deploy Linode from the custom image id,
3. wait until the provider reports the instance is running,
4. validate provider/API-level running status, requested region, and required tags,
5. delete the temporary instance unless `--preserve-instance` is set.

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

- exactly one `--region`,
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
the capture and deploy phases. This keeps each phase independently safe and
reusable, so combined manifests may show two `preflight_api_access` steps.

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

## Cleanup

Cleanup is independently runnable. It selects resources by required tags and an
expired `ttl` timestamp. A resource is not a cleanup candidate unless all
required tags are present:

- `project=linode-image-lab`
- `run_id=<unique-id>`
- `mode=<capture|deploy|capture-deploy>`
- `component=<capture|deploy>`
- `ttl=<timestamp>`

Execute-mode cleanup inside capture, deploy, and capture-deploy is narrower
than standalone cleanup. Capture only attempts to delete the current run's
temporary capture-source Linode, deploy only attempts to delete the current
run's temporary deploy Linode, and capture-deploy only attempts to delete those
two temporary Linodes for the current combined run. In all cases, cleanup
proceeds only when the resource has all required tags matching the current run.
If tags are missing or do not match, cleanup preserves the resource and records
`reason=tag_mismatch`.

Standalone `cleanup` is also dry-run by default. With `LINODE_TOKEN`, the dry
run lists managed Linodes and reports expired eligible resources without
deleting them. `cleanup --execute` requires `LINODE_TOKEN` and deletes only
expired temporary Linodes with the complete required tag set. It does not delete
custom images, untagged resources, or resources with malformed or unexpired TTL
values.

Cleanup manifests use the same fields across commands: `status`, `deleted`,
and `preserved`. `deleted` lists temporary Linodes removed after required tags
matched. `preserved` lists resources kept by request or kept because required
tags did not match, with a `reason` such as `requested`, `tag_mismatch`, or
`deliverable`. In capture-deploy, top-level cleanup is the combined summary;
`capture.cleanup` and `deploy.cleanup` are the phase-specific results.

## Manifest Structure

Single-command execute manifests expose top-level `status`, `steps`,
`resources`, `validation`, and `cleanup`. Capture-deploy keeps the same
top-level fields and also nests `capture` and `deploy` sections. Top-level
`resources` is the combined list for the whole run. Nested `capture.resources`
and `deploy.resources` are phase-specific slices of that same lifecycle.

`validation` means provider/API-level checks only: resource state, requested
region, required tags, disk presence for capture, and image availability for
capture. It does not include SSH, app health, service readiness, or cloud-init
completion checks.
