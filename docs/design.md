# Design

Linode Image Lab models the control-plane contract for custom image
capture/deploy workflows while keeping mutation paths explicit and narrow.

## Goals

- Provide a minimal CLI with deterministic JSON output.
- Create sanitized manifest previews for dry-run planning.
- Define a stable tag contract for resource rediscovery.
- Make cleanup selection testable without cloud access.
- Allow single-region capture, deploy, and capture-deploy execution only after
  explicit opt-in.

## Non-Goals

- No implicit Linode API mutations.
- No multi-region execution, GitHub Actions mutation, or external scheduler
  integration.
- No infrastructure ownership or planning model.
- CI exists to run `make check`.

## Execution Model Boundary

Future configuration and future multi-region support are execution defaults for
disposable validation runs. They must not turn the tool into infrastructure
ownership. Runs remain ephemeral validation workflows with explicit mutation
entrypoints, provider/API-level validation, tagged temporary resources, and
cleanup as a first-class outcome.

## Components

- `cli.py` owns command parsing and JSON output.
- `config.py` owns explicit TOML config loading and safe default resolution.
- `capture.py` owns dry-run capture manifests and execute-mode orchestration.
- `deploy.py` owns dry-run deploy manifests and execute-mode orchestration.
- `capture_deploy.py` owns the combined capture-deploy execute orchestration.
- `manifest.py` owns manifest creation, tag generation, and sanitized
  serialization.
- `regions.py` owns one-or-many region parsing.
- `cleanup.py` owns tag-based cleanup candidate selection.
- `redaction.py` owns recursive output sanitization.
- `linode_api.py` owns the mockable Linode client boundary.

## Manifest Foundation

Manifests are plain JSON-compatible dictionaries. They include schema version,
project, command, mode, regions, timestamps, dry-run status, tags, and planned
actions. Serialized manifests use stable JSON formatting and redacted provider
identifiers.

M2 capture execution adds `execution_mode`, ordered `steps`, `resources`,
`capture_source`, `custom_image`, `validation`, and `cleanup` fields. M3 deploy
execution adds `execution_mode`, ordered `steps`, `resources`, `deploy_source`,
`deploy_instance`, `validation`, and `cleanup` fields. M4 capture-deploy
execution adds top-level `execution_mode`, `steps`, `resources`, `capture`,
`deploy`, `validation`, and `cleanup` fields. Top-level `resources` is the
combined resource list; nested `capture.resources` and `deploy.resources` are
phase-specific views. Top-level `cleanup` is the combined cleanup summary;
nested cleanup blocks preserve phase-specific status. Internal manifests may
carry provider resource identifiers required for cleanup and debugging. Normal
stdout uses sanitized serialization, which redacts provider identifiers before
printing.

## Config Defaults

Config is opt-in through `--config PATH` and uses TOML `schema_version = 1`.
The config file can provide execution defaults in `[defaults]`, `[capture]`,
`[deploy]`, `[capture-deploy]`, and `[cleanup]` tables. CLI flags take
precedence over command-specific config, command-specific config takes
precedence over `[defaults]`, and existing generated defaults remain last.

Supported config values are intentionally narrow:

- `region` or `regions`,
- `ttl`,
- `source_image` for capture and capture-deploy,
- `image_id` for deploy,
- `type` for capture, deploy, and capture-deploy.

`--execute`, preservation flags, run id fields, image labels, tokens,
passwords, SSH keys, root passwords, and cloud-init or user-data fields are not
configurable. Unknown keys and secret-like keys fail before command execution.

Multi-region config is accepted for dry-run manifests. Execute mode still
requires exactly one effective region and fails before token lookup when config
or CLI values resolve to multiple regions.

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

Capture validation is provider/API-level only. It verifies region, required
tags, disk presence, image availability, and image tags from API responses.

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

## Capture-Deploy Execution Boundary

`capture-deploy` without `--execute` remains non-mutating and does not read
`LINODE_TOKEN`. `capture-deploy --execute` requires exactly one region, a source
image, a Linode type, and `LINODE_TOKEN`.

The execute flow reuses the capture and deploy internals:

1. run capture with `mode=capture-deploy` and `component=capture`,
2. pass the internal custom image id to deploy without exposing it in stdout,
3. run deploy with `mode=capture-deploy` and `component=deploy`,
4. surface deploy's provider/API-level validation result,
5. delete the temporary capture-source Linode when its current-run tags match,
6. delete or preserve the temporary deploy Linode according to
   `--preserve-instance`,
7. preserve the custom image as the deliverable.

Capture-deploy cleanup is tag-scoped. It only deletes resources carrying all
required tags for the current run, including the matching component tag.
Because capture and deploy remain independently reusable, capture-deploy runs
each phase's non-mutating API preflight. Seeing two `preflight_api_access`
steps in a combined manifest is expected.

## Cleanup Semantics

Cleanup status is narrow and literal. `deleted` means a temporary Linode was
deleted after matching current-run tags. `preserved` means no deletion occurred
for that resource because preservation was requested or tags did not match.
`completed` is reserved for combined cleanup after the phase cleanup blocks
finish. `failed` means cleanup did not complete. Preserved entries include a
`reason`; the custom image uses `deliverable`.
