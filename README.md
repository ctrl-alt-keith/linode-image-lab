# Linode Image Lab

Safe, repeatable Linode image capture and deploy validation with automatic cleanup.

- Plans capture, deploy, and capture-deploy runs before any API mutation.
- Captures custom images from temporary Linode instances.
- Deploys temporary validation instances from custom images.
- Validates requested region, tags, resources, and running status at the API level.
- Cleans up temporary resources while preserving custom images as deliverables.
- Emits redacted, public-safe manifests for review and automation.

## Quick Start

Set `LINODE_TOKEN` first; execute mode reads it from the environment.

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

`LINODE_TOKEN` is required only when `--execute` is used. Dry-run commands do
not read the token, call Linode, or mutate resources.

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

## Behavior Clarifications

- All commands are dry-run by default.
- `--execute` enables real Linode API mutations for `capture`, `deploy`, and
  `capture-deploy`.
- Execute runs use temporary resources and clean them up automatically unless a
  preservation flag is used.
- Custom images are preserved as deliverables.
- `cleanup` is independently runnable and currently previews tag-scoped cleanup
  selection.
- Normal stdout is redacted for public-safe review.

## What This Does

- Captures custom images from temporary Linode instances.
- Deploys temporary validation instances.
- Validates region, tags, resources, and running status at the API level.
- Cleans up temporary resources.

## What This Does Not Do

- SSH, cloud-init, service, or application-level validation.
- Manage long-lived infrastructure.
- Multi-region orchestration yet.

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

## Required Tags

Modeled resources use rediscoverable tags:

- `project=linode-image-lab`
- `run_id=<unique-id>`
- `mode=<capture|deploy|capture-deploy>`
- `component=<capture|deploy>`
- `ttl=<timestamp>`

## Manifest Output

Execute manifests use consistent top-level `status`, `steps`, `resources`,
`validation`, and `cleanup` fields. For `capture-deploy`, top-level `resources`
and `cleanup` summarize the combined run, while nested `capture` and `deploy`
blocks show phase-specific details.

Cleanup status values are literal: `deleted` means a temporary Linode was
deleted, `preserved` means a resource was kept or skipped for safety,
`completed` means combined cleanup finished, and `failed` means cleanup did not
complete.

## Independence and Intent

This is a personal, independent project. It is not affiliated with any employer
or organization.

It is designed as a public-safe workflow lab and does not use proprietary
systems, data, or credentials.
