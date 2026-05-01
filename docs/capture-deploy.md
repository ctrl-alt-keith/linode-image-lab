# Capture-Deploy Workflow

M1 documents the intended workflow shape while keeping execution non-mutating.

## Capture

The capture flow creates a reusable custom image from an existing Linode or
disk. In M1, the command returns a placeholder manifest and performs no Linode
action.

## Deploy

The deploy flow creates a new Linode from a custom image. In M1, the command
returns a placeholder manifest and performs no Linode action.

## Capture-Deploy

The capture-deploy flow validates the end-to-end path by capturing a custom
image, then deploying a new Linode from it. In M1, the command returns a
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
