# Freeze-Thaw Workflow

M1 documents the intended workflow shape while keeping execution non-mutating.

## Freeze

The future freeze flow will build or identify image inputs, create a manifest,
tag rediscoverable resources, and record enough state for cleanup. In M1, the
command returns a placeholder manifest and performs no Linode action.

## Thaw

The future thaw flow will recreate runnable resources from a frozen image and
manifest. In M1, the command returns a placeholder manifest and performs no
Linode action.

## Freeze-Thaw

The future combined flow will freeze and then thaw in a single run. In M1, the
command returns a placeholder manifest with `mode=freeze-thaw`.

## Cleanup

Cleanup is independently runnable. It selects resources by required tags and an
expired `ttl` timestamp. A resource is not a cleanup candidate unless all
required tags are present:

- `project=linode-image-lab`
- `run_id=<unique-id>`
- `mode=<freeze|thaw|freeze-thaw>`
- `component=<builder|thaw>`
- `ttl=<timestamp>`
