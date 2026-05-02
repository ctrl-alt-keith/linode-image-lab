# Compliance and Public Safety

This repository is intended to be safe for public publication.

## Allowed

- Public product names needed to explain the project.
- Environment variable names such as `LINODE_TOKEN`.
- Sanitized fixtures under `tests/fixtures/sanitized/`.
- Deterministic mock resource identifiers.
- Redacted/export-safe manifest examples.

## Not Allowed

- Secret values.
- Personal contact values.
- Non-public service URLs.
- Private account names or machine names.
- Workplace metadata unrelated to this public project.
- Provider resource identifiers in normal stdout or stderr examples.

## Review Checklist

Before opening a PR:

- run `make check`,
- confirm fixtures are sanitized,
- confirm dry-run commands remain non-mutating,
- confirm real mutation remains limited to explicit `capture --execute`,
  `deploy --execute`, `capture-deploy --execute`, and `cleanup --execute`,
- confirm cleanup only targets resources with complete matching tags,
- confirm docs describe behavior without private context.
