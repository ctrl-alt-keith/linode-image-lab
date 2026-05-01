# AGENTS.md

This repository uses the shared `ai-workflow-playbook` as the canonical source
for general workflow rules. This file is the thin repo-local execution layer.
Repo-local rules take precedence only for repo-specific behavior.

## Repo Scope

- This repo contains a public-safe Linode Image Lab scaffold.
- M1 behavior established dry-run custom image capture/deploy planning,
  manifest modeling, tag contracts, cleanup selection logic, docs, and tests.
- M2 permits explicit single-region capture execution only through
  `capture --execute`; default behavior remains non-mutating.
- Do not add deploy, capture-deploy, multi-region execution, or additional real
  Linode mutation behavior until a later milestone explicitly asks for it.

## Public-Safe Boundary

- Treat every file as public.
- Do not commit secret values, private identifiers, non-public URLs, or
  workplace metadata.
- `LINODE_TOKEN` may appear only as an environment variable name.
- Normal stdout and stderr must not expose provider resource identifiers.
- Fixtures must be sanitized and live under `tests/fixtures/sanitized/`.

## Validation

- Use `make check` as the canonical validation entrypoint.
- Use `make security-check` for the public-safety scan.
- Keep validation implemented through the Makefile rather than direct tool
  invocation in normal workflow.

## Branches and PRs

- Branch from current `origin/main`.
- Use focused branch names such as `codex/bootstrap-m1-foundation`.
- Open PRs against `main`.
