"""Run one reviewed WAN cutover from a controller-created Job."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from datetime import datetime
import sys

from .acl_reconcile import (
    EXACT_WAN_WRITE_WINDOW, ACLReconcileError, FirstWanCutover, apply_reviewed_exact_wan_acl,
)
from .acl_wan_command import configured_wan_inputs
from .manifest import serialize_manifest
from .registry_version_selection import RegistryVersionSelection, parse_selection, read_protected_document

EXECUTE_FLAG = "--execute-approved-cutover"


def load_approval(path: str, receipt_dir: str) -> tuple[RegistryVersionSelection, str, FirstWanCutover]:
    try:
        doc = read_protected_document(path, limit=4096)
        if set(doc) != {"selection", "reviewed_proof", "revision", "not_before", "expires_at"}:
            raise ValueError
        first_cutover = FirstWanCutover(
            revision=doc["revision"],
            not_before=datetime.fromisoformat(doc["not_before"]),
            expires_at=datetime.fromisoformat(doc["expires_at"]),
            receipt_dir=receipt_dir,
        )
        return parse_selection(doc["selection"]), doc["reviewed_proof"], first_cutover
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ACLReconcileError("invalid_first_wan_cutover_approval") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config", required=True)
    parser.add_argument("--approval-file", required=True)
    parser.add_argument("--receipt-dir", required=True)
    parser.add_argument(EXECUTE_FLAG, action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        selection, reviewed_proof, first_cutover = load_approval(args.approval_file, args.receipt_dir)
        inputs = configured_wan_inputs(args.config, command="acl_wan_first_run", option=EXECUTE_FLAG)
        result = apply_reviewed_exact_wan_acl(
            **inputs, selection=selection, reviewed_proof=reviewed_proof,
            max_dashboard_age=EXACT_WAN_WRITE_WINDOW, writer_session=nullcontext(), first_cutover=first_cutover,
        )
    except ACLReconcileError as error:
        result = {
            "outcome_version": 1, "operation": "first_wan_cutover", "execution_mode": "execute",
            "status": error.status, "failure": str(error), "enforcement_verified": False,
        }
    sys.stdout.write(serialize_manifest(result))
    return 0 if result["status"] == "enforced" else 1


if __name__ == "__main__":
    raise SystemExit(main())
