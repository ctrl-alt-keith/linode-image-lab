# Examples

The `config/` directory contains reusable command-default examples for normal
lab workflows. The `policy/` directory contains a compact region policy example
for artifact shape and review.

The `smoke/` directory contains operational provider smoke configs for
`capture-replicate-deploy`. These configs intentionally exercise the
checked-in `policy/region-policy.toml` operator-owned geo groups and matching
geo image-replication groups where they exist. They are bounded provider
validation surfaces, not topology planners: dry-run remains the default, and
provider mutation still requires both `--execute` and `LINODE_TOKEN`.

Smoke configs with no checked-in geo image-replication group omit
`replication_groups` and say why in comments. That keeps missing coverage
reviewable instead of inventing fallback, nearest-region, or partial execution
behavior.
