# AGENTS.md

This repository uses the shared `ai-workflow-playbook` as the canonical source
for general workflow rules. This file is the thin repo-local execution layer.
Repo-local rules take precedence only for repo-specific behavior.

## Startup And Interaction Mode

- Start with `ai-workflow-playbook/docs/start-here.md` before repository or
  software work.
- Before acting, select the interaction mode from
  `ai-workflow-playbook/docs/repo-readiness.md`: implementation, review/audit,
  or orchestration/prompt-authoring.
- Implementation agents make explicit repo changes and carry them through
  validation, commit, push, and PR delivery.
- Review/audit agents inspect and report findings without mutating the repo.
- Orchestration/prompt-authoring agents produce complete, self-contained
  handoffs or prompts unless explicitly asked to implement.

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
- M5 permits explicit bounded multi-region capture-deploy execution only through
  `capture-deploy --execute`; default behavior remains non-mutating.
- `firewall-sync --execute` may update only the documented single managed
  inbound allowlist rule on an existing Cloud Firewall.
- Do not add additional real Linode mutation behavior until a later milestone
  explicitly asks for it.

## Execution Model Boundary

- Linode Image Lab is an ephemeral validation workflow tool, not durable
  infrastructure ownership.
- Configuration may provide execution defaults only.
- Do not introduce desired-state management concepts.
- Do not add state files, drift reconciliation, resource graphs, dependency
  planning, or Terraform-like behavior.
- Do not expand `firewall-sync` into general firewall management, firewall
  creation or attachment, outbound policy management, continuous sync, or broad
  firewall reconciliation.
- Future multi-region support must remain execution fan-out for disposable
  validation runs, not infrastructure ownership.
- Keep cleanup and validation first-class in any execution path.

## File Placement

- Put source code under `src/linode_image_lab/`.
- Put unit tests under `tests/unit/`.
- Put repo documentation under `docs/`.
- Put example config files under `examples/config/`.
- Put sanitized fixtures under `tests/fixtures/sanitized/`.

## Local Execution

- Run commands from this repository working directory by default.
- Keep temporary workflow state repo-local, for example `.worktrees/`.
- Use direct command execution for ordinary repo commands such as `git ...`,
  `gh ...`, `make ...`, `python ...`, and repo-local scripts or tools.
- Before using `zsh`, `bash`, `sh`, `zsh -lc`, `bash -lc`, `sh -c`, aliases, or
  equivalent wrapper shells, check whether the command has a direct form and
  use that direct form when it does.
- Use shell wrappers only when shell syntax is genuinely required, such as
  pipelines, redirection, glob expansion, command chaining, scoped environment
  assignment, compound commands, or shell builtins.

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
- `make check` runs `make security-check` and `make test`.
- `make smoke` requires `LINODE_TOKEN` and `SMOKE_EXECUTE=1`, creates real
  Linode resources, and remains manual-only outside canonical PR validation.
- Release targets and the CI authoritative-source scan sit outside the local
  blocking `make check` path.
- Keep validation implemented through the Makefile rather than direct tool
  invocation in normal workflow.

## Branches and PRs

- Branch from current `origin/main`.
- Follow the shared playbook branch naming guidance; use focused,
  purpose-based names such as `docs/<short-name>` or `feat/<short-name>`.
- Open PRs against `main`.
