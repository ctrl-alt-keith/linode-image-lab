# Capture-Deploy Workflow

This document describes the current capture/deploy workflow shape. M2 adds
single-region capture execution, M3 adds single-region deploy execution, and
capture-deploy execution remains out of scope.

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
4. validate requested region, required tags, and disk presence,
5. power off the source,
6. create a custom image from the selected disk,
7. wait until the image is available,
8. delete the temporary source unless `--preserve-source` is set.

The custom image is preserved by default because it is the capture deliverable.

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

M3 deploy validation stops at provider/API-level instance creation. It does not
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

## Capture-Deploy

The capture-deploy flow validates the end-to-end path by capturing a custom
image, then deploying a new Linode from it. The command currently returns a
placeholder manifest with `mode=capture-deploy`.

## Cleanup

Cleanup is independently runnable. It selects resources by required tags and an
expired `ttl` timestamp. A resource is not a cleanup candidate unless all
required tags are present:

- `project=linode-image-lab`
- `run_id=<unique-id>`
- `mode=<capture|deploy|capture-deploy>`
- `component=<capture|deploy>`
- `ttl=<timestamp>`

Execute-mode cleanup is narrower than general cleanup. Capture only attempts to
delete the current run's temporary capture-source Linode, and deploy only
attempts to delete the current run's temporary deploy Linode. In both cases,
cleanup proceeds only when the resource has all required tags matching the
current run. If tags are missing or do not match, cleanup is skipped and the
manifest reports the skip.
