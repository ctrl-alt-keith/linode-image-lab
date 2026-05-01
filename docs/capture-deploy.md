# Capture-Deploy Workflow

This document describes the current capture/deploy workflow shape. M2 adds
single-region capture execution only; deploy and capture-deploy execution remain
out of scope.

## Capture

The capture flow creates a reusable custom image from an existing Linode or
disk. By default, the command returns a dry-run manifest and performs no Linode
action.

`capture --execute` is the only mutating M2 command. It requires:

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

The deploy flow creates a new Linode from a custom image. The command currently
returns a placeholder manifest and performs no Linode action.

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

Execute-mode capture cleanup is narrower than general cleanup. It only attempts
to delete the current run's temporary capture-source Linode, and only when that
resource has all required tags matching the current run. If tags are missing or
do not match, cleanup is skipped and the manifest reports the skip.
