"""Standalone, test-only semantic review of a source Stage 3 candidate."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from contracts.validator import accepted, load_document, validate
from core.atomic_artifact import is_pending_artifact
from core.project_job import ProjectJobError, ProjectJobWorkflow
from core.project_stage3 import (
    SOURCE_JOB_ID, TEST_JOB_ID, StandaloneStage3Workflow,
    load_stage3_provider_config,
)
from core.project_staged import (
    StagedProjectWorkflow, artifact_fingerprint, build_review_report,
    build_review_request, provider_review_request, validate_review_report,
    validate_testcase_candidate,
)
from scripts.dvlib import canonical_hash


SUBMISSION_PATH = "input_baseline/stage3_reviewer_submission.yaml"
MANIFEST_PATH = "input_baseline/stage3_reviewer_manifest.json"
RESULT_PATH = "audit/stage3_reviewer_test_result.json"
_CANDIDATE = re.compile(
    r"testcase\.r[0-9]{3}\.json")


def validate_stage3_reviewer_submission(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not accepted(validate(
            "project_stage3_reviewer_submission", value)):
        raise ProjectJobError(
            "INVALID_SCHEMA", "standalone Reviewer YAML is invalid")
    return copy.deepcopy(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class StandaloneStage3ReviewerWorkflow(StandaloneStage3Workflow):
    """Review one immutable source candidate without changing that source."""

    def _independent_candidate(self, relative: str) -> Path:
        """Resolve a separately supplied testcase candidate within the workspace."""
        if not isinstance(relative, str):
            raise ProjectJobError(
                "TOOL_PERMISSION_DENIED",
                "independent testcase candidate path is unsafe")
        path = Path(relative)
        if (not _CANDIDATE.fullmatch(path.name) or path.is_absolute() or
                ".." in path.parts or
                any(part.startswith(".") for part in path.parts)):
            raise ProjectJobError(
                "TOOL_PERMISSION_DENIED",
                "independent testcase candidate path is unsafe")
        root = self.root.resolve()
        unresolved = root / path
        cursor = root
        for part in path.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ProjectJobError(
                    "TOOL_PERMISSION_DENIED",
                    "independent testcase candidate contains a symlink")
        resolved = unresolved.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as caught:
            raise ProjectJobError(
                "TOOL_PERMISSION_DENIED",
                "independent testcase candidate escapes the workspace") from caught
        if (not resolved.is_file() or unresolved.is_symlink() or
                not unresolved.is_file()):
            raise ProjectJobError(
                "BLOCKED_INPUT",
                "independent testcase candidate is missing or unsafe")
        return resolved

    def _source_bundle(self, source: dict[str, str],
                       testcase_candidate: str):
        bundle = super()._load_source(source)
        value, source_root, evidence, sources, spec_fp, map1, map2, \
        testcases, shards, paths = bundle
        candidate_path = self._independent_candidate(testcase_candidate)
        candidate = load_document(candidate_path)
        validate_testcase_candidate(
            candidate, map1, map2, testcases, value, spec_fp,
            self.staged.policy_fingerprint,
            ProjectJobError)
        for name, relative in (
                ("owner_routing", "audit/scenario_owner_review_submission.json"),
                ("scenario_spec_issues", "staging/mappings/scenario_spec_issues.r000.json")):
            self._regular(source_root / relative, "MISSING_OWNER_ROUTING",
                          "source Reviewer routing evidence is missing")
            paths[name] = relative
        return (value, source_root, evidence, sources, spec_fp, map1, map2,
                testcases, shards, paths, candidate, candidate_path)

    @staticmethod
    def _regular(path: Path, code: str, message: str) -> Path:
        if not path.is_file() or path.is_symlink():
            raise ProjectJobError(code, message)
        return path

    def _manifest(self, submission: dict[str, Any], submission_bytes: bytes,
                  reviewer: dict[str, str], source_value: dict[str, Any],
                  source_root: Path, spec_fp: str, map1: dict[str, Any],
                  map2: dict[str, Any], candidate: dict[str, Any],
                  source_paths: dict[str, str],
                  candidate_path: Path) -> dict[str, Any]:
        artifacts = {name: {"path": path,
                            "fingerprint": _sha256(source_root / path)}
                     for name, path in sorted(source_paths.items())}
        value = {
            "schema_version": "2.0",
            "artifact_kind": "STAGE3_REVIEWER_TEST_MANIFEST",
            "job_id": submission["job_id"],
            "submission": {
                "baseline_path": SUBMISSION_PATH,
                "byte_fingerprint": hashlib.sha256(submission_bytes).hexdigest(),
                "document_fingerprint": canonical_hash(submission),
            },
            "source_project_job_id": source_value["job_id"],
            "source_input_fingerprint": source_value["input_fingerprint"],
            "source_spec_fingerprint": spec_fp,
            "source_scenario_ac_map_fingerprint": map1["artifact_fingerprint"],
            "source_ac_testcase_map_fingerprint": map2["artifact_fingerprint"],
            "source_candidate_fingerprint": candidate["candidate_fingerprint"],
            "testcase_candidate": {
                "path": submission["testcase_candidate"],
                "file_fingerprint": _sha256(candidate_path),
            },
            "source_artifacts": artifacts,
            "reviewer": copy.deepcopy(reviewer),
            "input_authority": copy.deepcopy(submission["input_authority"]),
            "input_authority_fingerprint": canonical_hash(submission["input_authority"]),
            "review_policy_fingerprint": self.staged.policy_fingerprint,
            "provider_attempts": 1,
            "qualification_scope": "TEST_ONLY_NO_PROMOTION_OR_EDA",
            "manifest_fingerprint": "0" * 64,
        }
        value["manifest_fingerprint"] = artifact_fingerprint(
            value, "manifest_fingerprint")
        return value

    def _persist_manifest(self, job_root: Path, manifest: dict[str, Any],
                          submission_bytes: bytes) -> None:
        manifest_path, submission_path = job_root / MANIFEST_PATH, job_root / SUBMISSION_PATH
        if manifest_path.exists():
            if (not manifest_path.is_file() or manifest_path.is_symlink() or
                    load_document(manifest_path) != manifest or
                    not submission_path.is_file() or submission_path.is_symlink() or
                    submission_path.read_bytes() != submission_bytes):
                raise ProjectJobError(
                    "STALE_EVIDENCE", "Reviewer test Job conflicts with immutable input")
            return
        existing = {
            item.relative_to(job_root).as_posix()
            for item in job_root.rglob("*")
            if item.is_file() and not is_pending_artifact(item)
        } if job_root.exists() else set()
        if existing:
            raise ProjectJobError(
                "PARTIAL_ARTIFACT", "Reviewer test Job has partial unbound artifacts")
        self.staged._immutable_text(submission_path, submission_bytes.decode("utf-8"))
        self.staged._immutable_json(manifest_path, manifest)

    def _probe_reviewer(self, job_root: Path, source_root: Path,
                        expected: dict[str, str]) -> None:
        provider = self.workflow.reviewer_provider
        if provider is None:
            raise ProjectJobError("BLOCKED_TOOL", "Reviewer provider is unavailable")
        path = job_root / "audit/reviewer_provider_probe.json"
        if path.exists():
            probe = load_document(self._regular(path, "STALE_EVIDENCE",
                                                 "Reviewer probe evidence is unsafe"))
        else:
            source_path = source_root / "audit/reviewer_provider_probe.json"
            probe = (load_document(source_path) if source_path.is_file() and
                     not source_path.is_symlink() else None)
            if not self._matching_probe(probe, expected):
                try:
                    probe = provider.probe()
                except Exception as caught:
                    raise ProjectJobError("BLOCKED_TOOL", "Reviewer provider probe failed safely") from caught
            self.staged._immutable_json(path, probe)
        if not self._matching_probe(probe, expected):
            raise ProjectJobError("BLOCKED_TOOL", "Reviewer provider probe failed")
        restore = getattr(provider, "restore_probe", None)
        if callable(restore):
            try:
                restore(probe)
            except Exception as caught:
                raise ProjectJobError("STALE_EVIDENCE", "Reviewer probe cannot activate provider") from caught

    @staticmethod
    def _matching_probe(probe: Any, expected: dict[str, str]) -> bool:
        return (isinstance(probe, dict) and
                accepted(validate("provider_probe", probe)) and
                probe.get("provider_id") == expected["provider_id"] and
                probe.get("model_id") == expected["model_id"] and
                probe.get("status") == "PASS" and
                bool(probe.get("tool_call_capable")))

    def _routing(self, source_root: Path, source_value: dict[str, Any],
                 map1: dict[str, Any]) -> dict[str, Any]:
        owner = load_document(self._regular(
            source_root / "audit/scenario_owner_review_submission.json",
            "MISSING_OWNER_ROUTING", "source Owner routing is missing"))
        return self.staged._review_routing_context(source_root, source_value, map1, {
            "owner_review_submission_path": "audit/scenario_owner_review_submission.json",
            "owner_review_submission_fingerprint": owner.get("submission_fingerprint"),
            "spec_issues": "staging/mappings/scenario_spec_issues.r000.json",
        })

    def _load_result(self, job_root: Path, manifest: dict[str, Any],
                     test_job_id: str, map1: dict[str, Any],
                     map2: dict[str, Any], candidate: dict[str, Any],
                     sources: dict[str, str]) -> dict[str, Any] | None:
        path = job_root / RESULT_PATH
        if not path.exists():
            return None
        result = load_document(self._regular(path, "STALE_EVIDENCE", "Reviewer test result is unsafe"))
        if (result.get("result_fingerprint") != artifact_fingerprint(result, "result_fingerprint") or
                result.get("manifest_fingerprint") != manifest["manifest_fingerprint"] or
                result.get("job_id") != test_job_id or
                result.get("candidate_fingerprint") != candidate["candidate_fingerprint"]):
            raise ProjectJobError("STALE_EVIDENCE", "Reviewer test result lineage is stale")
        report = load_document(self._regular(job_root / result.get("review_report_path", ""),
                                              "PARTIAL_ARTIFACT", "Reviewer report is missing"))
        validation = load_document(self._regular(job_root / result.get("review_validation_path", ""),
                                                  "PARTIAL_ARTIFACT", "Reviewer validation is missing"))
        validate_review_report(report, load_document(job_root / result["review_request_path"]),
                               map1, map2, candidate, sources, ProjectJobError)
        if validation.get("validation_fingerprint") != result.get("validation_fingerprint"):
            raise ProjectJobError("STALE_EVIDENCE", "Reviewer validation is stale")
        store = self.staged._incremental_store(job_root)
        stage1_index, _ = store.load(
            "STAGE1", "CURRENT", map1["revision"], test_job_id)
        stage2_index, _ = store.load(
            "STAGE2", "CURRENT", map2["revision"], test_job_id)
        stage3_index, _, assembly = store.load_stage3(
            candidate["revision"], test_job_id)
        review_index, _ = store.load(
            "REVIEW", "CURRENT", max(0, report.get("review_attempt", 0)),
            test_job_id)
        if result.get("artifact_unit_roots") != {
                "stage1": stage1_index["root_fingerprint"],
                "stage2": stage2_index["root_fingerprint"],
                "stage3": stage3_index["root_fingerprint"],
                "assembly": assembly["assembly_fingerprint"],
                "review": review_index["root_fingerprint"]}:
            raise ProjectJobError(
                "STALE_EVIDENCE", "Reviewer test unit roots are stale")
        return result

    def run(self, submission: dict[str, Any], submission_bytes: bytes | None = None) -> dict[str, Any]:
        submission = validate_stage3_reviewer_submission(submission)
        if submission_bytes is None:
            submission_bytes = yaml.safe_dump(submission, sort_keys=False, allow_unicode=True).encode("utf-8")
        if (not isinstance(submission_bytes, bytes) or not submission_bytes or
                len(submission_bytes) > 1024 * 1024 or b"\x00" in submission_bytes):
            raise ProjectJobError("INVALID_SCHEMA", "standalone Reviewer YAML is oversized")
        try:
            if yaml.safe_load(submission_bytes.decode("utf-8")) != submission:
                raise ProjectJobError("STALE_EVIDENCE", "parsed Reviewer YAML differs from exact input bytes")
        except (UnicodeError, yaml.YAMLError) as caught:
            raise ProjectJobError("INVALID_SCHEMA", "standalone Reviewer YAML is invalid") from caught
        if submission["job_id"] == submission["source"]["project_job_id"]:
            raise ProjectJobError("INVALID_INPUT", "test Job must differ from source Job")
        if (not TEST_JOB_ID.fullmatch(submission["job_id"]) or
                not SOURCE_JOB_ID.fullmatch(submission["job_id"]) or
                not SOURCE_JOB_ID.fullmatch(submission["source"]["project_job_id"])):
            raise ProjectJobError("INVALID_INPUT", "Reviewer Job ID is invalid")
        config = load_stage3_provider_config(self.root, submission["reviewer_config"])
        reviewer = {
            "path": submission["reviewer_config"],
            "baseline_path": submission["reviewer_config"],
            "fingerprint": _sha256(self.root / submission["reviewer_config"]),
            "document_fingerprint": canonical_hash(config),
            "provider_id": config["provider_id"],
            "model_id": config["model_id"],
            "auth_env": config["auth_env"],
        }
        job_root = self._job_root(submission["job_id"])
        (source_value, source_root, evidence, sources, spec_fp, map1, map2,
         testcases, shards, paths, candidate, candidate_path) = self._source_bundle(
             submission["source"], submission["testcase_candidate"])
        manifest = self._manifest(submission, submission_bytes, reviewer, source_value,
                                  source_root, spec_fp, map1, map2, candidate, paths,
                                  candidate_path)
        self._persist_manifest(job_root, manifest, submission_bytes)
        # The shared review contract binds the source Job's exact Human
        # routing evidence, so its request/report retain the source Job ID.
        # The separately selected test Job is bound by this manifest/result.
        execution = copy.deepcopy(source_value)
        execution["agent_profile"]["bindings"]["review"]["initial"] = \
            copy.deepcopy(reviewer)
        prior = self._load_result(job_root, manifest, submission["job_id"], map1, map2, candidate, sources)
        if prior is not None:
            return prior
        self._probe_reviewer(job_root, source_root, reviewer)
        routing = self._routing(source_root, source_value, map1)
        owner_scope = routing["owner_routing_decision"]["submission"][
            "submission_fingerprint"]
        unit_store = self.staged._incremental_store(job_root)
        stage1_bundle = unit_store.persist_stage1(
            map1, owner_scope, "CURRENT", submission["job_id"])
        stage2_bundle = unit_store.persist_stage2(
            map2, testcases, stage1_bundle["units"], owner_scope,
            submission["job_id"])
        stage3_bundle = unit_store.persist_stage3(
            candidate, stage1_bundle["units"], stage2_bundle["units"],
            owner_scope, submission["job_id"])
        request = build_review_request(execution, evidence, spec_fp, map1, map2, shards,
                                       candidate, reviewer, 1, ProjectJobError, routing)
        request_path = "staging/reviews/review_request.r001.json"
        report_path = "staging/reviews/review_report.r001.json"
        validation_path = "staging/validations/review_validation.r001.json"
        self.staged._persist_artifact(job_root, request_path, request)
        base = provider_review_request(request)
        budget = self.staged._existing_usage(job_root)
        attempt = 0
        rejection = None
        while True:
            provider_request = copy.deepcopy(base)
            tag = "review.r001"
            if attempt:
                tag = "review.r001.retry{:03d}".format(attempt)
                provider_request["metadata"]["retry_attempt"] = attempt
                provider_request["metadata"]["review_attempt"] = attempt
                provider_request["messages"].append({
                    "role": "USER",
                    "content": json.dumps({
                        "review_correction_feedback": rejection,
                        "required_action": (
                            "Submit a new complete Reviewer candidate that "
                            "corrects the typed validation failures."),
                    }, sort_keys=True, ensure_ascii=False),
                })
            response_path = job_root / (
                "audit/pj002_provider_response.{}.json".format(tag))
            persisted_before_run = response_path.exists()
            response = self.staged._invoke(
                execution, job_root, "REVIEWER", provider_request,
                budget, tag)
            try:
                report = build_review_report(
                    request, candidate, response, sources,
                    ProjectJobError, attempt)
                validation = validate_review_report(
                    report, request, map1, map2, candidate, sources,
                    ProjectJobError)
                break
            except ProjectJobError as caught:
                try:
                    prior_candidate = self.staged._response_stage_candidate(
                        response)
                except ProjectJobError:
                    prior_candidate = {}
                rejection = self.staged._persist_review_rejection(
                    job_root, execution, tag, request, request_path,
                    1, attempt,
                    response, prior_candidate, caught)
                if not persisted_before_run:
                    raise ProjectJobError(
                        "ATTEMPT_PAUSED",
                        "Reviewer candidate attempt failed; rerun the same "
                        "test Job to create a new attempt") from caught
                attempt += 1
        self.staged._persist_artifact(job_root, report_path, report)
        self.staged._persist_artifact(job_root, validation_path, validation)
        review_bundle = unit_store.persist_review(
            report, stage1_bundle["units"], stage2_bundle["units"],
            stage3_bundle["units"], owner_scope,
            self.staged.policy_fingerprint, spec_fp, submission["job_id"])
        result = {
            "schema_version": "2.0", "artifact_kind": "STAGE3_REVIEWER_TEST_RESULT",
            "status": "PASS", "reviewer_verdict": report["verdict"], "job_id": submission["job_id"],
            "source_project_job_id": source_value["job_id"], "manifest_fingerprint": manifest["manifest_fingerprint"],
            "candidate_fingerprint": candidate["candidate_fingerprint"], "review_request_path": request_path,
            "review_report_path": report_path, "review_validation_path": validation_path,
            "review_fingerprint": report["report_fingerprint"], "validation_fingerprint": validation["validation_fingerprint"],
            "artifact_unit_roots": {
                "stage1": stage1_bundle["index"]["root_fingerprint"],
                "stage2": stage2_bundle["index"]["root_fingerprint"],
                "stage3": stage3_bundle["index"]["root_fingerprint"],
                "assembly": stage3_bundle["assembly"]["assembly_fingerprint"],
                "review": review_bundle["index"]["root_fingerprint"],
            },
            "provider_calls": len(list(job_root.glob("audit/pj002_provider_response.review.r001*.json"))),
            "qualification_scope": "TEST_ONLY_NO_PROMOTION_OR_EDA", "result_fingerprint": "0" * 64,
        }
        result["result_fingerprint"] = artifact_fingerprint(result, "result_fingerprint")
        self.staged._immutable_json(job_root / RESULT_PATH, result)
        return result


load_reviewer_provider_config = load_stage3_provider_config


__all__ = ["StandaloneStage3ReviewerWorkflow", "load_reviewer_provider_config",
           "validate_stage3_reviewer_submission"]
