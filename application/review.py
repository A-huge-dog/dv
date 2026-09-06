"""Independent initial and final review application handlers."""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from contracts.validator import accepted, load_document, validate
from domain.artifacts import (
    STAGE1 as UNIT_STAGE1,
    STAGE2 as UNIT_STAGE2,
    artifact_fingerprint,
)
from domain.review import (
    REVIEW_EVIDENCE_CHECK_TOOL,
    REVIEW_TOOL,
    _validate_review_scope,
    build_review_report,
    build_review_request,
    check_review_evidence,
    provider_review_request,
    validate_review_report,
)


@dataclass(frozen=True)
class ReviewInput:
    phase: Literal["INITIAL", "FINAL"]
    project_input: dict[str, Any]
    job_root: Path
    spec_evidence: list[dict[str, Any]]
    sources: dict[str, str]
    spec_fingerprint: str
    scenario_ac_map: dict[str, Any]
    ac_testcase_map: dict[str, Any]
    shards: list[dict[str, Any]]
    candidate: dict[str, Any]
    reviewer_probe: dict[str, Any]
    review_round: int
    budget: dict[str, Any]
    routing_summary: dict[str, Any]
    previous_report: dict[str, Any] | None = None
    repair_lineage: list[dict[str, Any]] | None = None
    artifact_suffix: str = ""
    review_storage_revision: int | None = None
    attempt_zero_request_path: str | None = None
    attempt_zero_provider_tag: str | None = None


@dataclass(frozen=True)
class ReviewResult:
    report: dict[str, Any]
    validation: dict[str, Any]
    output_references: dict[str, str]
    replayed: bool


@dataclass(frozen=True)
class ReviewDependencies:
    error: type[Exception]
    root: Path
    policy_fingerprint: str
    uvm_context: Mapping[str, Any]
    routing_context: Callable[..., dict[str, Any]]
    persist_artifact: Callable[[Path, str, dict[str, Any]], None]
    incremental_store: Callable[[Path], Any]
    owner_scope_fingerprint: Callable[..., str]
    inspect_no_rtl: Callable[..., None]
    invoke: Callable[..., dict[str, Any]]
    response_candidate: Callable[[dict[str, Any]], dict[str, Any]]
    persist_review_rejection: Callable[..., dict[str, Any]]


class _ReviewHandler:
    phase: Literal["INITIAL", "FINAL"]

    def __init__(self, dependencies: ReviewDependencies):
        self.dependencies = dependencies

    def handle(self, command: ReviewInput) -> ReviewResult:
        if command.phase != self.phase:
            raise self.dependencies.error(
                "INVALID_INPUT",
                "{} review handler received {} input".format(
                    self.phase, command.phase))
        deps = self.dependencies
        value, job_root = command.project_input, command.job_root
        map1, map2, candidate = (
            command.scenario_ac_map, command.ac_testcase_map,
            command.candidate)
        routing_context = deps.routing_context(
            job_root, value, map1, command.routing_summary)
        fresh_review_request = build_review_request(
            value, command.spec_evidence, command.spec_fingerprint,
            map1, map2, command.shards, candidate,
            command.reviewer_probe, command.review_round, deps.error,
            routing_context, command.previous_report,
            command.repair_lineage, deps.uvm_context)
        if command.artifact_suffix and not re.fullmatch(
                r"\.[a-z0-9.-]+", command.artifact_suffix):
            raise deps.error("INVALID_INPUT", "Reviewer artifact suffix is unsafe")
        artifact_tag = "r{:03d}{}".format(
            command.review_round, command.artifact_suffix)
        request_path = "staging/reviews/review_request.{}.json".format(
            artifact_tag)
        report_path = "staging/reviews/review_report.{}.json".format(
            artifact_tag)
        validation_path = (
            "staging/validations/review_validation.{}.json".format(
                artifact_tag))
        if (command.attempt_zero_request_path is None) != (
                command.attempt_zero_provider_tag is None):
            raise deps.error(
                "INVALID_INPUT",
                "Reviewer attempt-zero recovery evidence is incomplete")
        recovered_attempt_zero = command.attempt_zero_request_path is not None
        legacy_review_request: dict[str, Any] | None = None
        if recovered_attempt_zero:
            if (not re.fullmatch(
                    r"staging/reviews/review_request\.r[0-9]{3}"
                    r"\.repair\.[0-9a-f]{16}\.json",
                    str(command.attempt_zero_request_path)) or
                    not re.fullmatch(
                        r"review\.r[0-9]{3}\.repair\.[0-9a-f]{16}",
                        str(command.attempt_zero_provider_tag))):
                raise deps.error(
                    "INVALID_INPUT",
                    "Reviewer attempt-zero recovery identity is invalid")
            legacy_path = job_root / str(command.attempt_zero_request_path)
            if not legacy_path.is_file() or legacy_path.is_symlink():
                raise deps.error(
                    "PARTIAL_ARTIFACT",
                    "Reviewer attempt-zero request evidence is unavailable")
            legacy_review_request = load_document(legacy_path)
        persisted_request_path = job_root / request_path
        if persisted_request_path.exists():
            if (not persisted_request_path.is_file()
                    or persisted_request_path.is_symlink()):
                raise deps.error(
                    "STALE_EVIDENCE",
                    "persisted Reviewer request is not a regular file")
            review_request = load_document(persisted_request_path)
            if (not accepted(validate(
                    "project_testcase_review_request", review_request)) or
                    review_request.get("request_fingerprint") !=
                    artifact_fingerprint(
                        review_request, "request_fingerprint")):
                raise deps.error(
                    "STALE_EVIDENCE",
                    "persisted Reviewer request is stale or malformed")
            _validate_review_scope(review_request, map1, deps.error)
            ignored = {"created_at", "request_fingerprint"}
            if ({key: item for key, item in review_request.items()
                 if key not in ignored} !=
                    {key: item for key, item in fresh_review_request.items()
                     if key not in ignored}):
                raise deps.error(
                    "STALE_EVIDENCE",
                    "persisted Reviewer request lineage conflicts with "
                    "the current staged bundle")
        else:
            review_request = (
                legacy_review_request
                if legacy_review_request is not None
                else fresh_review_request)
            deps.persist_artifact(job_root, request_path, review_request)
        if legacy_review_request is not None:
            ignored = {"created_at", "request_fingerprint"}
            if (
                not accepted(validate(
                    "project_testcase_review_request", legacy_review_request))
                or legacy_review_request.get("request_fingerprint") !=
                    artifact_fingerprint(
                        legacy_review_request, "request_fingerprint")
                or {key: item for key, item in legacy_review_request.items()
                    if key not in ignored} !=
                    {key: item for key, item in review_request.items()
                     if key not in ignored}
                or review_request != legacy_review_request
            ):
                raise deps.error(
                    "STALE_EVIDENCE",
                    "Reviewer attempt-zero request conflicts with loop authority")
        persisted_report_path = job_root / report_path
        persisted_validation_path = job_root / validation_path
        replayed = persisted_report_path.exists() or persisted_validation_path.exists()
        if replayed:
            if (
                not persisted_report_path.is_file()
                or persisted_report_path.is_symlink()
                or not persisted_validation_path.is_file()
                or persisted_validation_path.is_symlink()
            ):
                raise deps.error(
                    "PARTIAL_ARTIFACT",
                    "persisted Final Reviewer artifacts are incomplete")
            report = load_document(persisted_report_path)
            validation = load_document(persisted_validation_path)
            expected_validation = validate_review_report(
                report, review_request, map1, map2, candidate,
                command.sources, deps.error)
            if validation != expected_validation:
                raise deps.error(
                    "CONFLICTING_REPLAY",
                    "persisted Final Reviewer validation conflicts")
            output = self._persist_review_units(command, report)
            return ReviewResult(report, validation, {
                "review_request": request_path,
                "review_report": report_path,
                "review_validation": validation_path,
                "review_units": output,
            }, True)

        base_provider_request = provider_review_request(review_request)
        deps.inspect_no_rtl(base_provider_request, value, deps.root, deps.error)
        base_tag = "review.{}".format(artifact_tag)
        rejections: dict[int, dict[str, Any]] = {}
        for path in sorted(job_root.glob(
                "audit/pj002_rejected_review_response.*.json")):
            item = load_document(path)
            if item.get("review_round") != command.review_round:
                continue
            attempt_value = item.get("attempt")
            if (type(attempt_value) is not int or attempt_value < 0 or
                    item.get("record_fingerprint") != artifact_fingerprint(
                        item, "record_fingerprint") or
                    item.get("review_request_fingerprint") !=
                        review_request["request_fingerprint"] or
                    attempt_value in rejections):
                raise deps.error(
                    "STALE_EVIDENCE",
                    "persisted Reviewer rejection sequence is invalid")
            rejections[attempt_value] = item
        if sorted(rejections) != list(range(len(rejections))):
            raise deps.error(
                "STALE_EVIDENCE",
                "persisted Reviewer rejection sequence has a gap")
        attempt = len(rejections)
        rejection = rejections.get(attempt - 1)
        provider_request = copy.deepcopy(base_provider_request)
        tag = base_tag
        rejection_request_path = request_path
        if attempt == 0 and recovered_attempt_zero:
            tag = str(command.attempt_zero_provider_tag)
            rejection_request_path = str(command.attempt_zero_request_path)
        elif attempt:
            tag = "{}.retry{:03d}".format(base_tag, attempt)
            provider_request["metadata"]["retry_attempt"] = attempt
            provider_request["metadata"]["review_attempt"] = attempt
            provider_request["messages"].append({
                "role": "USER",
                "content": json.dumps({
                    "review_correction_feedback": rejection,
                    "required_action": (
                        "Submit a new complete Reviewer candidate. Correct "
                        "the recorded typed validation failures without "
                        "expanding Owner scope or changing upstream data."),
                }, sort_keys=True, ensure_ascii=False),
            })
        session_id = "REVIEWSESSION.R{:03d}.{}.ATTEMPT{:03d}".format(
            command.review_round,
            review_request["request_fingerprint"][:16].upper(), attempt)

        def submit_review(_arguments: dict[str, Any],
                          context: dict[str, Any]) -> dict[str, Any]:
            submitted_report = build_review_report(
                review_request, candidate, context["response"],
                command.sources, deps.error, attempt)
            validate_review_report(
                submitted_report, review_request, map1, map2,
                candidate, command.sources, deps.error)
            return submitted_report

        def preflight_review(arguments: dict[str, Any]) -> dict[str, Any]:
            return check_review_evidence(
                arguments, review_request, candidate, deps.error)

        try:
            report = deps.invoke(
                value, job_root, "REVIEWER", provider_request,
                command.budget, tag,
                submission_handlers={REVIEW_TOOL: submit_review},
                retrieval_handlers={
                    REVIEW_EVIDENCE_CHECK_TOOL: preflight_review},
                session_id=session_id)
            validation = validate_review_report(
                report, review_request, map1, map2, candidate,
                command.sources, deps.error)
        except deps.error as caught:
            response_path = job_root / (
                "audit/pj002_provider_response.{}.json".format(tag))
            if not response_path.is_file() or response_path.is_symlink():
                raise
            response = load_document(response_path)
            try:
                build_review_report(
                    review_request, candidate, response,
                    command.sources, deps.error, attempt)
            except deps.error as validation_error:
                caught = validation_error
            try:
                prior_candidate = deps.response_candidate(response)
            except deps.error:
                prior_candidate = {}
            deps.persist_review_rejection(
                job_root, value, tag, review_request,
                rejection_request_path, command.review_round, attempt,
                response, prior_candidate, caught)
            raise deps.error(
                "ATTEMPT_PAUSED",
                "Reviewer candidate attempt failed; rerun the same Job "
                "to create a new Reviewer attempt") from caught
        deps.persist_artifact(job_root, report_path, report)
        deps.persist_artifact(job_root, validation_path, validation)
        output = self._persist_review_units(command, report)
        return ReviewResult(report, validation, {
            "review_request": request_path,
            "review_report": report_path,
            "review_validation": validation_path,
            "review_units": output,
        }, False)

    def _persist_review_units(
            self, command: ReviewInput, report: dict[str, Any]) -> str:
        deps = self.dependencies
        store = deps.incremental_store(command.job_root)
        _, stage1_units = store.load(
            UNIT_STAGE1, "CURRENT", command.scenario_ac_map["revision"],
            command.project_input["job_id"])
        _, stage2_units = store.load(
            UNIT_STAGE2, "CURRENT", command.ac_testcase_map["revision"],
            command.project_input["job_id"])
        _, stage3_units, _ = store.load_stage3(
            command.candidate["revision"], command.project_input["job_id"])
        review_bundle = store.persist_review(
            report, stage1_units, stage2_units, stage3_units,
            deps.owner_scope_fingerprint(
                command.job_root, command.project_input),
            deps.policy_fingerprint, command.spec_fingerprint,
            storage_revision=command.review_storage_revision)
        return review_bundle["path"]


class InitialReviewHandler(_ReviewHandler):
    phase = "INITIAL"


class FinalReviewHandler(_ReviewHandler):
    phase = "FINAL"
