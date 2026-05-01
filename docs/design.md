# Design

Linode Image Lab models the control-plane contract for custom image
capture/deploy workflows while keeping mutation paths explicit and narrow.

## Goals

- Provide a minimal CLI with deterministic JSON output.
- Create sanitized manifest previews for dry-run planning.
- Define a stable tag contract for resource rediscovery.
- Make cleanup selection testable without cloud access.
- Allow single-region capture and deploy execution only after explicit opt-in.

## Non-Goals

- No implicit Linode API mutations.
- No capture-deploy execution, multi-region execution, GitHub Actions mutation,
  or external scheduler integration.
- CI exists to run `make check`.

## Components

- `cli.py` owns command parsing and JSON output.
- `capture.py` owns dry-run capture manifests and execute-mode orchestration.
- `deploy.py` owns dry-run deploy manifests and execute-mode orchestration.
- `manifest.py` owns manifest creation, tag generation, and sanitized
  serialization.
- `regions.py` owns one-or-many region parsing.
- `cleanup.py` owns tag-based cleanup candidate selection.
- `redaction.py` owns recursive output sanitization.
- `linode_api.py` owns the mockable Linode client boundary.

## Manifest Foundation

Manifests are plain JSON-compatible dictionaries. They include schema version,
project, command, mode, regions, timestamps, dry-run status, tags, and planned
actions. Future milestones can add fields while preserving the required tag
contract.

M2 capture execution adds `execution_mode`, ordered `steps`, `resources`,
`capture_source`, `custom_image`, and `cleanup` fields. M3 deploy execution adds
`execution_mode`, ordered `steps`, `resources`, `deploy_source`,
`deploy_instance`, `validation`, and `cleanup` fields. Internal manifests may
carry provider resource identifiers required for cleanup and debugging. Normal
stdout uses sanitized serialization, which redacts provider identifiers before
printing.

## Capture Execution Boundary

`capture` without `--execute` remains non-mutating and does not read
`LINODE_TOKEN`. `capture --execute` requires exactly one region, a source image,
a Linode type, and `LINODE_TOKEN`.

The execute flow is intentionally linear:

1. preflight the token with non-mutating API calls,
2. create a tagged temporary capture-source Linode,
3. wait for readiness,
4. validate region, tags, and disk presence,
5. shut down the source Linode,
6. capture a custom image from the selected disk,
7. wait for image availability,
8. delete or preserve the source according to explicit flags.

## Deploy Execution Boundary

`deploy` without `--execute` remains non-mutating and does not read
`LINODE_TOKEN`. `deploy --execute` requires exactly one region, an existing
custom image id via `--image-id`, a Linode type, and `LINODE_TOKEN`.

The execute flow is intentionally linear:

1. preflight the token with non-mutating API calls,
2. create a tagged temporary deploy Linode from the custom image id,
3. wait for provider/API-level running status,
4. validate running status, requested region, and required tags,
5. delete or preserve the deploy instance according to explicit flags.

M3 does not perform SSH, cloud-init, service, or application readiness
validation.
