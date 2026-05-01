# Design

Linode Image Lab M1 models the control-plane contract for a future image
freeze/thaw workflow without implementing real Linode mutations.

## Goals

- Provide a minimal CLI with deterministic JSON output.
- Create sanitized manifest previews for dry-run planning.
- Define a stable tag contract for resource rediscovery.
- Make cleanup selection testable without cloud access.

## Non-Goals

- No real Linode API mutations.
- No scheduler integration.
- No deployment automation.
- No GitHub Actions integration.

## Components

- `cli.py` owns command parsing and JSON output.
- `manifest.py` owns manifest creation, tag generation, and serialization.
- `regions.py` owns one-or-many region parsing.
- `cleanup.py` owns tag-based cleanup candidate selection.
- `redaction.py` owns recursive output sanitization.
- `linode_api.py` is a placeholder boundary for a future client.

## Manifest Foundation

Manifests are plain JSON-compatible dictionaries. They include schema version,
project, command, mode, regions, timestamps, dry-run status, tags, and planned
actions. Future milestones can add fields while preserving the required tag
contract.
