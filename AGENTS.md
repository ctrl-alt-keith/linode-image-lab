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
- M3 permits explicit single-region deploy execution only through
  `deploy --execute`; default behavior remains non-mutating.
- M4 permits explicit single-region capture-deploy execution only through
  `capture-deploy --execute`; default behavior remains non-mutating.
- M5 permits explicit sequential multi-region capture-deploy execution only
  through `capture-deploy --execute`; default behavior remains non-mutating.
- Do not add additional real Linode mutation behavior until a later milestone
  explicitly asks for it.

## Execution Model Boundary

- Linode Image Lab is an ephemeral validation workflow tool, not durable
  infrastructure ownership.
- Configuration may provide execution defaults only.
- Do not introduce desired-state management concepts.
- Do not add state files, drift reconciliation, resource graphs, dependency
  planning, or Terraform-like behavior.
- Future multi-region support must remain execution fan-out for disposable
  validation runs, not infrastructure ownership.
- Keep cleanup and validation first-class in any execution path.

## Provider Assumptions

- Before changing behavior, docs, tests, or user-facing claims that depend on
  Linode provider semantics, verify the assumption against public provider
  documentation.
- Examples include image region availability, API resource lifecycle, tagging
  behavior, cleanup safety, rate limits, and error or status semantics.
- Do not encode guessed provider limitations. If public docs are unclear, state
  the uncertainty and keep behavior conservative.
- When relevant, cite or summarize the verified source in PR notes or docs.

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
