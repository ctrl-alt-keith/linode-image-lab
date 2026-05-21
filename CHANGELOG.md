# Changelog

## Unreleased

- Add `region-policy generate` and `region-policy validate` for
  version-controlled provider-backed region policy artifacts that separate
  generated provider facts from operator-owned grouping intent.
- Add generated helper groups and a checked-in `policy/region-policy.toml`
  provider policy snapshot for reviewing provider region and capability drift.
- Add operator-owned geo groups to the checked-in region policy, including
  separate image-replication groups where the current policy supports them.
- Add capability-scoped generated country helper groups, such as
  `country_us_object_storage`, while preserving strict execution-time
  capability validation.
- Add narrow documented provider overrides for image-replication generated
  helper groups, including `country_us_image_replication`, without mutating raw
  provider facts or provider-backed capability groups.
- Allow `capture-replicate-deploy` to consume checked-in region policy groups
  as replication targets while keeping deploy regions explicit and mutation
  gated behind `--execute`.
- Allow `capture-replicate-deploy` to consume `deploy_groups` as deploy target
  expansion while keeping deploy intent and replication intent separate.
- Stop treating explicit deploy regions as requested replication targets when
  `capture-replicate-deploy` is configured with replication regions or groups;
  the replication request still preserves provider-reported existing image
  regions before adding requested replication targets.
- Resolve relative TTL inputs such as `"1 day"` at command runtime while
  preserving absolute UTC TTL values in manifests and cleanup tags.
- Add `capture-replicate-deploy` to capture in the first requested region,
  explicitly replicate the captured image to deploy regions, wait for requested
  replicas to report available, and deploy with cleanup-first manifests.
- Include sanitized provider status and reason details when image replication
  submission fails.
- Validate explicit image replication targets for the provider `Object Storage`
  capability before capture-replicate-deploy creates temporary Linodes.
- Add `replicate` to dry-run and explicitly execute custom image replication
  while preserving dry-run-first and mutation-gated behavior.
- Add `firewall-sync` to consume a private trusted network registry and sync
  one managed Linode firewall allowlist rule with dry-run-first behavior.
- Add TTL-aware lab artifact tags to custom images produced during capture
  flows so cleanup can identify expired image artifacts.
- Extend cleanup discovery to include expired lab-owned custom images and allow
  standalone `cleanup --execute` to delete those disposable artifacts.
- Preserve cleanup ownership boundaries when `image_project_tag` is customized
  by selecting only matching lab-owned images and skipping untagged, malformed,
  mismatched, or deliverable-tagged images.

## 0.3.0

- Add deploy config and execution support for firewall IDs, authorized SSH
  keys, and file-based metadata user data.
- Add bounded parallel multi-region `capture-deploy --execute`.
- Clarify manifest lifecycle tags and configurable image project tags.
- Add release recovery helpers and polish release-process documentation.
- Update provider assumptions, security guidance, and redaction documentation.
- Add CI, Dependabot, license, Makefile help, and repo workflow hygiene updates.
- Expand unit test coverage for config, deploy, capture-deploy, provider, and
  redaction paths.

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
- Add sequential multi-region `capture-deploy --execute`, capturing once and
  deploying the resulting custom image to each requested region in order.
- Add authoritative-source checking and provider-assumptions documentation for
  public API claims.
- Add a human-gated live smoke target with configurable smoke region.

## 0.1.0

- First public-safe release of Linode Image Lab.
- Includes dry-run-first planning, capture, deploy, capture-deploy, cleanup,
  sanitized manifests, and repo validation.
- Adds lightweight, human-gated Makefile support for tagged GitHub releases.
