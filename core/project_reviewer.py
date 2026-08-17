"""Public PJ-002 Spec-only staged reviewer surface.

The implementation lives with the staged lineage validators so the provider
request, report enrichment, and deterministic validation use one policy.
No function in this module accepts RTL evidence.
"""
from __future__ import annotations

import copy
from typing import Any

from core.project_staged import (
    WORKFLOW_VERSION,
    artifact_fingerprint,
    build_review_report,
    build_review_request,
    provider_review_request,
    validate_review_report,
)
from scripts.dvlib import canonical_hash


class ReviewValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def review_request_fingerprint(value: dict[str, Any]) -> str:
    return artifact_fingerprint(value, "request_fingerprint")


def review_report_fingerprint(value: dict[str, Any]) -> str:
    return artifact_fingerprint(value, "report_fingerprint")


def review_issue_fingerprint(value: dict[str, Any]) -> str:
    return artifact_fingerprint(value, "issue_fingerprint")


def issue_set_fingerprint(report: dict[str, Any]) -> str:
    return canonical_hash(sorted(
        item["issue_fingerprint"] for item in report["findings"]))


__all__ = [
    "ReviewValidationError",
    "WORKFLOW_VERSION",
    "build_review_report",
    "build_review_request",
    "issue_set_fingerprint",
    "provider_review_request",
    "review_issue_fingerprint",
    "review_report_fingerprint",
    "review_request_fingerprint",
    "validate_review_report",
]
