# Changelog

## 0.2.0

- Require Python 3.12 or newer.
- Add config-backed execution defaults and `config validate` reports with
  precedence, effective defaults, and source labels.
- Add executable cleanup for expired tagged temporary Linodes, including
  re-fetch-before-delete safety checks for `cleanup --execute`.
- Add provider preflight checks for token access, requested region, Linode type,
  and source or deploy image before mutating resources.
- Add structured validation results to execution manifests.
- Add bounded retries for read/list/poll operations, including `Retry-After`
  and `X-RateLimit-Reset` support for `429` responses.
- Add bounded parallel multi-region `capture-deploy --execute`, capturing once
  and deploying the resulting custom image to each requested region.
- Add authoritative-source checking and provider-assumptions documentation for
  public API claims.
- Add a human-gated live smoke target with configurable smoke region.

## 0.1.0

- First public-safe release of Linode Image Lab.
- Includes dry-run-first planning, capture, deploy, capture-deploy, cleanup,
  sanitized manifests, and repo validation.
- Adds lightweight, human-gated Makefile support for tagged GitHub releases.
