# Product Boundary

Linode Image Lab owns guarded operational validation for Linode custom-image
workflows. It plans requested work, validates provider inputs, optionally
executes a bounded workflow, cleans up resources within explicit ownership
rules, and emits redacted evidence of what happened.

It is not an infrastructure management tool. Infrastructure mutation is not the
product. Bounded, evidence-producing execution is the product.

## Product Role

The repository owns:

- guarded operational validation,
- bounded execution after explicit operator request,
- provider/API-level validation,
- redacted operational evidence,
- cleanup within explicit ownership boundaries,
- operational proof of image workflow behavior.

Execution exists to answer whether a requested image workflow was planned,
validated, run when requested, cleaned up according to the tag contract, and
reported safely.

## Core Invariant

Every provider mutation must be explicitly requested, preceded by bounded
validation, limited to the current workflow or explicitly owned resources,
cleaned up according to repository ownership rules, and reported as public-safe
evidence without becoming durable infrastructure ownership.

This means mutation paths stay opt-in. Preflight checks run before mutation.
Created or discovered resources are handled only when they match the current
workflow, explicit operator input, or the repository's managed tag contract.
Cleanup is part of the workflow result, not a background ownership promise.
Normal output remains safe to publish and does not expose provider identifiers.

## Product Object

The primary durable product object is the operational run receipt.

The run receipt is the redacted manifest that records:

- requested workflow intent,
- planned actions,
- provider validation results,
- execution steps when `--execute` is supplied,
- run evidence,
- cleanup evidence,
- preserved or failed cleanup entries when cleanup cannot or should not delete.

The receipt is evidence of a bounded operation. Custom images may remain as
explicit workflow deliverables when intentionally requested, but the durable
product remains the operational run receipt and public-safe evidence, not
ongoing ownership of infrastructure, deployments, accounts, or environments.

## Repository Boundaries

Operator intent comes from outside this repository. CLI flags, config files,
and existing provider resource identifiers express a requested workflow; they
do not make this repository the owner of broader cloud intent.

Long-lived infrastructure ownership belongs elsewhere. This repository may read
or reference existing provider resources when required for validation or a
bounded execute path, but those resources remain operator-owned inputs unless
the current workflow or tag contract explicitly owns them.

Related repository boundaries:

- Kubernetes and LKE orchestration belong in `lke-image-lab`.
- Backup and restore workflows belong in `linode-backup-lab`.
- Evidence bundle architecture belongs in `trusted-ai-environment`.

Those boundaries keep Linode Image Lab focused on image workflow proof rather
than deployment, backup, restore, or evidence-platform ownership.

## Operational Philosophy

Execution exists to produce trustworthy operational evidence. A successful run
shows that the requested image workflow passed bounded validation, provider
checks, optional execution, and cleanup reporting.

Execution does not imply:

- infrastructure ownership,
- orchestration,
- scheduling,
- declared target-state control,
- autonomous remediation,
- cloud lifecycle management.

The repository should continue to prefer explicit inputs, fail-closed
validation, narrow mutation, public-safe manifests, and cleanup that respects
tag and ownership boundaries.

## Product Decision Filter

Use these questions when deciding whether a change belongs here:

- Does this improve guarded operational validation?
- Does this improve trustworthy operational evidence?
- Does this strengthen bounded execution?
- Does this preserve explicit ownership boundaries?
- Does this begin owning infrastructure?
- Does this become orchestration?
- Does this become scheduling?
- Does this become cloud lifecycle management?

Changes that improve validation, receipts, cleanup evidence, or bounded
execution usually fit. Changes that make the repository own infrastructure,
coordinate deployments, schedule work, reconcile long-lived environments, or
operate a general cloud lifecycle do not fit.

## Non-Goals

- Durable infrastructure ownership.
- Orchestration.
- Account target-state control.
- Autonomous remediation.
- Backup and restore.
- Scheduling.
- Broad cloud management.
- Becoming a deployment framework.
