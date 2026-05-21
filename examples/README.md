# Examples

The `config/` directory contains reusable command-default examples for normal
lab workflows. The `policy/` directory contains a compact region policy example
for artifact shape and review. The `geo/` directory contains broader geo deploy
validation configs that use operator-owned deploy groups from the checked-in
policy.

The `smoke/` directory contains operational provider smoke configs for
`capture-replicate-deploy`. These configs intentionally exercise the
checked-in `policy/region-policy.toml` replication policy surface with one
explicit deploy region plus a matching geo image-replication group. They are
bounded provider validation surfaces, not broad deploy fan-out coverage or
topology planners: dry-run remains the default, and provider mutation still
requires both `--execute` and `LINODE_TOKEN`.

Geos with no checked-in known-good geo image-replication group do not have
execute smoke configs here. That keeps missing coverage reviewable without
creating accidental fail-closed runs or inventing fallback, nearest-region, or
partial execution behavior.

The `geo/` configs are intentionally broader and longer-running than smoke
configs. They validate deploy group surfaces across a geo, with replication
enabled only where a matching known-good geo image-replication group exists.
Deploy-only geos set `replication_enabled = false`, which explicitly disables
replication target resolution, capability checks, replication API calls, and
replica waits while preserving normal capture, deploy, and cleanup behavior.
