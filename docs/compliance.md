# Compliance and Public Safety

This repository is intended to be safe for public publication.

## Allowed

- Public product names needed to explain the project.
- Environment variable names such as `LINODE_TOKEN`.
- Sanitized fixtures under `tests/fixtures/sanitized/`.
- Deterministic mock resource identifiers.

## Not Allowed

- Secret values.
- Personal contact values.
- Non-public service URLs.
- Private account names or machine names.
- Workplace metadata unrelated to this public project.

## Review Checklist

Before opening a PR:

- run `make check`,
- confirm fixtures are sanitized,
- confirm commands remain dry-run or placeholder-only,
- confirm docs describe behavior without private context.
