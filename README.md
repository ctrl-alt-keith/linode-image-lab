# Linode Image Lab

Linode Image Lab is a small Python scaffold for modeling custom image
capture/deploy workflows before any real cloud mutation behavior exists.

M1 is intentionally conservative:

- `plan` emits a dry-run, sanitized manifest-like preview.
- `capture`, `deploy`, and `capture-deploy` are explicit placeholders.
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

`LINODE_TOKEN` is reserved for later Linode API work. M1 does not read or use
the value.

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
PYTHONPATH=src python3 -m linode_image_lab.cli deploy --region us-east
PYTHONPATH=src python3 -m linode_image_lab.cli capture-deploy --region us-east
PYTHONPATH=src python3 -m linode_image_lab.cli cleanup
```

The placeholder commands return structured JSON and do not mutate Linode
resources.
