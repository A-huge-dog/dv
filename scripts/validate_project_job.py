#!/usr/bin/env python3
"""Recompute immutable Project Job lineage and artifact fingerprints."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from contracts.validator import accepted, load_document, validate
from domain.artifacts import (
    project_candidate_fingerprint,
    project_input_fingerprint,
    project_report_fingerprint,
)
from runtime.project_job import validate_project_input
from domain.review import (
    review_report_fingerprint,
    validate_review_report,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a completed Project Job")
    parser.add_argument("--job-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.job_root.resolve()
    try:
        project_input = load_document(
            root / "input_baseline/project_input_manifest.json")
        checkpoints = [
            load_document(path)
            for path in sorted(
                (root / "audit").glob("project_checkpoint*.json"))]
        checkpoint = max(checkpoints, key=lambda item: (
            item.get(
                "checkpoint_sequence",
                item.get("revision_number", 0)),
            1 if item.get("workflow_version") == "3.0" else 0,
        ))
        candidate = load_document(
            root / checkpoint["candidate_metadata_path"])
        review_request = load_document(
            root / checkpoint["review_request_path"])
        review_report = load_document(
            root / checkpoint["review_report_path"])
        review_validation = load_document(
            root / checkpoint["review_validation_path"])
        promotion = load_document(
            root / "approved/generated/manifests/project_testcase.json")
        report = load_document(
            root / "reports/project/project_report.json")
        approved = root / promotion["approved_path"]
        checks = {
            "input_contract": accepted(validate(
                "project_job_input", project_input)),
            "input_fingerprint":
                project_input["input_fingerprint"] ==
                project_input_fingerprint(project_input),
            "candidate_contract": accepted(validate(
                "project_testcase_candidate", candidate)),
            "candidate_fingerprint":
                candidate["candidate_fingerprint"] ==
                project_candidate_fingerprint(candidate),
            "review_report_contract": accepted(validate(
                "project_testcase_review_report", review_report)),
            "review_report_fingerprint":
                review_report["report_fingerprint"] ==
                review_report_fingerprint(review_report),
            "review_validation":
                review_validation == validate_review_report(
                    review_report, review_request, candidate,
                    project_input),
            "promotion_bytes":
                approved.is_file() and
                _sha256(approved) == promotion["fingerprint"] ==
                candidate["content_fingerprint"],
            "report_contract": accepted(validate(
                "project_job_report", report)),
            "report_fingerprint":
                report["report_fingerprint"] ==
                project_report_fingerprint(report),
            "job_identity":
                len({
                    project_input["job_id"], candidate["job_id"],
                    review_report["job_id"], promotion["job_id"],
                    report["job_id"],
                    root.name,
                }) == 1,
        }
        try:
            validate_project_input(project_input, root.parents[2])
            checks["input_semantics"] = True
        except Exception:
            checks["input_semantics"] = False
        result = {
            "status": "PASS" if all(checks.values()) else "FAIL",
            "job_id": root.name,
            "checks": checks,
            "report_fingerprint": report.get("report_fingerprint", ""),
        }
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0 if result["status"] == "PASS" else 1
    except Exception:
        print(json.dumps({
            "status": "FAIL",
            "job_id": root.name,
            "code": "INVALID_OR_STALE_PROJECT_JOB",
        }, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
