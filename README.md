# Linode Image Lab

Linode Image Lab is a small Python scaffold for modeling and safely exercising
custom image capture/deploy workflows.

M4 remains intentionally conservative:

- `plan` emits a dry-run, sanitized manifest-like preview.
- `capture` is dry-run by default.
- `deploy` is dry-run by default.
- `capture-deploy` is dry-run by default.
- `capture --execute`, `deploy --execute`, and `capture-deploy --execute` are
  the only commands that can mutate Linode resources.
- `cleanup` is first-class and independently runnable.
- Manifest schema, rediscoverable tags, and cleanup selection are the foundation.

## Independence and Intent

This is a personal, independent project. It is not affiliated with any employer
or organization.

It is designed as a public-safe workflow lab and does not use proprietary
systems, data, or credentials.

## Quick Start

```sh
make check
PYTHONPATH=src python3 -m linode_image_lab.cli plan --region us-east --run-id demo-run
```

`LINODE_TOKEN` is read only when `capture --execute`, `deploy --execute`, or
`capture-deploy --execute` is used. Dry-run commands do not read the token, call
Linode, or mutate resources.

## Required Tags

Every modeled resource uses rediscoverable tags:

- `project=linode-image-lab`
- `run_id=<unique-id>`
- `mode=<capture|deploy|capture-deploy>`
- `component=<capture|deploy>`
- `ttl=<timestamp>`

## Commands

```sh
PYTHONPATH=src python3 -m linode_image_lab.cli plan --region us-east,us-west
PYTHONPATH=src python3 -m linode_image_lab.cli capture --region us-east
PYTHONPATH=src python3 -m linode_image_lab.cli capture --region us-east --execute --source-image linode/debian12 --type g6-nanode-1
PYTHONPATH=src python3 -m linode_image_lab.cli deploy --region us-east
PYTHONPATH=src python3 -m linode_image_lab.cli deploy --region us-east --execute --image-id "$CUSTOM_IMAGE_ID" --type g6-nanode-1
PYTHONPATH=src python3 -m linode_image_lab.cli capture-deploy --region us-east
PYTHONPATH=src python3 -m linode_image_lab.cli capture-deploy --region us-east --execute --source-image linode/debian12 --type g6-nanode-1
PYTHONPATH=src python3 -m linode_image_lab.cli cleanup
```

`capture --execute` requires exactly one region, `--source-image`, `--type`, and
`LINODE_TOKEN`. It creates a temporary capture-source Linode, waits for it to be
ready, powers it off, captures a custom image from its disk, waits for the image,
then deletes the temporary source unless `--preserve-source` is provided.

`deploy --execute` requires exactly one region, `--image-id`, `--type`, and
`LINODE_TOKEN`. `--image-id` is the existing custom image to boot from. The
command creates a temporary deploy Linode, waits for provider/API-level running
status, validates the requested region and required tags, then deletes the
temporary instance unless `--preserve-instance` is provided. Deploy validation
does not perform SSH, cloud-init, service, or application readiness validation.

`capture-deploy --execute` requires exactly one region, `--source-image`,
`--type`, and `LINODE_TOKEN`. It captures a custom image, deploys a temporary
validation Linode from that image, validates provider/API-level running status,
requested region, and required tags, then deletes the temporary capture-source
and deploy validation Linodes. The custom image is preserved by default as the
deliverable. `--preserve-instance` keeps only the deploy validation Linode.

Normal stdout is a redacted, export-safe manifest view. Local in-memory
manifests may retain provider identifiers needed for cleanup and debugging, but
serialized output redacts those identifiers.
